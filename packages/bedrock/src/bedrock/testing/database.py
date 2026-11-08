"""Transaction-isolated database sessions for tests."""

from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from ..database.manager import DatabaseManager, DatabaseNotConfiguredError
from ..database.session_factory import (
    AsyncSessionFactory,
    SessionFactory,
    _current_async_db_session,
    _current_db_session,
)

_NOT_INITIALIZED = (
    "Database is not initialized. Call db.init(...) in a session-scoped fixture "
    "before using db_session / rollback_session()."
)


def _default_db() -> DatabaseManager:
    """Return the global ``db``, imported on call rather than at plugin import."""
    from ..database import db

    return db


def _emit_begin(connection: Connection) -> None:
    """Start the SQLite transaction explicitly so savepoints roll back with it."""
    connection.exec_driver_sql("BEGIN")


def _sqlite_isolation(connection: Connection) -> str | None:
    """Put this SQLite connection in driver autocommit mode and return its previous isolation level."""
    raw = connection.connection.dbapi_connection
    previous = raw.isolation_level
    raw.isolation_level = None
    return previous


def _restore_sqlite_isolation(connection: Connection, previous: str | None) -> None:
    """Give the pooled SQLite connection back its original isolation level."""
    connection.connection.dbapi_connection.isolation_level = previous


@contextmanager
def rollback_session(manager: DatabaseManager | None = None) -> Iterator[Session]:
    """Yield an unbound session whose database writes are rolled back on exit.

    Args:
        manager: Initialized manager to isolate; defaults to the global database.

    Yields:
        A session bound to the isolated connection.

    Raises:
        DatabaseNotConfiguredError: If the manager has not been initialized.
    """
    if manager is None:
        manager = _default_db()
    if manager._session_factory is None:
        raise DatabaseNotConfiguredError(_NOT_INITIALIZED)
    with ExitStack() as stack:
        connection = manager.engine.connect()
        stack.callback(connection.close)
        if connection.dialect.name == "sqlite":
            previous = _sqlite_isolation(connection)
            stack.callback(_restore_sqlite_isolation, connection, previous)
            event.listen(connection, "begin", _emit_begin)
        outer = connection.begin()
        stack.callback(outer.rollback)
        original = manager._session_factory
        factory = SessionFactory(
            sessionmaker(bind=connection, autoflush=False, join_transaction_mode="create_savepoint")
        )
        manager._session_factory = factory
        stack.callback(setattr, manager, "_session_factory", original)
        _current_db_session.set(None)
        stack.callback(factory.clear_session)
        session = factory()
        stack.callback(session.close)
        yield session


@asynccontextmanager
async def arollback_session(manager: DatabaseManager | None = None) -> AsyncIterator[AsyncSession]:
    """Yield an unbound async session whose writes are rolled back on exit.

    Args:
        manager: Initialized manager to isolate; defaults to the global database.

    Yields:
        An async session bound to the isolated connection.

    Raises:
        DatabaseNotConfiguredError: If the manager has not been initialized.
    """
    if manager is None:
        manager = _default_db()
    if manager._settings is None:
        raise DatabaseNotConfiguredError(_NOT_INITIALIZED)
    engine = manager.aengine
    async with AsyncExitStack() as stack:
        connection = await stack.enter_async_context(engine.connect())
        if connection.dialect.name == "sqlite":
            previous = await connection.run_sync(_sqlite_isolation)
            stack.push_async_callback(connection.run_sync, _restore_sqlite_isolation, previous)
            event.listen(connection.sync_connection, "begin", _emit_begin)
        outer = await connection.begin()
        stack.push_async_callback(outer.rollback)
        original = manager._async_session_factory
        session_local = async_sessionmaker(bind=connection, autoflush=False, join_transaction_mode="create_savepoint")
        factory = AsyncSessionFactory(session_local)
        manager._async_session_factory = factory
        stack.callback(setattr, manager, "_async_session_factory", original)
        _current_async_db_session.set(None)
        stack.push_async_callback(factory.aclear_session)
        session = session_local()
        stack.push_async_callback(session.close)
        yield session


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Provide an unbound session isolated by a rolled-back transaction."""
    with rollback_session() as session:
        yield session
