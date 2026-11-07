"""Process-wide database engine and context-local sessions."""

from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from ..exc import ImproperlyConfigured
from .config import DbSettings
from .connection import build_async_engine, build_async_session_local, build_session_local_from_settings
from .session_factory import AsyncSessionFactory, SessionFactory


class DatabaseNotConfiguredError(ImproperlyConfigured):
    """Raised when database access is attempted before initialization."""

    detail: str = "Database is not configured. Call db.init() first."


def _async_engine_has_connections(engine: AsyncEngine) -> bool:
    """Return whether *engine* has opened a pooled connection.

    Closing those connections with ``sync_engine.dispose()`` runs driver IO
    outside the event loop and logs ``MissingGreenlet``. Queue-style pools
    report checked-in and checked-out connections. SQLite ``StaticPool`` keeps
    its single connection in instance state once that connection exists.
    """

    pool = engine.sync_engine.pool
    checked_in = getattr(pool, "checkedin", None)
    checked_out = getattr(pool, "checkedout", None)
    if callable(checked_in) and callable(checked_out):
        return checked_in() > 0 or checked_out() > 0
    return "connection" in pool.__dict__


def _discard_async_engine(engine: AsyncEngine) -> None:
    """Drop *engine* from a synchronous caller.

    An engine that has not opened a connection is disposed here, because that
    dispose performs no driver IO. An engine that has been used is left for
    ``await aclose()``; disposing it synchronously does not close the
    connections.
    """

    if not _async_engine_has_connections(engine):
        engine.sync_engine.dispose()


