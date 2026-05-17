"""Decorators for DI container integration."""

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, overload

from .lifetime import Lifetime

if TYPE_CHECKING:
    from .container import Container

_LIFETIME_DEFAULT = object()


def build_provider(container: "Container") -> Callable[..., Any]:
    """Build a ``@provider`` decorator bound to a specific container.

    Args:
        container: The container that should receive registrations.

    Returns:
        A decorator function compatible with the module-level ``provider`` API.
    """

    @overload
    def provider[T](cls: type[T]) -> type[T]: ...

    @overload
    def provider[T](
        key: type | str | None = None,
        *,
        lifetime: Lifetime = Lifetime.SINGLETON,
    ) -> Callable[[type[T]], type[T]]: ...

    def provider[T](
        key: type | str | None = None,
        *,
        lifetime: type | Lifetime = _LIFETIME_DEFAULT,
    ) -> type[T] | Callable[[type[T]], type[T]]:
        """Class decorator that registers the class in the bound container.

        Supports the same public forms as the top-level ``provider`` alias.

        Args:
            key: The registration key (type or string). Defaults to the class itself.
            lifetime: Service lifetime (default: SINGLETON).

        Returns:
            The class unchanged, or a decorator that returns the class unchanged.
        """
        actual_lifetime: Lifetime = lifetime if lifetime is not _LIFETIME_DEFAULT else Lifetime.SINGLETON

        def _do_register(cls: type[T], resolved_key: type | str, lt: Lifetime) -> type[T]:
            container.register(resolved_key, factory=cls, lifetime=lt)
            return cls

        if key is not None and isinstance(key, type):
            if lifetime is _LIFETIME_DEFAULT:
                return _do_register(key, key, actual_lifetime)

            def _decorator_with_key(cls: type[T]) -> type[T]:
                return _do_register(cls, key, actual_lifetime)

            return _decorator_with_key

        def _decorator(cls: type[T]) -> type[T]:
            resolved_key = key if key is not None else cls
            return _do_register(cls, resolved_key, actual_lifetime)

        return _decorator

    return provider


def build_inject(container: "Container") -> Callable[..., Any]:
    """Build an ``@inject`` decorator bound to a specific container.

    Args:
        container: The container used to resolve service mappings.

    Returns:
        A decorator factory compatible with the module-level ``inject`` API.
    """

    def inject(**mappings: type | str) -> Callable[..., Any]:
        """Function/method decorator that resolves dependencies from the bound container.

        Explicitly provided kwargs take precedence over injected ones.

        Args:
            **mappings: Map of parameter name -> service key.

        Returns:
            A decorator that injects resolved services as keyword arguments.
        """

        def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(fn)
            def _wrapper(*args: Any, **kwargs: Any) -> Any:
                for param_name, service_key in mappings.items():
                    if param_name not in kwargs:
                        kwargs[param_name] = container.resolve(service_key)
                return fn(*args, **kwargs)

            return _wrapper

        return _decorator

    return inject


def provider(*args: Any, **kwargs: Any) -> Any:
    """Register providers against the default global container.

    This compatibility wrapper preserves the historical import path:
    ``from bedrock.di.decorators import provider``.
    """
    from . import container

    return container.provider(*args, **kwargs)


def inject(**mappings: type | str) -> Callable[..., Any]:
    """Inject dependencies from the default global container.

    This compatibility wrapper preserves the historical import path:
    ``from bedrock.di.decorators import inject``.
    """
    from . import container

    return container.inject(**mappings)
