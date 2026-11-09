from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..exc import BedrockExc

_current_db_session: ContextVar[Session | None] = ContextVar("_current_db_session", default=None)
_current_async_db_session: ContextVar[AsyncSession | None] = ContextVar("_current_async_db_session", default=None)


class SessionNotBoundError(BedrockExc):
    """Raised when ``db.session`` or ``db.asession`` is read while no session is bound to the context."""

    detail: str = (
        "No database session is bound to this context. Open one with `with db.session_scope():` or "
        "`@db.session_scope()` at the entry point (script, CLI command, worker, WebSocket handler, startup hook), "
        "or use the integration middleware or task wrapper that binds one per request or task."
    )


_ASYNC_NOT_BOUND = (
    "No async database session is bound to this context. Open one with `async with db.asession_scope():` or "
    "`@db.asession_scope()` at the entry point, or use the integration middleware or task wrapper that binds "
    "one per request or task."
)


class SessionAlreadyBoundError(BedrockExc, RuntimeError):
    """Raised when ``session_scope()`` or ``asession_scope()`` opens while a session is already bound.

    Subclasses ``RuntimeError`` so code that caught the previous ``RuntimeError`` keeps working.
    """

    detail: str = (
        "session_scope() called with an active session already bound to this context. "
        "Use independent_session() for side transactions, or call clear_session() first."
    )


_ASYNC_ALREADY_BOUND = (
    "asession_scope() called with an active async session already bound to this context. "
    "Use aindependent_session() for side transactions, or clear the async session first."
)


class SessionFactory:
    """Provide context-local SQLAlchemy session access."""

    def __init__(self, session_local: Callable[[], Session]) -> None:
        self._session_local = session_local

    @property
    def session(self) -> Session:
        """Return the session bound to the current context.

        A session is bound by :meth:`session_scope`, :meth:`set_session`, or an integration that opens a
        scope per request or task. Reading it creates nothing, so a session can never outlive the scope
        that opened it.

        Raises:
            SessionNotBoundError: If no session is bound to the current context.
        """
        session = _current_db_session.get()
        if session is None:
            raise SessionNotBoundError()
        return session

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Provide a transactional scope bound to current context.

        Creates a new session, binds it to the current execution context,
        and guarantees commit/rollback/close/unbind on exit.

        Raises:
            SessionAlreadyBoundError: If a session is already bound to the current context.
        """
        if _current_db_session.get() is not None:
            raise SessionAlreadyBoundError()
        session = self._session_local()
        token = _current_db_session.set(session)
        try:
            yield session
            session.commit()
        except BaseException:
            try:
                session.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                session.close()
            except Exception:
                pass
            _current_db_session.reset(token)

    @contextmanager
    def independent_session(self) -> Generator[Session, None, None]:
        """Provide an independent transactional scope, isolated from current context.

        Creates a new session with its own transaction. Does NOT bind to the
        execution context (db.session is unaffected). Use for audit logs, event
        publishing, or reads that must not see uncommitted data from the caller.
        """
        session = self._session_local()
        try:
            yield session
            session.commit()
        except BaseException:
            try:
                session.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                session.close()
            except Exception:
                pass

    def set_session(self, session: Session) -> None:
        """Bind an existing session to the current context."""
        _current_db_session.set(session)

    def clear_session(self) -> None:
        """Close and unbind the current context-local session."""
        session = _current_db_session.get()
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        _current_db_session.set(None)

    def __call__(self) -> Session:
        """Return a new session from the factory without binding it to the context."""
        return self._session_local()


class AsyncSessionFactory:
    """Provide context-local SQLAlchemy async session access.

    The async session lives in its own :class:`~contextvars.ContextVar`, so a
    sync session bound in the same task is left untouched.
    """

    def __init__(self, session_local: Callable[[], AsyncSession]) -> None:
        self._session_local = session_local

    @property
    def session(self) -> AsyncSession:
        """Return the async session bound to the current context.

        Raises:
            SessionNotBoundError: If no async session is bound to the current context.
        """

        session = _current_async_db_session.get()
        if session is None:
            raise SessionNotBoundError(_ASYNC_NOT_BOUND)
        return session

    @asynccontextmanager
    async def asession_scope(self) -> AsyncIterator[AsyncSession]:
        """Provide a transactional async scope bound to the current context.

        Creates a new session, binds it to the current execution context, and
        guarantees commit, rollback, close, and unbind on exit.

        Raises:
            SessionAlreadyBoundError: If an async session is already bound to this context.
        """

        if _current_async_db_session.get() is not None:
            raise SessionAlreadyBoundError(_ASYNC_ALREADY_BOUND)
        session = self._session_local()
        token = _current_async_db_session.set(session)
        try:
            yield session
            await session.commit()
        except BaseException:
            try:
                await session.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                await session.close()
            except Exception:
                pass
            finally:
                _current_async_db_session.reset(token)

    @asynccontextmanager
    async def aindependent_session(self) -> AsyncIterator[AsyncSession]:
        """Provide an independent async transaction, isolated from the current context.

        The session is not bound to ``asession``. Commit and rollback follow the
        same rules as :meth:`asession_scope`.
        """

        session = self._session_local()
        try:
            yield session
            await session.commit()
        except BaseException:
            try:
                await session.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                await session.close()
            except Exception:
                pass

    def clear_session(self) -> None:
        """Close and unbind the current context-local async session.

        Uses the wrapped synchronous session so this can run from a sync
        shutdown path. Prefer :meth:`aclear_session` when a loop is running.
        """

        session = _current_async_db_session.get()
        if session is not None:
            try:
                session.sync_session.close()
            except Exception:
                pass
        _current_async_db_session.set(None)

    async def aclear_session(self) -> None:
        """Close and unbind the current context-local async session."""

        session = _current_async_db_session.get()
        try:
            if session is not None:
                await session.close()
        except Exception:
            pass
        finally:
            _current_async_db_session.set(None)
