# This file is derived from the Blinker library.
# Copyright 2010 Jason Kirtland
# Licensed under the MIT License. See LICENSE.txt for details.
# Original source: https://github.com/pallets-eco/blinker

from __future__ import annotations

import asyncio
import collections.abc as c
import inspect
import sys
import typing as t
import weakref
from collections import defaultdict
from contextlib import contextmanager
from functools import cached_property

from ._utilities import Symbol, make_id, make_ref

F = t.TypeVar("F", bound=c.Callable[..., t.Any])
Receiver = c.Callable[..., t.Any]
AsyncReceiver = c.Callable[..., c.Coroutine[t.Any, t.Any, t.Any]]
SyncToAsyncWrapper = c.Callable[[Receiver], AsyncReceiver]
AsyncToSyncWrapper = c.Callable[[AsyncReceiver], Receiver]
DispatchResult = list[tuple[Receiver, t.Any]]
RobustDispatchResult = list[tuple[Receiver, t.Any | Exception]]

ANY = Symbol("ANY")
"""Symbol for "any sender"."""

ANY_ID = 0


class Signal:
    """A notification emitter.

    Args:
        doc: The docstring for the signal.
    """

    ANY = ANY
    """An alias for the :data:`~blinker.ANY` sender symbol."""

    set_class: type[set[t.Any]] = set
    """The set class to use for tracking connected receivers and senders.
    Python's ``set`` is unordered. If receivers must be dispatched in the order
    they were connected, an ordered set implementation can be used.

    .. versionadded:: 1.7
    """

    @cached_property
    def receiver_connected(self) -> Signal:
        """Emitted at the end of each :meth:`connect` call.

        The signal sender is the signal instance, and the :meth:`connect`
        arguments are passed through: ``receiver``, ``sender``, and ``weak``.

        Returns:
            `Signal`: The connected receiver.
        """
        return Signal(doc="Emitted after a receiver connects.")

    @cached_property
    def receiver_disconnected(self) -> Signal:
        """Emitted at the end of each :meth:`disconnect` call.

        The sender is the signal instance, and the :meth:`disconnect` arguments
        are passed through: ``receiver`` and ``sender``.

        This signal is emitted **only** when :meth:`disconnect` is called
        explicitly. This signal cannot be emitted by an automatic disconnect
        when a weakly referenced receiver or sender goes out of scope, as the
        instance is no longer available to be used as the sender for this
        signal.

        An alternative approach is available by subscribing to
        :attr:`receiver_connected` and setting up a custom weakref cleanup
        callback on weak receivers and senders.

        Returns:
            `Signal`: The connected receiver.
        """
        return Signal(doc="Emitted after a receiver disconnects.")

    def __init__(self, doc: str | None = None) -> None:
        """Initialize the Signal instance.

        Args:
            doc: Optional docstring for the signal. If provided, it will be
                set as the instance's ``__doc__`` attribute.
        """
        if doc:
            self.__doc__ = doc

        self.receivers: dict[t.Any, weakref.ref[Receiver] | Receiver] = {}
        """The map of connected receivers. Useful to quickly check if any
        receivers are connected to the signal: ``if s.receivers:``. The
        structure and data is not part of the public API, but checking its
        boolean value is.
        """

        self.is_muted: bool = False
        self._by_receiver: dict[t.Any, set[t.Any]] = defaultdict(self.set_class)
        self._by_sender: dict[t.Any, set[t.Any]] = defaultdict(self.set_class)
        self._weak_senders: dict[t.Any, weakref.ref[t.Any]] = {}

    def connect(self, receiver: F, sender: t.Any = ANY, weak: bool = True) -> F:
        """Connect ``receiver`` to be called when the signal is sent by ``sender``.

        Args:
            receiver: The callable to call when :meth:`send` is called with
                the given ``sender``, passing ``sender`` as a positional argument
                along with any extra keyword arguments.
            sender: Any object or :data:`ANY`. ``receiver`` will only be
                called when :meth:`send` is called with this sender. If ``ANY``, the
                receiver will be called for any sender. A receiver may be connected
                to multiple senders by calling :meth:`connect` multiple times.
            weak: Track the receiver with a :mod:`weakref`. The receiver will
                be automatically disconnected when it is garbage collected. When
                connecting a receiver defined within a function, set to ``False``,
                otherwise it will be disconnected when the function scope ends.

        Returns:
            The connected receiver.
        """
        receiver_id = make_id(receiver)
        sender_id = ANY_ID if sender is ANY else make_id(sender)

        if weak:
            self.receivers[receiver_id] = make_ref(receiver, self._make_cleanup_receiver(receiver_id))
        else:
            self.receivers[receiver_id] = receiver

        self._by_sender[sender_id].add(receiver_id)
        self._by_receiver[receiver_id].add(sender_id)

        if sender is not ANY and sender_id not in self._weak_senders:
            try:
                self._weak_senders[sender_id] = make_ref(sender, self._make_cleanup_sender(sender_id))
            except TypeError:
                pass

        if "receiver_connected" in self.__dict__ and self.receiver_connected.receivers:
            try:
                self.receiver_connected.send(self, receiver=receiver, sender=sender, weak=weak)
            except TypeError:
                self.disconnect(receiver, sender)
                raise

        return receiver

    def connect_via(self, sender: t.Any, weak: bool = False) -> c.Callable[[F], F]:
        """Connect the decorated function to be called when the signal is sent by ``sender``.

        The decorated function will be called when :meth:`send` is called with
        the given ``sender``, passing ``sender`` as a positional argument along
        with any extra keyword arguments.

        Args:
            sender: Any object or :data:`ANY`. ``receiver`` will only be
                called when :meth:`send` is called with this sender. If ``ANY``, the
                receiver will be called for any sender. A receiver may be connected
                to multiple senders by calling :meth:`connect` multiple times.
            weak: Track the receiver with a :mod:`weakref`. The receiver will
                be automatically disconnected when it is garbage collected. When
                connecting a receiver defined within a function, set to ``False``,
                otherwise it will be disconnected when the function scope ends.

        Returns:
            A decorator that connects the function and returns it.
        """

        def decorator(fn: F) -> F:
            self.connect(fn, sender, weak)
            return fn

        return decorator

    @contextmanager
    def connected_to(self, receiver: c.Callable[..., t.Any], sender: t.Any = ANY) -> c.Generator[None, None, None]:
        """A context manager that temporarily connects ``receiver`` to the
        signal while a ``with`` block executes. When the block exits, the
        receiver is disconnected. Useful for tests.

        Args:
            receiver: The callable to call when :meth:`send` is called with
                the given ``sender``, passing ``sender`` as a positional argument
                along with any extra keyword arguments.
            sender: Any object or :data:`ANY`. ``receiver`` will only be
                called when :meth:`send` is called with this sender. If ``ANY``, the
                receiver will be called for any sender.

        Yields:
            Nothing.
        """
        self.connect(receiver, sender=sender, weak=False)

        try:
            yield None
        finally:
            self.disconnect(receiver)

    @contextmanager
    def muted(self) -> c.Generator[None, None, None]:
        """A context manager that temporarily disables the signal.

        Yields:
            Nothing.
        """
        self.is_muted = True

        try:
            yield None
        finally:
            self.is_muted = False

    def send(
        self,
        sender: t.Any | None = None,
        /,
        *,
        _async_wrapper: AsyncToSyncWrapper | None = None,
        **kwargs: t.Any,
    ) -> DispatchResult:
        """Call all receivers connected to ``sender`` or :data:`ANY`.

        Each receiver is called with ``sender`` as a positional argument along
        with any extra keyword arguments. Return a list of ``(receiver, result)``
        tuples.

        If an async receiver is connected, provide ``_async_wrapper`` to adapt it
        to sync execution, or use :meth:`asend` instead.

        Args:
            sender: The signal sender. If ``None``, receivers connected to
                :data:`ANY` will be called.
            _async_wrapper: Optional wrapper to adapt async receivers to sync execution.
            **kwargs: Extra keyword arguments to pass to receivers.

        Returns:
            A list of ``(receiver, result)`` tuples.

        Raises:
            RuntimeError: If an async receiver is connected and no ``_async_wrapper``
                is provided.
        """
        return self._send_sync(
            sender,
            _async_wrapper=_async_wrapper,
            robust=False,
            **kwargs,
        )

    def send_robust(
        self,
        sender: t.Any | None = None,
        /,
        *,
        _async_wrapper: AsyncToSyncWrapper | None = None,
        **kwargs: t.Any,
    ) -> RobustDispatchResult:
        """Call all receivers connected to ``sender`` and capture exceptions.

        Exceptions derived from :class:`Exception` are collected in the result
        list instead of being raised. ``BaseException`` subclasses still
        propagate.

        Args:
            sender: The signal sender. If ``None``, receivers connected to
                :data:`ANY` will be called.
            _async_wrapper: Optional wrapper to adapt async receivers to sync execution.
            **kwargs: Extra keyword arguments to pass to receivers.

        Returns:
            A list of ``(receiver, result)`` tuples, where ``result`` may be
            an ``Exception`` instance if the receiver raised.
        """
        return self._send_sync(
            sender,
            _async_wrapper=_async_wrapper,
            robust=True,
            **kwargs,
        )

    async def asend(
        self,
        sender: t.Any | None = None,
        /,
        *,
        _sync_wrapper: SyncToAsyncWrapper | None = None,
        **kwargs: t.Any,
    ) -> DispatchResult:
        """Await all receivers connected to ``sender`` or :data:`ANY`.

        Synchronous receivers are adapted to async execution with
        ``_sync_wrapper``. When no wrapper is supplied, synchronous receivers are
        executed in a worker thread via :func:`asyncio.to_thread`.

        Args:
            sender: The signal sender. If ``None``, receivers connected to
                :data:`ANY` will be called.
            _sync_wrapper: Optional wrapper to adapt sync receivers to async execution.
            **kwargs: Extra keyword arguments to pass to receivers.

        Returns:
            A list of ``(receiver, result)`` tuples.
        """
        return await self._send_async(
            sender,
            _sync_wrapper=_sync_wrapper,
            robust=False,
            **kwargs,
        )

    async def asend_robust(
        self,
        sender: t.Any | None = None,
        /,
        *,
        _sync_wrapper: SyncToAsyncWrapper | None = None,
        **kwargs: t.Any,
    ) -> RobustDispatchResult:
        """Await all receivers connected to ``sender`` and capture exceptions.

        Args:
            sender: The signal sender. If ``None``, receivers connected to
                :data:`ANY` will be called.
            _sync_wrapper: Optional wrapper to adapt sync receivers to async execution.
            **kwargs: Extra keyword arguments to pass to receivers.

        Returns:
            A list of ``(receiver, result)`` tuples, where ``result`` may be
            an ``Exception`` instance if the receiver raised.
        """
        return await self._send_async(
            sender,
            _sync_wrapper=_sync_wrapper,
            robust=True,
            **kwargs,
        )

    def _send_sync(
        self,
        sender: t.Any | None,
        /,
        *,
        _async_wrapper: AsyncToSyncWrapper | None,
        robust: bool,
        **kwargs: t.Any,
    ) -> DispatchResult | RobustDispatchResult:
        if self.is_muted:
            return []

        results: DispatchResult | RobustDispatchResult = []

        for receiver in self.receivers_for(sender):
            try:
                result = self._invoke_receiver_sync(
                    receiver,
                    sender,
                    _async_wrapper=_async_wrapper,
                    **kwargs,
                )
            except Exception as exc:
                if not robust:
                    raise
                result = exc

            results.append((receiver, result))

        return results

    async def _send_async(
        self,
        sender: t.Any | None,
        /,
        *,
        _sync_wrapper: SyncToAsyncWrapper | None,
        robust: bool,
        **kwargs: t.Any,
    ) -> DispatchResult | RobustDispatchResult:
        if self.is_muted:
            return []

        results: DispatchResult | RobustDispatchResult = []
        sync_wrapper = _sync_wrapper or self._default_sync_wrapper

        for receiver in self.receivers_for(sender):
            try:
                result = await self._invoke_receiver_async(
                    receiver,
                    sender,
                    _sync_wrapper=sync_wrapper,
                    **kwargs,
                )
            except Exception as exc:
                if not robust:
                    raise
                result = exc

            results.append((receiver, result))

        return results

    def _invoke_receiver_sync(
        self,
        receiver: Receiver,
        sender: t.Any | None,
        /,
        *,
        _async_wrapper: AsyncToSyncWrapper | None,
        **kwargs: t.Any,
    ) -> t.Any:
        if self._is_async_callable(receiver):
            if _async_wrapper is None:
                raise RuntimeError(
                    "Cannot send to an async receiver with send(). Use await signal.asend(...) or provide _async_wrapper."
                )
            return _async_wrapper(t.cast(AsyncReceiver, receiver))(sender, **kwargs)

        return receiver(sender, **kwargs)

    async def _invoke_receiver_async(
        self,
        receiver: Receiver,
        sender: t.Any | None,
        /,
        *,
        _sync_wrapper: SyncToAsyncWrapper,
        **kwargs: t.Any,
    ) -> t.Any:
        if self._is_async_callable(receiver):
            return await t.cast(AsyncReceiver, receiver)(sender, **kwargs)

        return await _sync_wrapper(receiver)(sender, **kwargs)

    @staticmethod
    def _is_async_callable(receiver: Receiver) -> bool:
        if inspect.iscoroutinefunction(receiver):
            return True

        if not callable(receiver):
            return False

        return inspect.iscoroutinefunction(receiver)

    @staticmethod
    def _default_sync_wrapper(receiver: Receiver) -> AsyncReceiver:
        async def wrapped(*args: t.Any, **kwargs: t.Any) -> t.Any:
            return await asyncio.to_thread(receiver, *args, **kwargs)

        return wrapped

    def has_receivers_for(self, sender: t.Any) -> bool:
        """Check if at least one receiver will be called for ``sender``.

        Args:
            sender: The sender to check for connected receivers.

        Returns:
            ``True`` if at least one receiver is connected for the sender,
            ``False`` otherwise.
        """
        if not self.receivers:
            return False

        if self._by_sender[ANY_ID]:
            return True

        if sender is ANY:
            return False

        return make_id(sender) in self._by_sender

    def receivers_for(self, sender: t.Any) -> c.Generator[c.Callable[..., t.Any], None, None]:
        """Yield each receiver to be called for ``sender``.

        Args:
            sender: The sender to get receivers for.

        Yields:
            Each receiver callable that should be called for the given sender.
        """
        if not self.receivers:
            return

        sender_id = make_id(sender)

        if sender_id in self._by_sender:
            ids = self._by_sender[ANY_ID] | self._by_sender[sender_id]
        else:
            ids = self._by_sender[ANY_ID].copy()

        for receiver_id in ids:
            receiver = self.receivers.get(receiver_id)

            if receiver is None:
                continue

            if isinstance(receiver, weakref.ref):
                strong = receiver()

                if strong is None:
                    self._disconnect(receiver_id, ANY_ID)
                    continue

                yield strong
            else:
                yield receiver

    def disconnect(self, receiver: c.Callable[..., t.Any], sender: t.Any = ANY) -> None:
        """Disconnect ``receiver`` from being called when the signal is sent by ``sender``.

        Args:
            receiver: The callable to disconnect.
            sender: The sender to disconnect from. Defaults to :data:`ANY`.
        """
        sender_id: c.Hashable

        if sender is ANY:
            sender_id = ANY_ID
        else:
            sender_id = make_id(sender)

        receiver_id = make_id(receiver)
        self._disconnect(receiver_id, sender_id)

        if "receiver_disconnected" in self.__dict__ and self.receiver_disconnected.receivers:
            self.receiver_disconnected.send(self, receiver=receiver, sender=sender)

    def _disconnect(self, receiver_id: c.Hashable, sender_id: c.Hashable) -> None:
        if sender_id == ANY_ID:
            if self._by_receiver.pop(receiver_id, None) is not None:
                for bucket in self._by_sender.values():
                    bucket.discard(receiver_id)

            self.receivers.pop(receiver_id, None)
        else:
            self._by_sender[sender_id].discard(receiver_id)
            self._by_receiver[receiver_id].discard(sender_id)

    def _make_cleanup_receiver(
        self, receiver_id: c.Hashable
    ) -> c.Callable[[weakref.ref[c.Callable[..., t.Any]]], None]:
        """Create a callback to disconnect a weak receiver when collected."""

        def cleanup(ref: weakref.ref[c.Callable[..., t.Any]]) -> None:
            if not sys.is_finalizing():
                self._disconnect(receiver_id, ANY_ID)

        return cleanup

    def _make_cleanup_sender(self, sender_id: c.Hashable) -> c.Callable[[weakref.ref[t.Any]], None]:
        """Create a callback to disconnect receivers for a weak sender."""
        assert sender_id != ANY_ID

        def cleanup(ref: weakref.ref[t.Any]) -> None:
            self._weak_senders.pop(sender_id, None)

            for receiver_id in self._by_sender.pop(sender_id, ()):
                self._by_receiver[receiver_id].discard(sender_id)

        return cleanup

    def _cleanup_bookkeeping(self) -> None:
        """Prune unused sender and receiver bookkeeping."""
        for mapping in (self._by_sender, self._by_receiver):
            for ident, bucket in list(mapping.items()):
                if not bucket:
                    mapping.pop(ident, None)

    def _clear_state(self) -> None:
        """Disconnect all receivers and senders. Useful for tests."""
        self._weak_senders.clear()
        self.receivers.clear()
        self._by_sender.clear()
        self._by_receiver.clear()


