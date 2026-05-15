"""Unit tests for the signal package."""

from __future__ import annotations

import gc
import threading
import weakref

import pytest
from bedrock.signal import ANY, NamedSignal, Namespace, Signal, signal
from bedrock.signal._utilities import Symbol, make_id, make_ref


class TestSignalDispatch:
    """Tests for sync and async signal dispatch."""

    def test_send_returns_sync_receiver_results(self) -> None:
        sig = Signal()

        def receiver(sender, **kwargs):
            return (sender, kwargs["value"])

        sig.connect(receiver, weak=False)

        result = sig.send("worker", value=1)

        assert result == [(receiver, ("worker", 1))]

    @pytest.mark.asyncio
    async def test_asend_supports_mixed_sync_and_async_receivers(self) -> None:
        sig = Signal()

        def sync_receiver(sender, **kwargs):
            return ("sync", sender, kwargs["value"])

        async def async_receiver(sender, **kwargs):
            return ("async", sender, kwargs["value"])

        sig.connect(sync_receiver, weak=False)
        sig.connect(async_receiver, weak=False)

        result = await sig.asend("worker", value=2)

        assert sorted(item[1] for item in result) == [
            ("async", "worker", 2),
            ("sync", "worker", 2),
        ]

    @pytest.mark.asyncio
    async def test_asend_runs_sync_receivers_off_event_loop_thread(self) -> None:
        sig = Signal()
        loop_thread_id = threading.get_ident()
        receiver_thread_ids: list[int] = []

        def sync_receiver(sender, **kwargs):
            receiver_thread_ids.append(threading.get_ident())
            return "ok"

        sig.connect(sync_receiver, weak=False)

        result = await sig.asend("worker")

        assert result == [(sync_receiver, "ok")]
        assert receiver_thread_ids
        assert receiver_thread_ids[0] != loop_thread_id

    def test_send_supports_async_receivers(self) -> None:
        sig = Signal()

        async def async_receiver(sender, **kwargs):
            return "ok"

        sig.connect(async_receiver, weak=False)

        result = sig.send("worker")

        assert result == [(async_receiver, "ok")]

    def test_send_supports_mixed_sync_and_async_receivers(self) -> None:
        sig = Signal()

        def sync_receiver(sender, **kwargs):
            return ("sync", sender, kwargs["value"])

        async def async_receiver(sender, **kwargs):
            return ("async", sender, kwargs["value"])

        sig.connect(sync_receiver, weak=False)
        sig.connect(async_receiver, weak=False)

        result = sig.send("worker", value=2)

        assert sorted(item[1] for item in result) == [
            ("async", "worker", 2),
            ("sync", "worker", 2),
        ]

    @pytest.mark.asyncio
    async def test_send_supports_sync_receivers_in_running_loop(self) -> None:
        sig = Signal()

        def sync_receiver(sender, **kwargs):
            return ("sync", sender, kwargs["value"])

        sig.connect(sync_receiver, weak=False)

        result = sig.send("worker", value=42)

        assert result == [(sync_receiver, ("sync", "worker", 42))]

    @pytest.mark.asyncio
    async def test_send_raises_for_async_receivers_in_running_loop(self) -> None:
        sig = Signal()

        async def async_receiver(sender, **kwargs):
            return ("async", sender, kwargs["value"])

        sig.connect(async_receiver, weak=False)

        with pytest.raises(RuntimeError, match=r"Use await signal\.asend\(\.\.\.\) or provide _async_wrapper"):
            sig.send("worker", value=42)

    @pytest.mark.asyncio
    async def test_send_raises_for_mixed_receivers_in_running_loop(self) -> None:
        sig = Signal()

        def sync_receiver(sender, **kwargs):
            return ("sync", sender, kwargs["value"])

        async def async_receiver(sender, **kwargs):
            return ("async", sender, kwargs["value"])

        sig.connect(sync_receiver, weak=False)
        sig.connect(async_receiver, weak=False)

        with pytest.raises(RuntimeError, match="running event loop thread"):
            sig.send("worker", value=7)

    @pytest.mark.asyncio
    async def test_send_supports_async_receivers_in_running_loop_with_explicit_wrapper(self) -> None:
        sig = Signal()

        async def async_receiver(sender, **kwargs):
            return ("async", sender, kwargs["value"])

        sig.connect(async_receiver, weak=False)

        def async_to_sync(receiver):
            def wrapped(sender, **kwargs):
                result = None
                error = None

                def run_in_thread() -> None:
                    nonlocal result, error
                    import asyncio

                    try:
                        result = asyncio.run(receiver(sender, **kwargs))
                    except BaseException as exc:  # pragma: no cover - defensive passthrough
                        error = exc

                thread = threading.Thread(target=run_in_thread)
                thread.start()
                thread.join()

                if error is not None:
                    raise error

                return result

            return wrapped

        result = sig.send("worker", value=7, _async_wrapper=async_to_sync)

        assert result == [(async_receiver, ("async", "worker", 7))]

    def test_send_supports_async_callable_objects(self) -> None:
        sig = Signal()

        class AsyncCallable:
            async def __call__(self, sender, **kwargs):
                return ("callable", sender, kwargs["value"])

        receiver = AsyncCallable()
        sig.connect(receiver, weak=False)

        result = sig.send("worker", value=99)

        assert result == [(receiver, ("callable", "worker", 99))]

    @pytest.mark.asyncio
    async def test_asend_supports_async_callable_objects(self) -> None:
        sig = Signal()

        class AsyncCallable:
            async def __call__(self, sender, **kwargs):
                return ("callable", sender, kwargs["value"])

        receiver = AsyncCallable()
        sig.connect(receiver, weak=False)

        result = await sig.asend("worker", value=99)

        assert result == [(receiver, ("callable", "worker", 99))]

    def test_send_robust_collects_sync_and_async_failures(self) -> None:
        sig = Signal()

        def ok_sync(sender, **kwargs):
            return "ok-sync"

        def bad_sync(sender, **kwargs):
            raise ValueError("sync boom")

        async def ok_async(sender, **kwargs):
            return "ok-async"

        async def bad_async(sender, **kwargs):
            raise RuntimeError("async boom")

        sig.connect(ok_sync, weak=False)
        sig.connect(bad_sync, weak=False)
        sig.connect(ok_async, weak=False)
        sig.connect(bad_async, weak=False)

        result = sig.send_robust("worker")
        values = {receiver.__name__: value for receiver, value in result}

        assert values["ok_sync"] == "ok-sync"
        assert values["ok_async"] == "ok-async"
        assert isinstance(values["bad_sync"], ValueError)
        assert isinstance(values["bad_async"], RuntimeError)

    @pytest.mark.asyncio
    async def test_asend_robust_collects_sync_and_async_failures(self) -> None:
        sig = Signal()

        def ok_sync(sender, **kwargs):
            return "ok-sync"

        def bad_sync(sender, **kwargs):
            raise ValueError("sync boom")

        async def ok_async(sender, **kwargs):
            return "ok-async"

        async def bad_async(sender, **kwargs):
            raise RuntimeError("async boom")

        sig.connect(ok_sync, weak=False)
        sig.connect(bad_sync, weak=False)
        sig.connect(ok_async, weak=False)
        sig.connect(bad_async, weak=False)

        result = await sig.asend_robust("worker")
        values = {receiver.__name__: value for receiver, value in result}

        assert values["ok_sync"] == "ok-sync"
        assert values["ok_async"] == "ok-async"
        assert isinstance(values["bad_sync"], ValueError)
        assert isinstance(values["bad_async"], RuntimeError)


