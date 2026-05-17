"""Bedrock configuration module with optional lazy-initialised settings proxy."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic_settings import BaseSettings

T = TypeVar("T", bound=BaseSettings)


class SettingsProxy:
    """Thread-safe lazy proxy that defers ``BaseSettings`` instantiation until first access.

    Use this to wrap module-level settings singletons so that environment
    variables are **not** read at import time.  The underlying settings
    object is created on the first attribute access and cached thereafter.

    Example::

        class AppSettings(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="APP_")
            DEBUG: bool = False


        settings: AppSettings = SettingsProxy(AppSettings)  # type: ignore[assignment]

    Args:
        factory: A ``BaseSettings`` subclass (or any callable returning one).
    """

    def __init__(self, factory: type[T] | Callable[[], T]) -> None:
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_wrapped", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def _setup(self) -> None:
        """Instantiate the wrapped settings object if not already done (double-checked locking)."""
        if self._wrapped is None:
            with self._lock:
                if self._wrapped is None:
                    wrapped = self._factory()
                    object.__setattr__(self, "_wrapped", wrapped)

    def __getattr__(self, name: str) -> Any:
        self._setup()
        return getattr(self._wrapped, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in ("_factory", "_wrapped", "_lock"):
            object.__setattr__(self, name, value)
        else:
            self._setup()
            setattr(self._wrapped, name, value)

    def __delattr__(self, name: str) -> None:
        self._setup()
        delattr(self._wrapped, name)

    def __repr__(self) -> str:
        self._setup()
        return repr(self._wrapped)

    def __str__(self) -> str:
        self._setup()
        return str(self._wrapped)

    def __bool__(self) -> bool:
        self._setup()
        return bool(self._wrapped)

    def __eq__(self, other: object) -> bool:
        self._setup()
        return self._wrapped == other

    def __hash__(self) -> int:
        self._setup()
        return hash(self._wrapped)

    @property
    def __class__(self) -> type:
        """Masquerade as the target class for ``isinstance`` checks."""
        if self._wrapped is not None:
            return type(self._wrapped)
        factory = object.__getattribute__(self, "_factory")
        return factory if isinstance(factory, type) else type(factory)

    def __dir__(self) -> list[str]:
        """Return attributes of the wrapped object or the factory class."""
        factory = object.__getattribute__(self, "_factory")
        attrs: set[str] = set(dir(factory))
        if hasattr(factory, "model_fields"):
            attrs.update(factory.model_fields.keys())
        return sorted(attrs)


__all__ = ["SettingsProxy"]
