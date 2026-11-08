"""Execution machinery shared by the synchronous and asynchronous lifecycle entrypoints of ``ModuleRegistry``.

Private to ``bedrock.module``. A registry operation is written once, as a generator that yields *effects* (call a
hook, send a signal, run a cleanup step). A sync driver and an async driver execute those effects, so the state
machine, rollback, idempotency and aggregate-error behavior exist in exactly one place.

This module also owns the operation gate that admits one registry operation at a time; it must not depend on a
thread lock being held across an ``await``. The event loop that synchronous entrypoints use to run asynchronous hooks
is process-wide and lives in ``bedrock._loop``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .exc import ModuleLifecycleError

T = TypeVar("T")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HookEffect:
    """Call the ``hook_name`` bootstrap hook of ``app`` if the module defines it.

    ``protected`` effects are cleanup work that must run to completion: the async driver does not let a cancellation
    of the caller interrupt them and delivers it once the whole protected section has finished.
    """

    app: Any
    hook_name: str
    protected: bool = False


@dataclass(frozen=True)
class SignalEffect:
    """Send ``signal`` with ``sender`` and ``kwargs`` to its receivers; ``protected`` as for ``HookEffect``."""

    signal: Any
    sender: Any
    kwargs: dict[str, Any]
    protected: bool = False


@dataclass(frozen=True)
class CallEffect:
    """Run a synchronous cleanup callable inline, in both modes."""

    fn: Callable[[], Any]


@dataclass(frozen=True)
class AwaitEffect:
    """Run the awaitable produced by ``factory``; the factory is only called once running it is possible."""

    factory: Callable[[], Awaitable[Any]]


def is_protected(effect: Any) -> bool:
    """Return whether ``effect`` is cleanup work that a caller cancellation must not interrupt."""
    return bool(getattr(effect, "protected", False))


Effect = HookEffect | SignalEffect | CallEffect | AwaitEffect
Steps = Generator[Effect, Any, T]


async def drain(future: asyncio.Future[Any]) -> asyncio.CancelledError | None:
    """Wait until ``future`` is done however often this task is cancelled; return the first cancellation seen."""
    cancelled: asyncio.CancelledError | None = None
    while not future.done():
        try:
            await asyncio.wait({future})
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    return cancelled


async def _dispose_unawaited(result: Any) -> None:
    """Release an awaitable produced by a worker whose caller was cancelled, so nothing is left unmanaged."""
    if asyncio.iscoroutine(result):
        result.close()
        return
    if not inspect.isawaitable(result):
        return
    future = asyncio.ensure_future(result)
    future.cancel()
    await drain(future)
    if not future.cancelled():
        future.exception()


async def run_in_worker[**P, R](fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``fn`` in a worker thread and never abandon it while the calling task is cancelled.

    A running thread cannot be stopped. Plain ``await asyncio.to_thread(...)`` lets a cancelled caller move on while
    the thread keeps running, so the caller's cleanup, the operation gate and a retry could overlap its side effects.
    Here a cancellation request is remembered and the call keeps waiting until the thread has finished, however often
    it is cancelled. Only then is the cancellation raised. If the thread returned an awaitable, that awaitable is
    released instead of being returned, because its caller will not await it. A failure of the thread is logged and
    the cancellation wins.

    Args:
        fn: Synchronous callable to run in the worker thread.
        *args: Positional arguments for ``fn``.
        **kwargs: Keyword arguments for ``fn``.

    Returns:
        The return value of ``fn``.

    Raises:
        asyncio.CancelledError: If the calling task was cancelled, after the worker finished.
        Exception: Whatever ``fn`` raised, when the calling task was not cancelled.
    """
    worker = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = await drain(worker)
    if cancelled is None:
        return worker.result()
    if worker.cancelled():
        raise cancelled
    failure = worker.exception()
    if failure is not None:
        logger.warning(
            "Worker %r failed after its caller was cancelled: %r",
            getattr(fn, "__qualname__", fn),
            failure,
            exc_info=failure,
        )
    else:
        await _dispose_unawaited(worker.result())
    raise cancelled


@dataclass(eq=False)
class _Operation:
    """One top-level registry operation and the stack of nested operation names inside it."""

    gate: OperationGate
    stack: list[str]
    thread_id: int
    active: bool = True
    installs: list[Lease] = field(default_factory=list)
    """Leases of the install calls in progress, outermost first. Only the innermost may start a nested install."""


_ACTIVE_LEASES: ContextVar[tuple[Lease, ...]] = ContextVar("bedrock_active_registry_leases", default=())
"""Leases of the operations, and nested installs, the current context runs inside.

Hooks and receivers run inside the context of the operation that called them, including child tasks they spawn and
worker threads started with ``asyncio.to_thread``, so a call back into the registry from there is recognized as
re-entrant. Unrelated tasks and threads have no entry and wait their turn instead. The innermost lease tells which
install a context runs inside, which separates a genuinely nested install from a concurrent sibling.
"""


@dataclass(eq=False)
class Lease:
    """Proof that the caller may run an operation; pass it back to :meth:`OperationGate.leave`."""

    operation: _Operation | None
    nested: bool = False
    token: Token[tuple[Lease, ...]] | None = None

    @property
    def outermost(self) -> bool:
        """Return whether this lease started a new top-level operation."""
        return self.operation is not None and not self.nested