class TestSignalRouting:
    """Tests for sender matching and named signals."""

    def test_sender_specific_and_any_receivers(self) -> None:
        sig = Signal()
        sender_a = object()
        sender_b = object()

        def receiver_any(sender, **kwargs):
            return f"any:{kwargs['value']}"

        def receiver_a(sender, **kwargs):
            return f"a:{kwargs['value']}"

        sig.connect(receiver_any, sender=ANY, weak=False)
        sig.connect(receiver_a, sender=sender_a, weak=False)

        result_a = sig.send(sender_a, value=1)
        result_b = sig.send(sender_b, value=2)

        assert sorted(item[1] for item in result_a) == ["a:1", "any:1"]
        assert result_b == [(receiver_any, "any:2")]

    def test_signal_function_reuses_named_signal(self) -> None:
        assert signal("before_save") is signal("before_save")

    def test_has_receivers_for_respects_sender_registration(self) -> None:
        sig = Signal()
        sender = object()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, sender=sender, weak=False)

        assert sig.has_receivers_for(sender) is True
        assert sig.has_receivers_for(object()) is False


class TestSignalUtilities:
    """Tests for muted, temporary connections, and cleanup behavior."""

    def test_connected_to_temporarily_registers_receiver(self) -> None:
        sig = Signal()
        calls: list[str] = []

        def receiver(sender, **kwargs):
            calls.append(sender)
            return "ok"

        with sig.connected_to(receiver):
            inside = sig.send("inside")

        outside = sig.send("outside")

        assert inside == [(receiver, "ok")]
        assert outside == []
        assert calls == ["inside"]

    def test_muted_suppresses_dispatch(self) -> None:
        sig = Signal()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, weak=False)

        with sig.muted():
            result = sig.send("ignored")

        assert result == []

    def test_disconnect_removes_receiver(self) -> None:
        sig = Signal()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, weak=False)
        sig.disconnect(receiver)

        assert sig.send("worker") == []

    def test_weak_receiver_is_cleaned_up_after_collection(self) -> None:
        sig = Signal()

        class Handler:
            def __call__(self, sender, **kwargs):
                return "ok"

        handler = Handler()
        sig.connect(handler, weak=True)
        handler_ref = weakref.ref(handler)

        del handler
        gc.collect()

        assert handler_ref() is None
        assert list(sig.receivers_for("worker")) == []


