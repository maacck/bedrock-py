"""ContextVar-backed scope management for SCOPED lifetime services."""

import contextvars
from typing import Any


class ScopeManager:
    """Manages the ContextVar-backed scope for SCOPED services.

    Each scope is a simple dictionary that acts as a frame. Nested scopes
    are supported by pushing/popping via ``contextvars.Token``.
    """

    def __init__(self) -> None:
        self._scope_stack: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
            "bedrock_di_scope",
            default=None,
        )

    def enter(self, name: str) -> contextvars.Token:
        """Enter a new scope.

        Args:
            name: Human-readable name for this scope.

        Returns:
            A ``contextvars.Token`` that can be used to restore the previous scope.
        """
        new_frame: dict[str, Any] = {"__scope_name__": name}
        return self._scope_stack.set(new_frame)

    def exit(self, token: contextvars.Token) -> dict[str, Any]:
        """Exit the current scope, restoring the previous one.

        Args:
            token: The token returned by :meth:`enter`.

        Returns:
            The exited scope frame (useful for cleanup).
        """
        frame = self._scope_stack.get() or {}
        self._scope_stack.reset(token)
        return frame

    def get(self, key: str) -> Any | None:
        """Get a value from the current scope frame.

        Args:
            key: The key to look up.

        Returns:
            The value, or ``None`` if no scope is active or key is missing.
        """
        frame = self._scope_stack.get()
        if frame is None:
            return None
        return frame.get(key)

    def set(self, key: str, value: Any) -> None:
        """Set a value in the current scope frame.

        Args:
            key: The key to set.
            value: The value to store.

        Raises:
            RuntimeError: If no scope is currently active.
        """
        frame = self._scope_stack.get()
        if frame is None:
            raise RuntimeError("No active scope")
        frame[key] = value

    @property
    def active(self) -> bool:
        """Whether a scope is currently active.

        Returns:
            ``True`` if a scope frame exists in the current context.
        """
        return self._scope_stack.get() is not None