class DatabaseManager:
    """Own database initialization state and expose lazy session access.

    ``close()`` disposes the synchronous engine. ``aclose()`` disposes the
    asynchronous engine. Call both when both were built. Neither method shuts
    down the module registry; process shutdown wiring belongs to the caller
    that owns the process lifetime.
    """

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._session_factory: SessionFactory | None = None
        self._async_engine: AsyncEngine | None = None
        self._async_session_factory: AsyncSessionFactory | None = None
        self._settings: DbSettings | None = None
        self._database_url: str | None = None

    def init(self, url: str | None = None, settings: DbSettings | None = None) -> None:
        """Initialize or safely replace the synchronous engine and session factory.

        Replacement is supported. The new engine is constructed before the
        existing synchronous engine is disposed, so a failed initialization
        leaves the current database configuration usable.

        A previous async engine that has already opened a connection is not
        disposed here. ``sync_engine.dispose()`` closes those connections
        outside the event loop and fails with ``MissingGreenlet``. Call
        ``await aclose()`` before ``init()`` when that engine should be closed.
        An async engine that has not opened a connection is disposed, because
        that close does no driver IO. The next async access builds a new engine
        from the replacement configuration.
        """

        resolved_settings = settings or DbSettings()
        resolved_url = url or resolved_settings.SQLALCHEMY_DATABASE_URI
        engine, session_local = build_session_local_from_settings(
            settings=resolved_settings,
            database_url=resolved_url,
        )

        previous_engine = self._engine
        previous_session_factory = self._session_factory
        previous_async_engine = self._async_engine
        previous_async_factory = self._async_session_factory
        if previous_session_factory is not None:
            previous_session_factory.clear_session()
        if previous_async_factory is not None:
            previous_async_factory.clear_session()
        if previous_engine is not None:
            previous_engine.dispose()
        if previous_async_engine is not None:
            _discard_async_engine(previous_async_engine)

        self._engine = engine
        self._session_factory = SessionFactory(session_local)
        self._async_engine = None
        self._async_session_factory = None
        self._settings = resolved_settings
        self._database_url = resolved_url

    def close(self) -> None:
        """Dispose the synchronous engine and drop synchronous session state.

        An async engine that was already built stays open and usable. Call
        :meth:`aclose` to dispose it. When both engines were built, call both
        methods. Synchronous access raises :class:`DatabaseNotConfiguredError`
        until :meth:`init`. The call is safe to repeat.
        """

        if self._session_factory is not None:
            self._session_factory.clear_session()
        if self._engine is not None:
            self._engine.dispose()
        self._engine = None
        self._session_factory = None
        self._forget_config_if_idle()

    async def aclose(self) -> None:
        """Dispose the asynchronous engine and drop asynchronous session state.

        The synchronous engine stays open and usable. Call :meth:`close` to
        dispose it. When both engines were built, call both methods. The next
        async access builds a new async engine when synchronous configuration
        is still in place. The call is safe to repeat.
        """

        if self._async_session_factory is not None:
            await self._async_session_factory.aclear_session()
        if self._async_engine is not None:
            await self._async_engine.dispose()
        self._async_engine = None
        self._async_session_factory = None
        self._forget_config_if_idle()

    @property
    def engine(self) -> Engine:
        """Return the configured SQLAlchemy engine."""

        if self._engine is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.engine.")
        return self._engine

    @property
    def aengine(self) -> AsyncEngine:
        """Return the async engine for the URL resolved by :meth:`init`."""

        self._ensure_async()
        if self._async_engine is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.aengine.")
        return self._async_engine

    @property
    def session(self) -> Session:
        """Return the current context-local SQLAlchemy session."""

        if self._session_factory is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.session.")
        return self._session_factory.session

    @property
    def asession(self) -> AsyncSession:
        """Return the current context-local async session."""

        return self._ensure_async().session

    @property
    def session_factory(self) -> SessionFactory:
        """Return the current context-local SQLAlchemy session."""

        if self._session_factory is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.session.")
        return self._session_factory

    @property
    def settings(self) -> DbSettings:
        """Return the resolved database settings."""

        if self._settings is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.settings.")
        return self._settings

    def set_session(self, session: Session) -> None:
        """Bind an existing session to the current execution context."""

        if self._session_factory is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before binding sessions.")
        self._session_factory.set_session(session)

    def clear_session(self) -> None:
        """Clear the current execution context session binding."""

        if self._session_factory is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before clearing sessions.")
        self._session_factory.clear_session()

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Proxy to SessionFactory.session_scope."""

        if self._session_factory is None:
            raise DatabaseNotConfiguredError(
                "Database is not initialized. Call db.init(...) before using session_scope."
            )
        with self._session_factory.session_scope() as session:
            yield session

    @contextmanager
    def independent_session(self) -> Generator[Session, None, None]:
        """Proxy to SessionFactory.independent_session."""

        if self._session_factory is None:
            raise DatabaseNotConfiguredError(
                "Database is not initialized. Call db.init(...) before using independent_session."
            )
        with self._session_factory.independent_session() as session:
            yield session

    @asynccontextmanager
    async def asession_scope(self) -> AsyncIterator[AsyncSession]:
        """Bind a transactional async session to the current context.

        Commit, rollback, and close follow :meth:`session_scope`. The async
        session uses a separate context variable from the sync session.
        """

        factory = self._ensure_async()
        async with factory.asession_scope() as session:
            yield session

    @asynccontextmanager
    async def aindependent_session(self) -> AsyncIterator[AsyncSession]:
        """Open an async transaction that does not bind ``asession``.

        Commit and rollback follow :meth:`independent_session`.
        """

        factory = self._ensure_async()
        async with factory.aindependent_session() as session:
            yield session

    def _ensure_async(self) -> AsyncSessionFactory:
        """Return the async session factory, building the engine on first use.

        An engine left in place by :meth:`close` is returned as-is. Building a
        new one still requires :meth:`init` so the synchronous configuration
        and URL are known.
        """

        if self._async_engine is not None and self._async_session_factory is not None:
            return self._async_session_factory
        if self._settings is None or self._session_factory is None or self._database_url is None:
            raise DatabaseNotConfiguredError(
                "Database is not initialized. Call db.init(...) before using async sessions."
            )
        self._async_engine = build_async_engine(self._settings, database_url=self._database_url)
        self._async_session_factory = AsyncSessionFactory(build_async_session_local(self._async_engine))
        return self._async_session_factory

    def _forget_config_if_idle(self) -> None:
        """Drop settings once neither engine remains."""

        if (
            self._engine is None
            and self._session_factory is None
            and self._async_engine is None
            and self._async_session_factory is None
        ):
            self._settings = None
            self._database_url = None


# Public singleton mirroring Flask-SQLAlchemy style usage.
db = DatabaseManager()
