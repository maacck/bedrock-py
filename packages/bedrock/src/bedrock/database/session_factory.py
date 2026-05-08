from collections.abc import Callable
from contextvars import ContextVar

from sqlalchemy.orm import Session

_current_db_session: ContextVar[Session | None] = ContextVar("_current_db_session", default=None)


class SessionFactory:
    """Provide context-local SQLAlchemy session access."""

    def __init__(self, session_local: Callable[[], Session]) -> None:
        self.session_local = session_local

    @property
    def session(self) -> Session:
        """Return the current context-local session, creating one on first access."""

        session = _current_db_session.get()
        if session is None:
            session = self.session_local()
            _current_db_session.set(session)
        return session

    def set_session(self, session: Session) -> None:
        """Bind an existing session to the current context."""

        _current_db_session.set(session)

    def clear_session(self) -> None:
        """Clear the current context-local session binding."""

        _current_db_session.set(None)

    def __call__(self) -> Session:
        """Return New session from the factory, bypassing context-local caching."""

        return self.session_local()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Clear the context-local session when exiting a context manager block."""

        self.clear_session()
