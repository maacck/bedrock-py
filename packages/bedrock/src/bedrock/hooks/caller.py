"""Hook dispatch logic for sync, async, and robust calling."""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


def dispatch_call(
    impls: list[Any],
    kwargs: dict[str, Any],
    firstresult: bool = False,
) -> list[Any]:
    """Dispatch a hook call synchronously.

    Iterates through implementations in priority order and calls each one.

    Args:
        impls: Ordered list of :class:`HookImpl` instances.
        kwargs: Keyword arguments to pass to each implementation.
        firstresult: If ``True``, stop after the first non-None result.

    Returns:
        List of return values from implementations.
    """
    results: list[Any] = []
    for impl in impls:
        result = impl.fn(**kwargs)
        if firstresult:
            if result is not None:
                results.append(result)
                break
        else:
            results.append(result)
    return results


async def dispatch_acall(
    impls: list[Any],
    kwargs: dict[str, Any],
    firstresult: bool = False,
) -> list[Any]:
    """Dispatch a hook call asynchronously.

    Synchronous implementations are wrapped via :func:`asyncio.to_thread`.
    Async implementations are awaited directly.

    Args:
        impls: Ordered list of :class:`HookImpl` instances.
        kwargs: Keyword arguments to pass to each implementation.
        firstresult: If ``True``, stop after the first non-None result.

    Returns:
        List of return values from implementations.
    """
    results: list[Any] = []
    for impl in impls:
        if inspect.iscoroutinefunction(impl.fn):
            result = await impl.fn(**kwargs)
        else:
            result = await asyncio.to_thread(impl.fn, **kwargs)
        if firstresult:
            if result is not None:
                results.append(result)
                break
        else:
            results.append(result)
    return results


def dispatch_call_robust(
    impls: list[Any],
    kwargs: dict[str, Any],
    firstresult: bool = False,
) -> list[tuple[Callable[..., Any], Any | Exception]]:
    """Dispatch a hook call synchronously, catching exceptions per-implementation.

    Each implementation is called in priority order. If an implementation raises
    an :class:`Exception`, it is captured and returned as part of the result
    rather than propagated.

    Args:
        impls: Ordered list of :class:`HookImpl` instances.
        kwargs: Keyword arguments to pass to each implementation.
        firstresult: If ``True``, stop after the first non-None result.

    Returns:
        List of ``(callable, result_or_exception)`` tuples.
    """
    results: list[tuple[Callable[..., Any], Any | Exception]] = []
    for impl in impls:
        try:
            result = impl.fn(**kwargs)
            if firstresult:
                if result is not None:
                    results.append((impl.fn, result))
                    break
            else:
                results.append((impl.fn, result))
        except Exception as exc:
            results.append((impl.fn, exc))
    return results