class NamedSignal(Signal):
    """A named generic notification emitter.

    This is a subclass of :class:`Signal` that also has a ``name`` attribute
    for identification purposes.

    Args:
        name: The name of the signal.
        doc: Optional docstring for the signal.
    """

    def __init__(self, name: str, doc: str | None = None) -> None:
        super().__init__(doc)
        self.name: str = name

    def __repr__(self) -> str:
        base = super().__repr__()
        return f"{base[:-1]}; {self.name!r}>"


class Namespace(dict[str, NamedSignal]):
    """A dictionary-like container that maps signal names to :class:`NamedSignal` instances.

    This class provides a convenient way to manage a collection of named signals,
    automatically creating them when requested if they don't already exist.
    """

    def signal(self, name: str, doc: str | None = None) -> NamedSignal:
        """Return the named signal for ``name``, creating it if required.

        Args:
            name: The name of the signal to retrieve or create.
            doc: Optional docstring for the new signal (only used if the signal
                is being created for the first time).

        Returns:
            The :class:`NamedSignal` associated with the given name.
        """
        if name not in self:
            self[name] = NamedSignal(name, doc)

        return self[name]


class _PNamespaceSignal(t.Protocol):
    """Protocol for the signal factory function in a namespace.

    This protocol defines the interface for creating or retrieving named signals
    within a namespace.

    Args:
        name: The name of the signal to retrieve or create.
        doc: Optional docstring for the new signal.

    Returns:
        The :class:`NamedSignal` associated with the given name.
    """

    def __call__(self, name: str, doc: str | None = None) -> NamedSignal: ...


default_namespace: Namespace = Namespace()
"""Default namespace for creating named signals."""

signal: _PNamespaceSignal = default_namespace.signal
"""Return a :class:`NamedSignal` in :data:`default_namespace`."""