class TestSignalConnect:
    """Tests for connection-related behaviors of the Signal class."""

    def test_connect_via_registers_and_dispatches_for_target_sender(self) -> None:
        sig = Signal()
        sender_a = object()
        sender_b = object()
        calls: list[str] = []

        @sig.connect_via(sender_a, weak=False)
        def receiver(sender, **kwargs):
            calls.append("called")
            return "ok"

        result_a = sig.send(sender_a)
        result_b = sig.send(sender_b)

        assert result_a == [(receiver, "ok")]
        assert result_b == []
        assert calls == ["called"]

    def test_connect_via_with_weak_true_allows_gc(self) -> None:
        sig = Signal()
        sender = object()

        @sig.connect_via(sender, weak=True)
        def receiver(sender, **kwargs):
            return "ok"

        receiver_ref = weakref.ref(receiver)
        del receiver
        gc.collect()

        assert receiver_ref() is None
        assert list(sig.receivers_for(sender)) == []

    def test_receiver_connected_meta_signal_fires_on_connect(self) -> None:
        sig = Signal()
        meta_calls: list[dict] = []

        def meta_receiver(*args, **kwargs):
            meta_calls.append(kwargs)

        sig.receiver_connected.connect(meta_receiver, weak=False)

        def my_receiver(sender, **kwargs):
            return "ok"

        sig.connect(my_receiver, sender="s1", weak=False)

        assert len(meta_calls) == 1
        assert meta_calls[0]["receiver"] is my_receiver
        assert meta_calls[0]["sender"] == "s1"
        assert meta_calls[0]["weak"] is False

    def test_receiver_disconnected_meta_signal_fires_on_disconnect(self) -> None:
        sig = Signal()
        meta_calls: list[dict] = []

        def meta_receiver(*args, **kwargs):
            meta_calls.append(kwargs)

        sig.receiver_disconnected.connect(meta_receiver, weak=False)

        def my_receiver(sender, **kwargs):
            return "ok"

        sig.connect(my_receiver, weak=False)
        sig.disconnect(my_receiver)

        assert len(meta_calls) == 1
        assert meta_calls[0]["receiver"] is my_receiver
        assert meta_calls[0]["sender"] is ANY

    def test_receiver_connected_error_rolls_back_connection(self) -> None:
        sig = Signal()

        def bad_meta_receiver(*args, **kwargs):
            raise TypeError("meta error")

        sig.receiver_connected.connect(bad_meta_receiver, weak=False)

        def my_receiver(sender, **kwargs):
            return "ok"

        with pytest.raises(TypeError, match="meta error"):
            sig.connect(my_receiver, weak=False)

        assert list(sig.receivers_for("any_sender")) == []

    def test_weak_sender_cleanup_removes_sender_specific_receiver(self) -> None:
        sig = Signal()

        class Sender:
            pass

        def receiver(sender, **kwargs):
            return "ok"

        sender = Sender()
        sig.connect(receiver, sender=sender, weak=False)

        assert list(sig.receivers_for(sender))

        del sender
        gc.collect()

        assert list(sig.receivers_for(object())) == []

    def test_disconnect_specific_sender_preserves_other_senders(self) -> None:
        sig = Signal()

        class Sender:
            pass

        def receiver(sender, **kwargs):
            return "ok"

        sender_a = Sender()
        sender_b = Sender()
        sig.connect(receiver, sender=sender_a, weak=False)
        sig.connect(receiver, sender=sender_b, weak=False)

        sig.disconnect(receiver, sender=sender_a)

        assert list(sig.receivers_for(sender_a)) == []
        assert list(sig.receivers_for(sender_b)) == [receiver]

    def test_bound_method_weak_receiver_cleaned_up_after_instance_deletion(self) -> None:
        sig = Signal()

        class Handler:
            def method(self, sender, **kwargs):
                return "ok"

        handler = Handler()
        sig.connect(handler.method, weak=True)
        handler_ref = weakref.ref(handler)

        del handler
        gc.collect()

        assert handler_ref() is None
        assert list(sig.receivers_for("any_sender")) == []

    def test_connect_returns_the_receiver(self) -> None:
        sig = Signal()

        def my_receiver(sender, **kwargs):
            return "ok"

        result = sig.connect(my_receiver, weak=False)

        assert result is my_receiver


