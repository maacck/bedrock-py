"""Tests for async sessions, missing drivers, and engine shutdown."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bedrock.database.config import DatabaseConfigurationError, DbSettings
from bedrock.database.connection import async_url_for_driver, build_async_engine
from bedrock.database.manager import DatabaseManager, DatabaseNotConfiguredError
from bedrock.database.session_factory import (
    SessionAlreadyBoundError,
    SessionNotBoundError,
    _current_async_db_session,
    _current_db_session,
)
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.engine.url import make_url
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
            manager.close()

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
            manager.close()

    @pytest.mark.asyncio
    async def test_asession_scope_unbinds_when_close_is_cancelled(self, tmp_path: Path) -> None:
        """A cancellation while closing still unbinds the session, so the next scope can start."""

        manager = _manager(tmp_path)

        async def cancelled_close() -> None:
            raise asyncio.CancelledError

        try:
            with pytest.raises(asyncio.CancelledError):
                async with manager.asession_scope() as session:
                    session.close = cancelled_close  # type: ignore[method-assign]

            assert _current_async_db_session.get() is None
            async with manager.asession_scope():
                pass
        finally:
            await manager.aclose()
            manager.close()

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
            manager.close()

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
            manager.close()

    @pytest.mark.asyncio
    async def test_asession_scope_decorates_async_entry_points(self, tmp_path: Path) -> None:
        """@db.asession_scope() binds a fresh async session for every awaited call."""

        manager = _manager(tmp_path)
        seen: list[AsyncSession] = []

        @manager.asession_scope()
        async def job(name: str) -> None:
            seen.append(manager.asession)
            manager.asession.add(Item(name=name))

        try:
            await job("first")
            await job("second")
            assert seen[0] is not seen[1]
            with pytest.raises(SessionNotBoundError):
                _ = manager.asession
            async with manager.aindependent_session() as session:
                assert set(await session.scalars(select(Item.name))) == {"first", "second"}
        finally:
            await manager.aclose()
            manager.close()

    @pytest.mark.asyncio
    async def test_asession_scope_rejects_nesting(self, tmp_path: Path) -> None:
        """A nested async scope raises and leaves the outer session bound."""

        manager = _manager(tmp_path)
        try:
            async with manager.asession_scope() as outer:
                with pytest.raises(SessionAlreadyBoundError, match="aindependent_session"):
                    async with manager.asession_scope():
                        pass
                assert manager.asession is outer
        finally:
            await manager.aclose()
            manager.close()


class TestExplicitInitUrl:
    """An explicit init URL is the database both engines open."""

    def test_sync_and_async_engines_share_the_explicit_url(self, tmp_path: Path) -> None:
        """db.init(url=...) keeps the async engine on that database, swapping only the driver."""

        explicit = tmp_path / "explicit.db"
        explicit_url = f"sqlite:///{explicit.as_posix()}"
        settings = DbSettings(ASYNC_DRIVER="aiosqlite")
        manager = DatabaseManager()
        manager.init(url=explicit_url, settings=settings)
        try:
            sync_url = make_url(manager.engine.url)
            async_url = make_url(manager.aengine.url)

            assert sync_url.database == explicit.as_posix()
            assert async_url.database == sync_url.database
            assert async_url.get_driver_name() == "aiosqlite"
            assert "bedrock.db" not in str(async_url)
        finally:
            manager.close()

    def test_driver_swap_preserves_the_rest_of_the_resolved_url(self) -> None:
        """Only the driver name changes when the resolved URL is adapted for async."""

        swapped = async_url_for_driver(
            "postgresql+psycopg2://user:p%40ss@db.example:5432/app?sslmode=require",
            "asyncpg",
        )
        parsed = make_url(swapped)

        assert parsed.drivername == "postgresql+asyncpg"
        assert parsed.username == "user"
        assert parsed.password == "p@ss"
        assert parsed.host == "db.example"
        assert parsed.port == 5432
        assert parsed.database == "app"
        assert parsed.query["sslmode"] == "require"

    def test_memory_url_keeps_its_query(self) -> None:
        """SQLite :memory: keeps connection options such as timeout when the driver is swapped."""

        parsed = make_url(async_url_for_driver("sqlite:///:memory:?timeout=60", "aiosqlite"))

        assert parsed.drivername == "sqlite+aiosqlite"
        assert parsed.database == ":memory:"
        assert parsed.query["timeout"] == "60"


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

    @pytest.mark.parametrize("driver", ["psycopg", "aiomysql"])
    def test_driver_outside_every_extra_names_its_package(self, driver: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """A driver no extra installs points at the package itself, not at an extra that lacks it."""

        import bedrock.database.connection as connection

        def fake_import(name: str, package: str | None = None) -> object:
            raise ImportError(f"no {name}")

        monkeypatch.setattr(connection.importlib, "import_module", fake_import)

        with pytest.raises(DatabaseConfigurationError) as exc_info:
            connection.ensure_async_driver_installed(driver)

        assert f"Install the '{driver}' package" in str(exc_info.value)
        assert "bedrock-core[" not in str(exc_info.value)

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
    """close and aclose each dispose one engine and can be called twice."""

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
    async def test_close_leaves_the_async_engine_usable(self, tmp_path: Path) -> None:
        """close() disposes only the sync engine and leaves a built async engine open."""

        manager = _manager(tmp_path)
        sync_engine = manager.engine
        async_engine = manager.aengine
        async with async_engine.connect() as connection:
            assert await connection.scalar(select(1)) == 1

        sync_calls: list[int] = []
        async_calls: list[int] = []
        original_sync = sync_engine.dispose
        original_async = async_engine.sync_engine.dispose

        def dispose_sync(*args: object, **kwargs: object) -> None:
            sync_calls.append(1)
            original_sync(*args, **kwargs)

        def dispose_async(*args: object, **kwargs: object) -> None:
            async_calls.append(1)
            original_async(*args, **kwargs)

        sync_engine.dispose = dispose_sync  # type: ignore[method-assign]
        async_engine.sync_engine.dispose = dispose_async  # type: ignore[method-assign]

        manager.close()
        manager.close()

        assert sync_calls == [1]
        assert async_calls == []
        with pytest.raises(DatabaseNotConfiguredError):
            _ = manager.engine
        with pytest.raises(DatabaseNotConfiguredError):
            _ = manager.session
        assert manager.aengine is async_engine
        async with manager.aengine.connect() as connection:
            assert await connection.scalar(select(1)) == 1

        manager.init(settings=_settings(tmp_path))
        with manager.engine.connect() as connection:
            assert connection.exec_driver_sql("select 1").scalar() == 1
        assert async_calls == []
        await async_engine.dispose()
        await manager.aclose()
        manager.close()

    @pytest.mark.asyncio
    async def test_aclose_leaves_the_sync_engine_usable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """aclose() disposes only the async engine and leaves the sync engine open."""

        from sqlalchemy.ext.asyncio import AsyncEngine

        settings = _settings(tmp_path)
        manager = _manager(tmp_path)
        sync_engine = manager.engine
        async_engine = manager.aengine
        async with async_engine.connect() as connection:
            assert await connection.scalar(select(1)) == 1

        sync_calls: list[int] = []
        async_calls: list[int] = []
        original_sync = sync_engine.dispose
        original_async = AsyncEngine.dispose

        def dispose_sync(*args: object, **kwargs: object) -> None:
            sync_calls.append(1)
            original_sync(*args, **kwargs)

        async def dispose_async(self: AsyncEngine, *args: object, **kwargs: object) -> None:
            if self is async_engine:
                async_calls.append(1)
            await original_async(self, *args, **kwargs)

        sync_engine.dispose = dispose_sync  # type: ignore[method-assign]
        monkeypatch.setattr(AsyncEngine, "dispose", dispose_async)

        await manager.aclose()
        await manager.aclose()

        assert sync_calls == []
        assert async_calls == [1]
        assert manager.engine is sync_engine
        with manager.engine.connect() as connection:
            assert connection.exec_driver_sql("select 1").scalar() == 1
        with manager.session_scope() as session:
            assert session.get_bind() is sync_engine

        manager.init(settings=settings)
        with manager.engine.connect() as connection:
            assert connection.exec_driver_sql("select 1").scalar() == 1
        manager.close()

    @pytest.mark.asyncio
    async def test_init_does_not_sync_dispose_a_used_async_engine(self, tmp_path: Path) -> None:
        """init() drops a used async engine without sync_engine.dispose()."""

        manager = _manager(tmp_path)
        async_engine = manager.aengine
        async with async_engine.connect() as connection:
            assert await connection.scalar(select(1)) == 1

        calls: list[int] = []
        original = async_engine.sync_engine.dispose

        def dispose(*args: object, **kwargs: object) -> None:
            calls.append(1)
            original(*args, **kwargs)

        async_engine.sync_engine.dispose = dispose  # type: ignore[method-assign]
        manager.init(settings=_settings(tmp_path))

        assert calls == []
        with manager.engine.connect() as connection:
            assert connection.exec_driver_sql("select 1").scalar() == 1
        assert manager.aengine is not async_engine
        async with manager.aengine.connect() as connection:
            assert await connection.scalar(select(1)) == 1
        await async_engine.dispose()
        await manager.aclose()
        manager.close()

    def test_init_disposes_an_unused_async_engine(self, tmp_path: Path) -> None:
        """init() may dispose an async engine that has never opened a connection."""

        manager = _manager(tmp_path)
        async_engine = manager.aengine
        calls: list[int] = []
        original = async_engine.sync_engine.dispose

        def dispose(*args: object, **kwargs: object) -> None:
            calls.append(1)
            original(*args, **kwargs)

        async_engine.sync_engine.dispose = dispose  # type: ignore[method-assign]
        manager.init(settings=_settings(tmp_path))

        assert calls == [1]
        with manager.engine.connect() as connection:
            assert connection.exec_driver_sql("select 1").scalar() == 1
        manager.close()
