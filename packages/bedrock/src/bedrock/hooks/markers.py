"""Marker decorators for hook specifications and implementations."""

from collections.abc import Callable
from typing import Any


def hookspec(fn: Callable[..., Any] | None = None, *, firstresult: bool = False) -> Any:
    """Mark a function as a hook specification.

    Can be used as a bare decorator or with arguments::

        @hookspec
        def my_hook(self, request):
            pass


        @hookspec(firstresult=True)
        def my_hook(self, request):
            pass

    Args:
        fn: The function to decorate. When ``None``, returns a decorator factory.
        firstresult: If ``True``, dispatch stops after the first non-None result.

    Returns:
        The decorated function, or a decorator factory when ``fn`` is ``None``.
    """
    if fn is not None:
        fn._hookspec = {"firstresult": firstresult}
        return fn

    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        f._hookspec = {"firstresult": firstresult}
        return f

    return decorator


def hookimpl(fn: Callable[..., Any] | None = None, *, priority: int = 0) -> Any:
    """Mark a function as a hook implementation.

    Can be used as a bare decorator or with arguments::

        @hookimpl
        def my_hook(self, request):
            pass


        @hookimpl(priority=10)
        def my_hook(self, request):
            pass

    Args:
        fn: The function to decorate. When ``None``, returns a decorator factory.
        priority: Execution priority. Lower values run first.

    Returns:
        The decorated function, or a decorator factory when ``fn`` is ``None``.
    """
    if fn is not None:
        fn._hookimpl = {"priority": priority}
        return fn

    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        f._hookimpl = {"priority": priority}
        return f

    return decorator
