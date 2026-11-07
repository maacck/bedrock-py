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


class DatabaseManager:
    """Own database initialization state and expose lazy session access.

    ``close()`` and ``aclose()`` dispose engines this manager created. They do
    not shut down the module registry; process shutdown wiring belongs to the
    caller that owns the process lifetime.
    """

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._session_factory: SessionFactory | None = None
        self._async_engine: AsyncEngine | None = None
        self._async_session_factory: AsyncSessionFactory | None = None
        self._settings: DbSettings | None = None
        self._database_url: str | None = None

    def init(self, url: str | None = None, settings: DbSettings | None = None) -> None:
        """Initialize or safely replace the database engine and session factory.

        Replacement is supported. The new engine is constructed before the
        existing engine is disposed, so a failed initialization leaves the
        current database configuration usable. A previously built async engine
        is disposed and built again on the next async access.
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
            previous_async_engine.sync_engine.dispose()

        self._engine = engine
        self._session_factory = SessionFactory(session_local)
        self._async_engine = None
        self._async_session_factory = None
        self._settings = resolved_settings
        self._database_url = resolved_url

    def close(self) -> None:
        """Dispose owned engines and drop configuration.

        The call is safe to repeat. Afterwards :meth:`init` can configure the
        manager again. Async engines are disposed through their synchronous
        engine so this method does not need a running event loop.
        """

        self._close_sessions()
        if self._engine is not None:
            self._engine.dispose()
        if self._async_engine is not None:
            self._async_engine.sync_engine.dispose()
        self._clear_state()

    async def aclose(self) -> None:
        """Dispose owned engines from an async caller and drop configuration.

        The call is safe to repeat. Afterwards :meth:`init` can configure the
        manager again.
        """

        await self._aclose_sessions()
        if self._engine is not None:
            self._engine.dispose()
        if self._async_engine is not None:
            await self._async_engine.dispose()
        self._clear_state()

    @property
    def engine(self) -> Engine:
        """Return the configured SQLAlchemy engine."""

        if self._engine is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.engine.")
        return self._engine

    @property
    def aengine(self) -> AsyncEngine:
        """Return the async engine built from the configured settings."""

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
        """Build the async engine on first use and return its session factory."""

        if self._settings is None or self._session_factory is None:
            raise DatabaseNotConfiguredError(
                "Database is not initialized. Call db.init(...) before using async sessions."
            )
        if self._async_session_factory is None or self._async_engine is None:
            self._async_engine = build_async_engine(self._settings)
            self._async_session_factory = AsyncSessionFactory(build_async_session_local(self._async_engine))
        return self._async_session_factory

    def _close_sessions(self) -> None:
        """Close context-local sessions without disposing engines."""

        if self._session_factory is not None:
            self._session_factory.clear_session()
        if self._async_session_factory is not None:
            self._async_session_factory.clear_session()

    async def _aclose_sessions(self) -> None:
        """Close context-local sessions, awaiting the async session close."""

        if self._session_factory is not None:
            self._session_factory.clear_session()
        if self._async_session_factory is not None:
            await self._async_session_factory.aclear_session()

    def _clear_state(self) -> None:
        """Forget engines and settings so the manager can be initialized again."""

        self._engine = None
        self._session_factory = None
        self._async_engine = None
        self._async_session_factory = None
        self._settings = None
        self._database_url = None


# Public singleton mirroring Flask-SQLAlchemy style usage.
db = DatabaseManager()
