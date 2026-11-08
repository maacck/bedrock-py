"""Behavior of the bedrock-fastapi middleware and exception handler inside a FastAPI app."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from bedrock.database import DbSettings, db
from bedrock.di import Lifetime, container
from bedrock.exc import BedrockExc, ImproperlyConfigured
from bedrock_fastapi import (
    AsyncDbSessionMiddleware,
    DbSessionMiddleware,
    RequestScopeMiddleware,
    bedrock_exception_handler,
)
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from sqlalchemy import String, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the integration tests."""


class Item(Base):
    """Row written by request handlers."""

    __tablename__ = "fastapi_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


def _init_db(tmp_path: Path) -> None:
    db.init(
        settings=DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(tmp_path / "a.db"))
    )
    Base.metadata.create_all(db.engine)


def _app(tmp_path: Path) -> FastAPI:
    """Build an app whose lifespan owns the database, as a host application would."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _init_db(tmp_path)
        try:
            yield
        finally:
            await db.aclose()
            db.close()

    return FastAPI(lifespan=lifespan)


def _stored_ids() -> list[int]:
    with db.independent_session() as session:
        return sorted(session.scalars(select(Item.id)))


class Tracker:
    """SCOPED service that records its instances and whether the scope closed them."""

    instances: list["Tracker"] = []

    def __init__(self) -> None:
        self.closed = False
        Tracker.instances.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def scoped_tracker() -> Iterator[None]:
    Tracker.instances.clear()
    container.register(Tracker, factory=Tracker, lifetime=Lifetime.SCOPED)
    yield
    container.reset()


def test_request_scope_is_shared_per_request_and_closed(tmp_path: Path, scoped_tracker: None) -> None:
    """Sync dependencies run in worker threads; they still join the request scope, and it closes cleanly."""

    app = _app(tmp_path)
    app.add_middleware(RequestScopeMiddleware)

    def resolve_tracker() -> Tracker:
        return container.resolve(Tracker)

    @app.get("/sync")
    def sync_endpoint(dep: Tracker = Depends(resolve_tracker)) -> dict[str, bool]:
        return {"same": dep is container.resolve(Tracker)}

    @app.get("/async")
    async def async_endpoint(dep: Tracker = Depends(resolve_tracker)) -> dict[str, bool]:
        return {"same": dep is container.resolve(Tracker)}

    with TestClient(app) as client:
        assert client.get("/sync").json() == {"same": True}
        assert client.get("/async").json() == {"same": True}

    assert len(Tracker.instances) == 2
    assert all(tracker.closed for tracker in Tracker.instances)


def test_db_session_middleware_keeps_only_what_the_handler_commits(tmp_path: Path) -> None:
    """Both middleware together: one session per request; uncommitted work is rolled back at request end."""

    app = _app(tmp_path)
    app.add_middleware(DbSessionMiddleware)
    app.add_middleware(AsyncDbSessionMiddleware)
    sessions: list = []

    @app.post("/sync/{item_id}")
    def create_sync(item_id: int, commit: bool = False) -> None:
        sessions.append(db.session)
        db.session.add(Item(id=item_id, name="sync"))
        if commit:
            db.session.commit()

    @app.post("/async/{item_id}")
    async def create_async(item_id: int, commit: bool = False) -> None:
        sessions.append(db.asession)
        db.asession.add(Item(id=item_id, name="async"))
        if commit:
            await db.asession.commit()

    paths = (
        "/sync/1?commit=true",
        "/sync/2",
        "/async/3?commit=true",
        "/async/4",
        "/sync/1?commit=true",
        "/async/3?commit=true",
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        statuses = [client.post(path).status_code for path in paths]
        assert _stored_ids() == [1, 3]

    # A duplicate key fails the handler's commit, so the client gets an error instead of a success.
    assert statuses == [200, 200, 200, 200, 500, 500]
    assert len({id(session) for session in sessions}) == 6
    assert not any(session.in_transaction() for session in sessions)


def test_db_session_middleware_rolls_back_cleanly_when_the_request_is_cancelled(tmp_path: Path) -> None:
    """A cancelled request, such as one hitting a timeout, still rolls back instead of discarding the connection."""

    invalidations: list[BaseException | None] = []

    async def run() -> None:
        _init_db(tmp_path)
        event.listen(db.aengine.sync_engine, "invalidate", lambda conn, record, exc: invalidations.append(exc))
        try:
            with anyio.CancelScope() as request_scope:

                async def handler(scope: dict, receive: object, send: object) -> None:
                    db.session.execute(select(Item))
                    await db.asession.execute(select(Item))
                    request_scope.cancel()
                    await anyio.sleep(0)

                await DbSessionMiddleware(AsyncDbSessionMiddleware(handler))({"type": "http"}, None, None)
        finally:
            await db.aclose()
            db.close()

    anyio.run(run)
    assert invalidations == []


class InventoryError(BedrockExc):
    detail = "Inventory failed."


class ItemNotFound(InventoryError):
    detail = "Item not found."


class ItemLocked(InventoryError):
    detail = "Item is locked."


def test_exception_handler_maps_by_mro_and_hides_unmapped(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.add_exception_handler(BedrockExc, bedrock_exception_handler({InventoryError: 400, ItemNotFound: 404}))

    @app.get("/missing")
    async def missing() -> None:
        raise ItemNotFound("Item 7 not found.")

    @app.get("/locked")
    async def locked() -> None:
        raise ItemLocked()

    @app.get("/misconfigured")
    async def misconfigured() -> None:
        raise ImproperlyConfigured("dsn=postgres://secret")

    with TestClient(app, raise_server_exceptions=False) as client:
        missing_response = client.get("/missing")
        locked_response = client.get("/locked")
        misconfigured_response = client.get("/misconfigured")

    assert (missing_response.status_code, missing_response.json()) == (404, {"detail": "Item 7 not found."})
    assert (locked_response.status_code, locked_response.json()) == (400, {"detail": "Item is locked."})
    assert misconfigured_response.status_code == 500
    assert "secret" not in misconfigured_response.text


def test_exception_handler_leaves_websocket_errors_unanswered(tmp_path: Path) -> None:
    """After accept() an HTTP response is impossible, so a mapped exception still propagates."""

    app = _app(tmp_path)
    app.add_exception_handler(BedrockExc, bedrock_exception_handler({ItemNotFound: 404}))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        raise ItemNotFound()

    with TestClient(app) as client, pytest.raises(ItemNotFound):
        with client.websocket_connect("/ws") as connection:
            connection.receive()
