"""Tests for async sessions, missing drivers, and engine shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bedrock.database.config import DatabaseConfigurationError, DbSettings
from bedrock.database.connection import build_async_engine
from bedrock.database.manager import DatabaseManager, DatabaseNotConfiguredError
from bedrock.database.session_factory import _current_async_db_session, _current_db_session
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for async session tests."""


class Item(Base):
    """Minimal model stored by the async session tests."""

    __tablename__ = "async_items"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)


def _settings(tmp_path: Path) -> DbSettings:
    """Settings for a file-backed SQLite database with the aiosqlite driver."""

    return DbSettings(
        TYPE="sqlite",
        DRIVER="pysqlite",
        ASYNC_DRIVER="aiosqlite",
        SCHEMA=str(tmp_path / "async.db"),
    )


def _manager(tmp_path: Path) -> DatabaseManager:
    """Initialize a manager and create the async test table."""

    manager = DatabaseManager()
    manager.init(settings=_settings(tmp_path))
    Base.metadata.create_all(manager.engine)
    manager.engine.dispose()
    return manager


@pytest.fixture(autouse=True)
def _reset_session_context() -> None:
    """Drop context-local sessions left behind by a failed test."""

    _current_db_session.set(None)
    _current_async_db_session.set(None)
    yield
    _current_db_session.set(None)
    _current_async_db_session.set(None)


class TestAsyncSessions:
    """Async scopes mirror the sync commit, rollback, and context rules."""

    @pytest.mark.asyncio
    async def test_asession_scope_commits(self, tmp_path: Path) -> None:
        """asession_scope commits work that a later session can read."""

        manager = _manager(tmp_path)
        try:
            async with manager.asession_scope() as session:
                assert isinstance(session, AsyncSession)
                session.add(Item(name="committed"))

            async with manager.aindependent_session() as session:
                found = await session.scalar(select(Item).where(Item.name == "committed"))

            assert found is not None
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_asession_scope_rolls_back(self, tmp_path: Path) -> None:
        """asession_scope rolls back when the block raises."""

        manager = _manager(tmp_path)
        try:
            with pytest.raises(ValueError, match="boom"):
                async with manager.asession_scope() as session:
                    session.add(Item(name="rolled-back"))
                    raise ValueError("boom")

            async with manager.asession_scope() as session:
                found = await session.scalar(select(Item).where(Item.name == "rolled-back"))

            assert found is None
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_async_contexts_are_isolated_across_tasks(self, tmp_path: Path) -> None:
        """Concurrent tasks do not share the async session context variable."""

        manager = _manager(tmp_path)
        seen: list[tuple[str, int]] = []

        async def worker(label: str) -> None:
            async with manager.asession_scope() as session:
                seen.append((label, id(session)))
                await asyncio.sleep(0.05)
                assert manager.asession is session

        try:
            await asyncio.gather(worker("a"), worker("b"))
            assert len(seen) == 2
            assert seen[0][1] != seen[1][1]
        finally:
            await manager.aclose()

    @pytest.mark.asyncio
    async def test_async_session_does_not_replace_the_sync_session(self, tmp_path: Path) -> None:
        """Sync and async sessions use different context variables."""

        manager = _manager(tmp_path)
        try:
            with manager.session_scope() as sync_session:
                async with manager.asession_scope() as async_session:
                    assert manager.session is sync_session
                    assert manager.asession is async_session
                    assert sync_session is not async_session
        finally:
            await manager.aclose()


