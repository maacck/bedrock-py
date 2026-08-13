from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from ..exc import ImproperlyConfigured
from .config import DbSettings
from .connection import build_session_local_from_settings
from .session_factory import SessionFactory


class DatabaseNotConfiguredError(ImproperlyConfigured):
    """Raised when database access is attempted before initialization."""

    detail: str = "Database is not configured. Call db.init() first."


class DatabaseManager:
    """Own database initialization state and expose lazy session access."""

    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._session_factory: SessionFactory | None = None
        self._settings: DbSettings | None = None

    def init(self, url: str | None = None, settings: DbSettings | None = None) -> None:
        """Initialize or safely replace the database engine and session factory.

        Replacement is supported. The new engine is constructed before the
        existing engine is disposed, so a failed initialization leaves the
        current database configuration usable.
        """

        resolved_settings = settings or DbSettings()
        resolved_url = url or resolved_settings.SQLALCHEMY_DATABASE_URI
        engine, session_local = build_session_local_from_settings(
            settings=resolved_settings,
            database_url=resolved_url,
        )

        previous_engine = self._engine
        previous_session_factory = self._session_factory
        if previous_session_factory is not None:
            previous_session_factory.clear_session()
        if previous_engine is not None:
            previous_engine.dispose()

        self._engine = engine
        self._session_factory = SessionFactory(session_local)
        self._settings = resolved_settings
        self._database_url = resolved_url

    @property
    def engine(self) -> Engine:
        """Return the configured SQLAlchemy engine."""

        if self._engine is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.engine.")
        return self._engine

    @property
    def session(self) -> Session:
        """Return the current context-local SQLAlchemy session."""

        if self._session_factory is None:
            raise DatabaseNotConfiguredError("Database is not initialized. Call db.init(...) before using db.session.")
        return self._session_factory.session

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


# Public singleton mirroring Flask-SQLAlchemy style usage.
db = DatabaseManager()