class OperationGate:
    """Admit one registry operation at a time without holding a lock across awaits.

    The gate state is guarded by a short-lived ``threading.Condition`` that is never held while a hook runs. A
    caller that already belongs to the running operation (same context) is re-entrant: only ``install`` may nest
    inside ``install``, and only from the innermost running install, so concurrent sibling installs are refused;
    anything else raises ``ModuleLifecycleError``. Any other caller waits: a thread blocks, a task awaits. A
    synchronous caller on the thread that owns the running operation raises instead of waiting, because waiting
    there would deadlock.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._current: _Operation | None = None
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    def entrypoint(self) -> str | None:
        """Return the top-level operation the current context is running inside, if any."""
        mine = self._mine()
        return mine.operation.stack[0] if mine is not None and mine.operation is not None else None

    def enter(self, name: str, introduces: Callable[[], bool] | None = None) -> Lease:
        """Admit ``name``, blocking the calling thread while another context runs an operation.

        Args:
            name: Operation starting: ``install``, ``populate``, ``mark_ready`` or ``shutdown``.
            introduces: For ``install``, tells whether it would add a module; an install that adds none
                is always allowed because it changes nothing.

        Returns:
            The lease to release with :meth:`leave`.

        Raises:
            ModuleLifecycleError: If ``name`` may not nest in the running operation, or the running operation
                belongs to another context on this same thread.
        """
        with self._cond:
            while True:
                lease = self._admit(name, introduces)
                if lease is not None:
                    break
                owner = self._current
                if owner is not None and owner.thread_id == threading.get_ident():
                    raise ModuleLifecycleError(
                        f"Cannot call {name}() while {owner.stack[0]}() is running in another task or context on "
                        "this thread; waiting would deadlock. Await the async entrypoint instead, or wait for "
                        "the running operation to finish."
                    )
                self._cond.wait()
        self._bind(lease)
        return lease

    async def aenter(self, name: str, introduces: Callable[[], bool] | None = None) -> Lease:
        """Admit ``name``, suspending the calling task while another context runs an operation.

        Args:
            name: Operation starting.
            introduces: See :meth:`enter`.

        Returns:
            The lease to release with :meth:`leave`.

        Raises:
            ModuleLifecycleError: If ``name`` may not nest in the running operation.
        """
        while True:
            waiter: asyncio.Future[None] | None = None
            with self._cond:
                lease = self._admit(name, introduces)
                if lease is None:
                    loop = asyncio.get_running_loop()
                    waiter = loop.create_future()
                    self._waiters.append((loop, waiter))
            if lease is not None:
                break
            assert waiter is not None
            try:
                await waiter
            finally:
                with self._cond:
                    self._waiters = [entry for entry in self._waiters if entry[1] is not waiter]
        self._bind(lease)
        return lease

    def leave(self, lease: Lease) -> None:
        """Release ``lease``; the next waiting thread or task may then start its operation."""
        operation = lease.operation
        if operation is None:
            return
        if lease.token is not None:
            try:
                _ACTIVE_LEASES.reset(lease.token)
            except ValueError:
                pass  # released from another context; the inactive operation is ignored from now on
        if lease.nested:
            operation.stack.pop()
            operation.installs.pop()
            return
        with self._cond:
            operation.active = False
            self._current = None
            self._cond.notify_all()
            waiters = list(self._waiters)
        for loop, future in waiters:
            _wake(loop, future)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Name an internal phase of the running operation, such as the ready phase of ``populate``."""
        mine = self._mine()
        if mine is None or mine.operation is None:
            yield
            return
        stack = mine.operation.stack
        stack.append(name)
        try:
            yield
        finally:
            stack.pop()

    def _mine(self) -> Lease | None:
        """Return the innermost lease of the running operation the current context belongs to."""
        for lease in reversed(_ACTIVE_LEASES.get()):
            operation = lease.operation
            if operation is not None and operation.gate is self and operation.active:
                return lease
        return None

    def _admit(self, name: str, introduces: Callable[[], bool] | None) -> Lease | None:
        """Return a lease, or ``None`` when another context must finish first (call with the condition held)."""
        lease = self._mine()
        if lease is not None and lease.operation is not None:
            mine = lease.operation
            outer = mine.stack[-1]
            if name == "install" and introduces is not None and not introduces():
                return Lease(operation=None)
            if name == "install" and outer == "install":
                if mine.installs[-1] is not lease:
                    raise ModuleLifecycleError(
                        f"Cannot call install() while another install() started inside the same {mine.stack[0]}() "
                        "is still running; a failed install would roll back modules its sibling added. Await each "
                        "install() before starting the next."
                    )
                nested = Lease(operation=mine, nested=True)
                mine.stack.append(name)
                mine.installs.append(nested)
                return nested
            raise ModuleLifecycleError(_reentry_message(name, outer))
        if self._current is not None:
            return None
        operation = _Operation(gate=self, stack=[name], thread_id=threading.get_ident())
        outermost = Lease(operation=operation)
        operation.installs.append(outermost)
        self._current = operation
        return outermost

    @staticmethod
    def _bind(lease: Lease) -> None:
        if lease.operation is not None:
            lease.token = _ACTIVE_LEASES.set((*_ACTIVE_LEASES.get(), lease))


def _wake(loop: asyncio.AbstractEventLoop, future: asyncio.Future[None]) -> None:
    def release() -> None:
        if not future.done():
            future.set_result(None)

    try:
        loop.call_soon_threadsafe(release)
    except RuntimeError:
        pass  # the waiting loop is closed; there is nobody to wake


def _reentry_message(name: str, outer: str) -> str:
    if name == "install":
        advice = "install modules before readiness or shutdown starts, then call mark_ready() again to ready the new modules."
    elif outer == "install":
        advice = "raise an error from the hook to abort the installation and roll it back instead."
    else:
        advice = "lifecycle hooks and signal receivers must not call back into it."
    return f"Cannot call {name}() while {outer}() is running; {advice}"
