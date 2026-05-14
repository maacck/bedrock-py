"""Unit tests for the signal package."""

from __future__ import annotations

import gc
import threading
import weakref

import pytest
from bedrock.signal import ANY, Signal, signal


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