class TestAsyncDriverErrors:
    """Missing or unset async drivers raise a Bedrock configuration error."""

    def test_missing_driver_names_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A missing known driver names the bedrock-core extra that provides it."""

        import bedrock.database.connection as connection

        real_import = connection.importlib.import_module

        def fake_import(name: str, package: str | None = None) -> object:
            if name == "asyncpg":
                raise ImportError("no asyncpg")
            return real_import(name, package)

        monkeypatch.setattr(connection.importlib, "import_module", fake_import)
        settings = DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="asyncpg", SCHEMA=":memory:")

        with pytest.raises(DatabaseConfigurationError, match="async-postgresql") as exc_info:
            build_async_engine(settings)

        assert "super-secret-password" not in str(exc_info.value)
        assert "bedrock-core[async-postgresql]" in str(exc_info.value)

    def test_manager_reports_missing_driver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DatabaseManager.aengine surfaces the same missing-driver error."""

        import bedrock.database.connection as connection

        real_import = connection.importlib.import_module

        def fake_import(name: str, package: str | None = None) -> object:
            if name == "asyncpg":
                raise ImportError("no asyncpg")
            return real_import(name, package)

        monkeypatch.setattr(connection.importlib, "import_module", fake_import)
        manager = DatabaseManager()
        manager.init(settings=DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="asyncpg", SCHEMA=":memory:"))
        try:
            with pytest.raises(DatabaseConfigurationError, match="async-postgresql"):
                _ = manager.aengine
        finally:
            manager.close()

    def test_async_access_requires_a_driver(self) -> None:
        """Async helpers explain that DATABASE_ASYNC_DRIVER is required."""

        manager = DatabaseManager()
        manager.init("sqlite:///:memory:")
        try:
            with pytest.raises(DatabaseConfigurationError, match="DATABASE_ASYNC_DRIVER"):
                _ = manager.aengine
        finally:
            manager.close()

    @pytest.mark.asyncio
    async def test_async_scope_requires_init(self) -> None:
        """Async scopes fail with the same not-configured error as sync scopes."""

        manager = DatabaseManager()
        with pytest.raises(DatabaseNotConfiguredError):
            async with manager.asession_scope():
                pass


class TestEngineShutdown:
    """close and aclose dispose owned engines and can be called twice."""

    def test_close_is_idempotent_and_reinitializable(self) -> None:
        """close() disposes the sync engine once and leaves init() usable."""

        manager = DatabaseManager()
        manager.init("sqlite:///:memory:")
        engine = manager.engine
        calls: list[int] = []
        original = engine.dispose

        def dispose(*args: object, **kwargs: object) -> None:
            calls.append(1)
            original(*args, **kwargs)

        engine.dispose = dispose  # type: ignore[method-assign]

        manager.close()
        manager.close()

        assert calls == [1]
        with pytest.raises(DatabaseNotConfiguredError):
            _ = manager.engine

        manager.init("sqlite:///:memory:")
        assert manager.engine is not None
        manager.close()

    @pytest.mark.asyncio
    async def test_close_disposes_the_async_engine(self, tmp_path: Path) -> None:
        """The synchronous close path disposes an async engine that was created."""

        manager = _manager(tmp_path)
        async_engine = manager.aengine
        calls: list[int] = []
        original = async_engine.sync_engine.dispose

        def dispose(*args: object, **kwargs: object) -> None:
            calls.append(1)
            original(*args, **kwargs)

        async_engine.sync_engine.dispose = dispose  # type: ignore[method-assign]
        manager.close()
        manager.close()

        assert calls == [1]
        with pytest.raises(DatabaseNotConfiguredError):
            _ = manager.engine

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent_and_reinitializable(self, tmp_path: Path) -> None:
        """aclose() disposes both engines once and allows a later init()."""

        from sqlalchemy.ext.asyncio import AsyncEngine

        settings = _settings(tmp_path)
        manager = _manager(tmp_path)
        async_engine = manager.aengine
        sync_calls: list[int] = []
        async_calls: list[int] = []
        original_sync = manager.engine.dispose
        original_async = AsyncEngine.dispose

        def dispose_sync(*args: object, **kwargs: object) -> None:
            sync_calls.append(1)
            original_sync(*args, **kwargs)

        async def dispose_async(self: AsyncEngine, *args: object, **kwargs: object) -> None:
            if self is async_engine:
                async_calls.append(1)
            await original_async(self, *args, **kwargs)

        manager.engine.dispose = dispose_sync  # type: ignore[method-assign]
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(AsyncEngine, "dispose", dispose_async)
        try:
            await manager.aclose()
            await manager.aclose()
        finally:
            monkeypatch.undo()

        assert sync_calls == [1]
        assert async_calls == [1]

        manager.init(settings=settings)
        with manager.engine.connect() as connection:
            assert connection is not None
        manager.close()
