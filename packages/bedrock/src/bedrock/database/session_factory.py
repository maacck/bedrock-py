from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.orm import Session

_current_db_session: ContextVar[Session | None] = ContextVar("_current_db_session", default=None)


class SessionFactory:
    """Provide context-local SQLAlchemy session access."""

    def __init__(self, session_local: Callable[[], Session]) -> None:
        self._session_local = session_local

    @property
    def session(self) -> Session:
        """Return the current context-local session, creating one on first access."""
        session = _current_db_session.get()
        if session is None:
            session = self._session_local()
            _current_db_session.set(session)
        return session

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Provide a transactional scope bound to current context.

        Creates a new session, binds it to the current execution context,
        and guarantees commit/rollback/close/unbind on exit.

        Raises:
            RuntimeError: If a session is already bound to the current context.
        """
        if _current_db_session.get() is not None:
            raise RuntimeError(
                "session_scope() called with an active session already bound to this context. "
                "Use independent_session() for side transactions, or call clear_session() first."
            )
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
        """Return a new session from the factory, bypassing context-local caching."""
        return self._session_local()

    def __enter__(self) -> "SessionFactory":
        """Enter context manager block."""
        return self

    def __exit__(self, *args: object) -> None:
        """Close and unbind session when exiting context manager block."""
        self.clear_session()
