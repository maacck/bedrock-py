"""The process-wide event loop that synchronous code uses to run asynchronous work.

Synchronous ``bedrock.setup()`` runs asynchronous lifecycle hooks on this loop, ``bedrock.run_async()`` runs
application coroutines on it, and synchronous ``bedrock.shutdown()`` releases the async database engine on it and
then closes it. Because all of them share one loop, an async client opened by an ``on_load`` hook is usable from
``run_async()`` and is closed by ``on_shutdown`` on the loop that opened it. ``asyncio.run`` per call would strand
such clients on a closed loop.

The loop has no thread of its own: the calling thread drives it with ``run_until_complete`` and blocks until the
awaitable finishes. One call drives it at a time; calls from other threads wait their turn.
"""

import asyncio
import threading
import weakref
from collections.abc import Awaitable
from contextvars import ContextVar
from typing import Any


def loop_is_running() -> bool:
    """Return whether the calling thread is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def discard_awaitable(awaitable: Any) -> None:
    """Release an awaitable that will not be awaited so no coroutine is left unawaited."""
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
        return
    cancel = getattr(awaitable, "cancel", None)
    if callable(cancel):
        cancel()


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel leftover tasks, finalize async generators and the default executor, then close ``loop``."""
    if loop.is_closed():
        return
    if loop_is_running():
        loop.close()
        return
    try:
        leftover = asyncio.all_tasks(loop)
        for task in leftover:
            task.cancel()
        if leftover:
            loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()


_driving: ContextVar[bool] = ContextVar("bedrock_runtime_loop_driving", default=False)
"""Set inside work running on the runtime loop; ``to_thread`` workers it starts inherit it."""


async def _await[T](awaitable: Awaitable[T]) -> T:
    _driving.set(True)
    return await awaitable


def inside_runtime_loop() -> bool:
    """Return whether the caller is work running on the runtime loop, or a ``to_thread`` worker that work started."""
    return _driving.get()


class RuntimeLoop:
    """The persistent event loop shared by synchronous lifecycle entrypoints and ``run_async()``.

    Created when the first awaitable has to run; closed by :meth:`close` (``bedrock.shutdown()``) or, failing
    that, when the interpreter exits.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._finalizer: weakref.finalize | None = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        """Return whether the loop currently exists."""
        loop = self._loop
        return loop is not None and not loop.is_closed()

    def run[T](self, awaitable: Awaitable[T]) -> T:
        """Run ``awaitable`` to completion on the loop, creating the loop first if needed.

        The caller must not be running an event loop; callers check that and raise their own error first.

        Args:
            awaitable: Anything awaitable, such as a coroutine.

        Returns:
            The awaitable's result.

        Raises:
            RuntimeError: When called from a thread that work on this loop started and is waiting for, which
                would otherwise deadlock. The awaitable is discarded.
        """
        if _driving.get():
            discard_awaitable(awaitable)
            raise RuntimeError(
                "Cannot run async work on the Bedrock runtime loop from a thread that work on that loop is waiting "
                "for (for example a function passed to asyncio.to_thread() by a coroutine running there). Await "
                "the coroutine in that coroutine instead."
            )
        with self._lock:
            loop = self._loop
            if loop is None or loop.is_closed():
                loop = self._loop = asyncio.new_event_loop()
                self._finalizer = weakref.finalize(self, _close_loop, loop)
            return loop.run_until_complete(_await(awaitable))

    def close(self) -> None:
        """Cancel work left on the loop and close it; the next :meth:`run` creates a new one.

        Waits for a call running on the loop in another thread to finish. Must not be called from a thread that
        is running another event loop.
        """
        with self._lock:
            loop, self._loop = self._loop, None
            finalizer, self._finalizer = self._finalizer, None
            if finalizer is not None:
                finalizer.detach()
            if loop is not None:
                _close_loop(loop)


runtime_loop = RuntimeLoop()
