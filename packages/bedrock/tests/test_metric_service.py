"""Tests for the MetricsManager: handles, decorators, logging, fan-out, isolation."""

import asyncio
import logging

import pytest

from bedrock.contrib.metric import (
    Counter,
    Gauge,
    Timer,
    list_providers,
    metrics,
    register_provider,
)
from bedrock.contrib.metric.base import MetricProvider
from bedrock.contrib.metric.events import MetricEvent
from bedrock.contrib.metric.service import MetricsManager


class FakeProvider:
    """Provider recording received events."""

    def __init__(self) -> None:
        self.events: list[MetricEvent] = []
        self.closed = False

    def emit(self, event: MetricEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


class FailingProvider:
    """Provider that always raises on emit."""

    def emit(self, event: MetricEvent) -> None:
        raise RuntimeError("boom")

    def close(self) -> None:
        pass


class EqualProvider(FakeProvider):
    """Provider whose distinct instances compare equal (identity must still win)."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EqualProvider)


@pytest.fixture
def manager() -> MetricsManager:
    m = MetricsManager()
    yield m
    m.close()


def test_counter_inc_and_tags(manager: MetricsManager) -> None:
    """counter().inc emits counter events with tags."""
    fake = FakeProvider()
    manager.register_provider(fake)
    manager.counter("a.b", tags={"k": "v"}).inc(2)
    assert len(fake.events) == 1
    event = fake.events[0]
    assert event.type == "counter"
    assert event.name == "a.b"
    assert event.value == 2.0
    assert event.tags == {"k": "v"}


def test_gauge_set(manager: MetricsManager) -> None:
    """gauge().set emits gauge events."""
    fake = FakeProvider()
    manager.register_provider(fake)
    manager.gauge("g").set(42)
    assert fake.events[0].type == "gauge"
    assert fake.events[0].value == 42.0


def test_timer_observe_and_context_manager(manager: MetricsManager) -> None:
    """timer().observe and the context manager emit timer events."""
    fake = FakeProvider()
    manager.register_provider(fake)
    manager.timer("t").observe(1.5)
    assert fake.events[0].type == "timer"
    assert fake.events[0].value == 1.5
    with manager.timer("t2"):
        pass
    assert fake.events[1].type == "timer"
    assert fake.events[1].value >= 0.0


def test_counter_decorator(manager: MetricsManager) -> None:
    """@counter increments once per call; exceptions still increment and propagate."""

    @manager.counter("calls", tags={"fn": "f"})
    def f(x: int) -> int:
        return x * 2

    fake = FakeProvider()
    manager.register_provider(fake)
    assert f(2) == 4
    assert f(3) == 6
    assert len(fake.events) == 2
    assert all(e.type == "counter" and e.value == 1.0 and e.tags == {"fn": "f"} for e in fake.events)


def test_counter_decorator_async(manager: MetricsManager) -> None:
    """@counter works on async callables; exceptions still increment and propagate."""

    @manager.counter("acalls")
    async def af(x: int) -> int:
        return x + 1

    @manager.counter("abooms")
    async def aboom() -> None:
        raise RuntimeError("async boom")

    fake = FakeProvider()
    manager.register_provider(fake)
    assert asyncio.run(af(1)) == 2
    with pytest.raises(RuntimeError):
        asyncio.run(aboom())
    assert len(fake.events) == 2
    assert all(e.type == "counter" and e.value == 1.0 for e in fake.events)


def test_timer_decorator_sync_and_async(manager: MetricsManager) -> None:
    """@timer works on sync and async callables."""

    @manager.timer("dur")
    def sync_fn() -> str:
        return "ok"

    @manager.timer("adur")
    async def async_fn() -> str:
        return "ok"

    fake = FakeProvider()
    manager.register_provider(fake)
    assert sync_fn() == "ok"
    assert asyncio.run(async_fn()) == "ok"
    assert [e.name for e in fake.events] == ["dur", "adur"]
    assert all(e.type == "timer" for e in fake.events)


def test_decorator_exception_still_emits_and_propagates(manager: MetricsManager) -> None:
    """A raising function still records the metric and re-raises to the caller."""

    @manager.timer("failing")
    def boom() -> None:
        raise ValueError("nope")

    fake = FakeProvider()
    manager.register_provider(fake)
    with pytest.raises(ValueError):
        boom()
    assert len(fake.events) == 1
    assert fake.events[0].type == "timer"


def test_timer_decorator_async_exception_propagates(manager: MetricsManager) -> None:
    """An async function that raises still records the metric and re-raises."""

    @manager.timer("afailing")
    async def aboom() -> None:
        raise ValueError("async nope")

    fake = FakeProvider()
    manager.register_provider(fake)
    with pytest.raises(ValueError):
        asyncio.run(aboom())
    assert len(fake.events) == 1
    assert fake.events[0].type == "timer"


def test_hardcoded_logger_emits_without_providers(manager: MetricsManager, caplog: pytest.LogCaptureFixture) -> None:
    """With no providers, emits still write a log line (logger is hardcoded)."""
    manager.set_log_level(logging.INFO)
    with caplog.at_level(logging.INFO, logger="bedrock.contrib.metric"):
        manager.counter("lonely").inc()
    assert any("metric counter lonely=1.0" in r.message for r in caplog.records)


def test_log_level_is_configurable(manager: MetricsManager, caplog: pytest.LogCaptureFixture) -> None:
    """set_log_level changes the level at which events are logged."""
    manager.set_log_level(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger="bedrock.contrib.metric"):
        manager.counter("dbg").inc()
    assert any(r.levelno == logging.DEBUG and "metric counter dbg=1.0" in r.message for r in caplog.records)


def test_fanout_all_providers_receive(manager: MetricsManager) -> None:
    """Every registered provider receives every event."""
    fake1 = FakeProvider()
    fake2 = FakeProvider()
    manager.register_provider(fake1)
    manager.register_provider(fake2)
    manager.counter("c").inc()
    assert len(fake1.events) == 1 and len(fake2.events) == 1


def test_register_is_idempotent(manager: MetricsManager) -> None:
    """Registering the same provider twice is a no-op."""
    fake = FakeProvider()
    manager.register_provider(fake)
    manager.register_provider(fake)
    assert len(manager.list_providers()) == 1


def test_register_keeps_distinct_equal_instances(manager: MetricsManager) -> None:
    """Distinct instances that compare equal are both kept; only the same instance dedupes."""
    p1 = EqualProvider()
    p2 = EqualProvider()
    assert p1 is not p2 and p1 == p2
    manager.register_provider(p1)
    manager.register_provider(p2)
    manager.register_provider(p1)
    assert len(manager.list_providers()) == 2
    manager.counter("c").inc()
    assert len(p1.events) == 1 and len(p2.events) == 1


def test_provider_registered_after_handle_creation_receives(manager: MetricsManager) -> None:
    """Handles re-read the provider list at emit time."""
    counter = manager.counter("late")
    fake = FakeProvider()
    manager.register_provider(fake)
    counter.inc()
    assert len(fake.events) == 1


def test_failing_provider_does_not_break_business(manager: MetricsManager, caplog: pytest.LogCaptureFixture) -> None:
    """A provider that raises is swallowed; other providers still receive; caller unaffected."""
    bad = FailingProvider()
    good = FakeProvider()
    manager.register_provider(bad)
    manager.register_provider(good)
    with caplog.at_level(logging.WARNING, logger="bedrock.contrib.metric"):
        manager.counter("safe").inc()  # must not raise
    assert len(good.events) == 1
    assert any("failed in FailingProvider" in r.message for r in caplog.records)


def test_provider_failure_warning_is_rate_limited(manager: MetricsManager, caplog: pytest.LogCaptureFixture) -> None:
    """Provider failures warn on the first occurrence and every 100th per provider+metric."""
    manager.register_provider(FailingProvider())
    counter = manager.counter("r")
    with caplog.at_level(logging.WARNING, logger="bedrock.contrib.metric"):
        for _ in range(250):
            counter.inc()
    messages = [r.message for r in caplog.records if "failed in FailingProvider" in r.message]
    assert len(messages) == 3  # occurrences 1, 100, 200


def test_provider_failure_warnings_are_per_instance(manager: MetricsManager, caplog: pytest.LogCaptureFixture) -> None:
    """Same-class provider instances warn independently (first and every 100th each)."""
    bad1 = FailingProvider()
    bad2 = FailingProvider()
    manager.register_provider(bad1)
    counter = manager.counter("r")
    with caplog.at_level(logging.WARNING, logger="bedrock.contrib.metric"):
        for _ in range(100):
            counter.inc()
        manager.register_provider(bad2)
        for _ in range(100):
            counter.inc()
    messages = [r.message for r in caplog.records if "failed in FailingProvider" in r.message]
    # bad1: occurrences 1, 100, 200; bad2: occurrences 1, 100 — five independent warnings.
    assert len(messages) == 5


def test_close_calls_provider_close(manager: MetricsManager) -> None:
    """close() releases every provider."""
    fake = FakeProvider()
    manager.register_provider(fake)
    manager.close()
    assert fake.closed is True
    assert manager.list_providers() == []


def test_close_resets_warning_state(manager: MetricsManager, caplog: pytest.LogCaptureFixture) -> None:
    """close() clears rate-limit state so a replacement provider starts fresh."""
    manager.register_provider(FailingProvider())
    counter = manager.counter("r")
    with caplog.at_level(logging.WARNING, logger="bedrock.contrib.metric"):
        for _ in range(100):
            counter.inc()
    assert manager._warning_counts != {}  # noqa: SLF001  # state accumulated
    manager.close()
    assert manager._warning_counts == {}  # noqa: SLF001  # state reset with provider retirement
    caplog.clear()
    manager.register_provider(FailingProvider())
    with caplog.at_level(logging.WARNING, logger="bedrock.contrib.metric"):
        counter.inc()
    messages = [r.message for r in caplog.records if "failed in FailingProvider" in r.message]
    assert len(messages) == 1
    assert "(1 occurrences)" in messages[0]


def test_module_singleton_and_helpers() -> None:
    """The module exposes the metrics singleton and delegating helpers; only original state is restored."""
    before_providers = list(metrics._providers)  # noqa: SLF001
    before_counts = dict(metrics._warning_counts)  # noqa: SLF001
    try:
        # Seed extra state on top of whatever existed before; assert relative changes.
        metrics.register_provider(FakeProvider())
        metrics._warning_counts[("seeded", "m")] = 7  # noqa: SLF001
        register_provider(FakeProvider())
        assert isinstance(metrics, MetricsManager)
        assert list_providers() == [type(p).__name__ for p in before_providers] + ["FakeProvider", "FakeProvider"]
    finally:
        metrics._providers.clear()  # noqa: SLF001
        metrics._providers.extend(before_providers)  # noqa: SLF001
        metrics._warning_counts.clear()  # noqa: SLF001
        metrics._warning_counts.update(before_counts)  # noqa: SLF001
    assert metrics.list_providers() == [type(p).__name__ for p in before_providers]
    assert metrics._warning_counts == before_counts
