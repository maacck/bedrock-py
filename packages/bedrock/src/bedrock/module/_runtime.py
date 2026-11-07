"""Execution machinery shared by the synchronous and asynchronous lifecycle entrypoints of ``ModuleRegistry``.

Private to ``bedrock.module``. A registry operation is written once, as a generator that yields *effects* (call a
hook, send a signal, run a cleanup step). A sync driver and an async driver execute those effects, so the state
machine, rollback, idempotency and aggregate-error behavior exist in exactly one place.

This module also owns the two pieces of runtime state that must not depend on a thread lock being held across an
``await``: the operation gate that admits one registry operation at a time, and the persistent event loop that
synchronous entrypoints use to adapt asynchronous hooks.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Awaitable, Callable, Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, TypeVar

from .exc import ModuleLifecycleError

T = TypeVar("T")


@dataclass(frozen=True)
class HookEffect:
    """Call the ``hook_name`` bootstrap hook of ``app`` if the module defines it."""

    app: Any
    hook_name: str


@dataclass(frozen=True)
class SignalEffect:
    """Send ``signal`` with ``sender`` and ``kwargs`` to its receivers."""

    signal: Any
    sender: Any
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class CallEffect:
    """Run a synchronous cleanup callable inline, in both modes."""

    fn: Callable[[], Any]


@dataclass(frozen=True)
class AwaitEffect:
    """Run the awaitable produced by ``factory``; the factory is only called once running it is possible."""

    factory: Callable[[], Awaitable[Any]]


Effect = HookEffect | SignalEffect | CallEffect | AwaitEffect
Steps = Generator[Effect, Any, T]


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


async def _await[T](awaitable: Awaitable[T]) -> T:
    return await awaitable


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


class LoopAdapter:
    """Persistent event loop that lets synchronous entrypoints run asynchronous hooks.

    One adapter belongs to one registry. The loop is created when the first awaitable has to run and stays open
    for as long as the registry has a module that has not shut down, so an async client opened by a startup hook
    is closed by the shutdown hook on the same loop. ``asyncio.run`` per call would close the loop between hooks
    and strand such clients on a closed loop. A registry that is dropped without being shut down closes its loop
    when the adapter is garbage collected.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._finalizer: weakref.finalize | None = None

    @property
    def is_open(self) -> bool:
        """Return whether an adapter loop currently exists."""
        return self._loop is not None and not self._loop.is_closed()

    def run(self, awaitable: Awaitable[T]) -> T:
        """Run ``awaitable`` to completion on the adapter loop.

        Args:
            awaitable: Anything awaitable, such as a coroutine returned by a hook.

        Returns:
            The awaitable's result.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            loop = self._loop = asyncio.new_event_loop()
            self._finalizer = weakref.finalize(self, _close_loop, loop)
        return loop.run_until_complete(_await(awaitable))

    def close(self) -> None:
        """Close the adapter loop; see :func:`_close_loop`.

        Must not be called from a thread that is running another event loop.
        """
        loop, self._loop = self._loop, None
        finalizer, self._finalizer = self._finalizer, None
        if finalizer is not None:
            finalizer.detach()
        if loop is not None:
            _close_loop(loop)


@dataclass(eq=False)
class _Operation:
    """One top-level registry operation and the stack of nested operation names inside it."""

    gate: OperationGate
    stack: list[str]
    thread_id: int
    active: bool = True


_ACTIVE_OPERATIONS: ContextVar[tuple[_Operation, ...]] = ContextVar("bedrock_active_registry_operations", default=())
"""Operations the current context belongs to.

Hooks and receivers run inside the context of the operation that called them, including child tasks they spawn and
worker threads started with ``asyncio.to_thread``, so a call back into the registry from there is recognized as
re-entrant. Unrelated tasks and threads have no entry and wait their turn instead.
"""


@dataclass(eq=False)
class Lease:
    """Proof that the caller may run an operation; pass it back to :meth:`OperationGate.leave`."""

    operation: _Operation | None
    nested: bool = False
    token: Token[tuple[_Operation, ...]] | None = None

    @property
    def outermost(self) -> bool:
        """Return whether this lease started a new top-level operation."""
        return self.operation is not None and not self.nested


class OperationGate:
    """Admit one registry operation at a time without holding a lock across awaits.

    The gate state is guarded by a short-lived ``threading.Condition`` that is never held while a hook runs. A
    caller that already belongs to the running operation (same context) is re-entrant: only ``install`` may nest
    inside ``install``, anything else raises ``ModuleLifecycleError``. Any other caller waits: a thread blocks, a
    task awaits. A synchronous caller on the thread that owns the running operation raises instead of waiting,
    because waiting there would deadlock.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._current: _Operation | None = None
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    def entrypoint(self) -> str | None:
        """Return the top-level operation the current context is running inside, if any."""
        mine = self._mine()
        return mine.stack[0] if mine is not None else None

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
        if lease.nested:
            operation.stack.pop()
            return
        if lease.token is not None:
            try:
                _ACTIVE_OPERATIONS.reset(lease.token)
            except ValueError:
                pass  # released from another context; the inactive operation is ignored from now on
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
        if mine is None:
            yield
            return
        mine.stack.append(name)
        try:
            yield
        finally:
            mine.stack.pop()

    def _mine(self) -> _Operation | None:
        for operation in reversed(_ACTIVE_OPERATIONS.get()):
            if operation.gate is self and operation.active:
                return operation
        return None

    def _admit(self, name: str, introduces: Callable[[], bool] | None) -> Lease | None:
        """Return a lease, or ``None`` when another context must finish first (call with the condition held)."""
        mine = self._mine()
        if mine is not None:
            outer = mine.stack[-1]
            if name == "install" and introduces is not None and not introduces():
                return Lease(operation=None)
            if name == "install" and outer == "install":
                mine.stack.append(name)
                return Lease(operation=mine, nested=True)
            raise ModuleLifecycleError(_reentry_message(name, outer))
        if self._current is not None:
            return None
        operation = _Operation(gate=self, stack=[name], thread_id=threading.get_ident())
        self._current = operation
        return Lease(operation=operation)

    @staticmethod
    def _bind(lease: Lease) -> None:
        if lease.outermost:
            assert lease.operation is not None
            lease.token = _ACTIVE_OPERATIONS.set((*_ACTIVE_OPERATIONS.get(), lease.operation))


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
