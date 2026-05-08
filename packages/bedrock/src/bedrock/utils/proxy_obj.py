"""Proxy object utilities for lazy-loading configuration objects."""

from __future__ import annotations

from typing import TypeVar

from bedrock.utils.lazyload import load_callable

T = TypeVar("T")


class Proxy[T]:
    """Lazy-loading proxy that defers instantiation of the wrapped object.

    Inspired by Django's ``LazyObject`` pattern.  A callable (typically a
    Pydantic settings class) is stored at construction time; actual
    instantiation happens on first attribute access.

    TypeVar ``T`` should be bound to the concrete settings class so that
    attribute hints propagate correctly.
    """

    _factory: type[T] | callable[[], T]
    _wrapped: T | None

    def __init__(self, factory: type[T]) -> None:
        self._factory = factory
        self._wrapped = None

    def _setup(self) -> None:
        """Instantiate the wrapped object if not already done."""
        if self._wrapped is None:
            self._wrapped = self._factory()

    def __getattr__(self, name: str) -> T | None:
        """Delegated attribute access after lazy instantiation."""
        if name in ("_factory", "_wrapped", "_setup"):
            return super().__getattribute__(name)  # type: ignore[return-value]

        self._setup()
        return getattr(self._wrapped, name)  # type: ignore[return-value]

    def __setattr__(self, name: str, value: object) -> None:
        """Delegated attribute assignment after lazy instantiation."""
        if name in ("_factory", "_wrapped"):
            super().__setattr__(name, value)
        else:
            self._setup()
            setattr(self._wrapped, name, value)

    def __dir__(self) -> list[str]:
        """Expose the wrapped object's attributes for tab-completion."""
        self._setup()
        return dir(self._wrapped)


class ProxyCallable:
    """Variant of Proxy that allows the factory to be any callable, not just a class."""

    def __init__(self, ref: str):
        self._ref = ref

    def __call__(self, *args, **kwargs):
        return load_callable(ref=self._ref)(*args, **kwargs)
