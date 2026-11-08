"""Tests for database rollback testing helpers."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from bedrock.database import db
from bedrock.database.config import DbSettings
from bedrock.database.manager import DatabaseManager, DatabaseNotConfiguredError
from bedrock.database.session_factory import _current_async_db_session, _current_db_session
from bedrock.testing.database import arollback_session, rollback_session
from sqlalchemy import text


def _settings(tmp_path: Path) -> DbSettings:
    return DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(tmp_path / "test.db"))


def _manager(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager()
    manager.init(settings=_settings(tmp_path))
    with manager.engine.begin() as connection:
        connection.execute(text("CREATE TABLE items (name TEXT NOT NULL)"))
    return manager


@pytest.fixture(autouse=True)
def _reset_session_context() -> Iterator[None]:
    _current_db_session.set(None)
    _current_async_db_session.set(None)
    yield
    _current_db_session.set(None)
    _current_async_db_session.set(None)


def test_sync_commit_is_rolled_back_and_factory_restored(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        with rollback_session(manager) as session:
            assert _current_db_session.get() is None
            with manager.session_scope() as service:
                service.execute(text("INSERT INTO items VALUES ('service')"))
            assert session.scalar(text("SELECT count(*) FROM items")) == 1
            assert manager.session.scalar(text("SELECT count(*) FROM items")) == 1
            manager.clear_session()
            with manager.independent_session() as independent:
                independent.execute(text("INSERT INTO items VALUES ('independent')"))
            assert session.scalar(text("SELECT count(*) FROM items")) == 2
        assert _current_db_session.get() is None
        with manager.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM items")) == 0
        with manager.session_scope() as service:
            service.execute(text("INSERT INTO items VALUES ('persistent')"))
        with manager.engine.begin() as connection:
            connection.execute(text("INSERT INTO items VALUES ('normal')"))
        with manager.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM items")) == 2
    finally:
        manager.close()


def test_sync_exception_rolls_back(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        with pytest.raises(ValueError, match="boom"):
            with rollback_session(manager):
                with manager.session_scope() as service:
                    service.execute(text("INSERT INTO items VALUES ('service')"))
                raise ValueError("boom")
        with manager.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM items")) == 0
    finally:
        manager.close()


def test_sync_uninitialized() -> None:
    with pytest.raises(DatabaseNotConfiguredError, match="session-scoped fixture"):
        with rollback_session(DatabaseManager()):
            pass


@pytest.mark.asyncio
async def test_async_commit_is_rolled_back_and_factory_restored(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        async with arollback_session(manager) as session:
            assert _current_async_db_session.get() is None
            async with manager.asession_scope() as service:
                await service.execute(text("INSERT INTO items VALUES ('service')"))
            assert await session.scalar(text("SELECT count(*) FROM items")) == 1
            assert await manager.asession.scalar(text("SELECT count(*) FROM items")) == 1
            await manager._async_session_factory.aclear_session()
            async with manager.aindependent_session() as independent:
                await independent.execute(text("INSERT INTO items VALUES ('independent')"))
            assert await session.scalar(text("SELECT count(*) FROM items")) == 2
        assert _current_async_db_session.get() is None
        async with manager.aengine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM items")) == 0
        async with manager.asession_scope() as service:
            await service.execute(text("INSERT INTO items VALUES ('persistent')"))
        async with manager.aengine.begin() as connection:
            await connection.execute(text("INSERT INTO items VALUES ('normal')"))
        async with manager.aengine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM items")) == 2
    finally:
        await manager.aclose()
        manager.close()


@pytest.mark.asyncio
async def test_async_exception_rolls_back(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        with pytest.raises(ValueError, match="boom"):
            async with arollback_session(manager):
                async with manager.asession_scope() as service:
                    await service.execute(text("INSERT INTO items VALUES ('service')"))
                raise ValueError("boom")
        async with manager.aengine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM items")) == 0
    finally:
        await manager.aclose()
        manager.close()


@pytest.mark.asyncio
async def test_async_uninitialized() -> None:
    with pytest.raises(DatabaseNotConfiguredError, match="session-scoped fixture"):
        async with arollback_session(DatabaseManager()):
            pass


@pytest.fixture
def initialized_db(tmp_path: Path) -> Iterator[None]:
    db.init(settings=_settings(tmp_path))
    try:
        yield
    finally:
        db.close()


def test_db_session_fixture(initialized_db: None, request: pytest.FixtureRequest) -> None:
    session = request.getfixturevalue("db_session")
    assert session is not db.session
    assert db.session.scalar(text("SELECT 1")) == 1
