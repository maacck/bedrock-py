"""Decorators for DI container integration."""

import functools
from collections.abc import Callable
from typing import Any, overload

from .container import Container
from .lifetime import Lifetime

_DEFAULT_CONTAINER: Container | None = None
_LIFETIME_DEFAULT = object()


def _get_container() -> Container:
    """Lazy-import the global container to avoid circular imports."""
    global _DEFAULT_CONTAINER
    if _DEFAULT_CONTAINER is None:
        from . import container as _container_instance

        _DEFAULT_CONTAINER = _container_instance
    return _DEFAULT_CONTAINER


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
    """Class decorator that registers the class as a service in the global container.

    Supports five usage patterns::

        @provider                           # key=cls, lifetime=SINGLETON
        @provider()                         # key=cls, lifetime=SINGLETON
        @provider(SomeInterface)            # key=SomeInterface, factory=SomeInterface
        @provider(SomeInterface, lifetime=Lifetime.TRANSIENT)
        @provider(lifetime=Lifetime.TRANSIENT)  # key=cls

    Args:
        key: The registration key (type or string). Defaults to the class itself.
        lifetime: Service lifetime (default: SINGLETON).

    Returns:
        The class unchanged, or a decorator that returns the class unchanged.
    """
    actual_lifetime: Lifetime = lifetime if lifetime is not _LIFETIME_DEFAULT else Lifetime.SINGLETON

    def _do_register(cls: type[T], resolved_key: type | str, lt: Lifetime) -> type[T]:
        _get_container().register(resolved_key, factory=cls, lifetime=lt)
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


def inject(**mappings: type | str) -> Callable[..., Any]:
    """Function/method decorator that resolves dependencies from the container.

    Explicitly provided kwargs take precedence over injected ones.

    Args:
        **mappings: Map of parameter name -> service key.

    Returns:
        A decorator that injects resolved services as keyword arguments.

    Example::

        @inject(repo=IUserRepo, cache="cache")
        def create_user(name: str, *, repo, cache): ...
    """

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            ctr = _get_container()
            for param_name, service_key in mappings.items():
                if param_name not in kwargs:
                    kwargs[param_name] = ctr.resolve(service_key)
            return fn(*args, **kwargs)

        return _wrapper

    return _decorator
