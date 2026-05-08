"""Bedrock configuration module with lazy-initialised settings."""

from __future__ import annotations

import contextvars
from typing import Any

from pydantic_settings import BaseSettings

_is_lazy_setup: contextvars.ContextVar[bool] = contextvars.ContextVar("_is_lazy_setup", default=False)


class _LazySettingsProxy:
    """Internal proxy that defers ``BaseSettings`` instantiation until first access."""

    _wrapped: BaseSettings | None

    def __init__(
        self,
        target_cls: type[BaseSettings],
        *args: object,
        **kwargs: object,
    ) -> None:
        self._target_cls: type[BaseSettings] = target_cls
        self._args: tuple[object, ...] = args
        self._kwargs: dict[str, object] = kwargs
        self._wrapped = None

    def _setup(self) -> None:
        """Trigger actual instantiation of the underlying settings object."""
        if self._wrapped is None:
            token = _is_lazy_setup.set(True)
            try:
                self._wrapped = self._target_cls(*self._args, **self._kwargs)
            finally:
                _is_lazy_setup.reset(token)

    def __getattr__(self, name: str) -> Any:
        self._setup()
        return getattr(self._wrapped, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in ("_target_cls", "_args", "_kwargs", "_wrapped"):
            super().__setattr__(name, value)
        else:
            self._setup()
            setattr(self._wrapped, name, value)

    @property
    def __class__(self) -> type[BaseSettings]:
        """Masquerade as the target class for ``isinstance`` checks."""
        return self._target_cls

    def __dir__(self) -> list[str]:
        """Return union of target class attributes and Pydantic field names."""
        attrs: set[str] = set(dir(self._target_cls))
        if hasattr(self._target_cls, "model_fields"):
            attrs.update(self._target_cls.model_fields.keys())
        return list(attrs)


class LazySettings(BaseSettings):
    """Bedrock settings that defer validation until first attribute access.

    Usage mirrors ``pydantic_settings.BaseSettings``, but instantiation is
    lazy: calling the constructor returns a :class:`_LazySettingsProxy`
    instead, so no environment variables are read until a setting is
    actually accessed.
    """

    def __new__(cls, *args: object, **kwargs: object) -> BaseSettings | _LazySettingsProxy:
        if _is_lazy_setup.get():
            return super().__new__(cls)

        return _LazySettingsProxy(cls, *args, **kwargs)


class BedrockSettings(LazySettings):
    pass


__all__ = ["BedrockSettings", "LazySettings"]