class TestAdvancedSignal:
    """Tests for advanced and utility behaviors of the Signal system."""

    def test_namespace_creates_and_reuses_named_signal(self) -> None:
        ns = Namespace()
        s1 = ns.signal("foo", doc="Foo")
        s2 = ns.signal("foo")

        assert s1 is s2
        assert s1.name == "foo"
        assert "foo" in ns

    def test_namespace_signals_are_independent(self) -> None:
        ns = Namespace()
        s1 = ns.signal("alpha")
        s2 = ns.signal("beta")

        assert s1 is not s2
        assert s1.name == "alpha"
        assert s2.name == "beta"

    def test_named_signal_repr_contains_name(self) -> None:
        ns = Namespace()
        s = ns.signal("my_event")

        r = repr(s)
        assert "my_event" in r

    def test_named_signal_inherits_signal_behavior(self) -> None:
        ns = Namespace()
        s = ns.signal("greeting")

        def receiver(sender, **kwargs):
            return f"hello {sender}"

        s.connect(receiver, weak=False)

        result = s.send("world")
        assert result == [(receiver, "hello world")]

    def test_symbol_singleton(self) -> None:
        s1 = Symbol("test")
        s2 = Symbol("test")
        assert s1 is s2

    def test_symbol_different_names_are_different_objects(self) -> None:
        s1 = Symbol("alpha")
        s2 = Symbol("beta")
        assert s1 is not s2

    def test_symbol_repr(self) -> None:
        assert repr(Symbol("foo")) == "foo"

    def test_signal_factory_returns_named_signal_and_reuses(self) -> None:
        ns = Namespace()
        factory = ns.signal

        s = factory("x")
        assert isinstance(s, NamedSignal)
        assert factory("x") is s

    def test_send_with_sender_none_delivers_to_any_receivers(self) -> None:
        sig = Signal()

        def receiver(sender, **kwargs):
            return f"got:{sender}"

        sig.connect(receiver, sender=ANY, weak=False)

        result = sig.send(None)
        assert result == [(receiver, "got:None")]

    def test_has_receivers_for_any_returns_false_when_only_specific_senders(self) -> None:
        sig = Signal()
        sender = object()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, sender=sender, weak=False)

        assert sig.has_receivers_for(ANY) is False

    def test_has_receivers_for_specific_sender_true_when_any_connected(self) -> None:
        sig = Signal()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, sender=ANY, weak=False)

        assert sig.has_receivers_for("some_sender") is True

    @pytest.mark.asyncio
    async def test_muted_suppresses_asend(self) -> None:
        sig = Signal()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, weak=False)

        with sig.muted():
            result = await sig.asend("ignored")

        assert result == []

    def test_send_with_no_receivers_returns_empty_list(self) -> None:
        sig = Signal()
        result = sig.send("x")
        assert result == []

    @pytest.mark.asyncio
    async def test_asend_with_no_receivers_returns_empty_list(self) -> None:
        sig = Signal()
        result = await sig.asend("x")
        assert result == []

    def test_clear_state_resets_all_internal_state(self) -> None:
        sig = Signal()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, weak=False)
        assert sig.send("x") == [(receiver, "ok")]

        sig._clear_state()

        assert sig.receivers == {}
        assert sig.send("x") == []

    def test_signal_doc_is_set_when_provided(self) -> None:
        sig = Signal(doc="My custom signal")
        assert sig.__doc__ == "My custom signal"

    def test_signal_doc_is_not_set_when_omitted(self) -> None:
        sig = Signal()
        assert sig.__doc__ == Signal.__doc__

    def test_make_id_for_bound_methods(self) -> None:
        class Obj:
            def method(self):
                pass

        obj = Obj()
        result = make_id(obj.method)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result == (id(obj.method.__func__), id(obj.method.__self__))

    def test_make_id_for_strings_and_ints(self) -> None:
        assert make_id("foo") == "foo"
        assert make_id(42) == 42

    def test_make_ref_for_bound_methods_uses_weak_method(self) -> None:
        class Obj:
            def method(self):
                pass

        obj = Obj()
        ref = make_ref(obj.method)
        assert isinstance(ref, weakref.WeakMethod)

    def test_cleanup_bookkeeping_prunes_empty_buckets(self) -> None:
        sig = Signal()
        sender = object()

        def receiver(sender, **kwargs):
            return "ok"

        sig.connect(receiver, sender=sender, weak=False)
        sig.disconnect(receiver, sender=sender)

        sig._cleanup_bookkeeping()

        receiver_id = make_id(receiver)
        sender_id = id(sender)
        assert receiver_id not in sig._by_receiver
        assert sender_id not in sig._by_sender
