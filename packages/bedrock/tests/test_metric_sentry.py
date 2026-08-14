"""Tests for the Sentry provider mapping."""

import importlib
import sys
import types

import pytest

from bedrock.contrib.metric.backends.sentry import SentryProvider
from bedrock.contrib.metric.events import MetricEvent
from bedrock.contrib.metric.exc import MetricProviderError


class FakeSentryMetrics:
    """Records sentry_sdk.metrics calls (current API: count/gauge/distribution)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list, dict]] = []

    def count(self, key: str, value: float, attributes: dict | None = None) -> None:
        self.calls.append(("count", [key, value], {"attributes": attributes or {}}))

    def gauge(self, key: str, value: float, attributes: dict | None = None) -> None:
        self.calls.append(("gauge", [key, value], {"attributes": attributes or {}}))

    def distribution(
        self, key: str, value: float, unit: str | None = None, attributes: dict | None = None
    ) -> None:
        self.calls.append(("distribution", [key, value], {"unit": unit, "attributes": attributes or {}}))


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> SentryProvider:
    """A SentryProvider backed by a recording fake ``sentry_sdk`` module."""
    fake = FakeSentryMetrics()
    fake_module = types.SimpleNamespace(metrics=fake)
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_module)
    return SentryProvider()


def test_counter_maps_to_count(provider: SentryProvider) -> None:
    """counter -> metrics.count with attributes passed through."""
    provider.emit(MetricEvent(type="counter", name="orders.total", value=3.0, tags={"c": "web"}))
    assert provider._sentry.metrics.calls[0] == ("count", ["orders.total", 3.0], {"attributes": {"c": "web"}})


def test_counter_preserves_float_value(provider: SentryProvider) -> None:
    """counter keeps the float value intact (no int truncation, no rounding)."""
    provider.emit(MetricEvent(type="counter", name="orders.total", value=2.5))
    provider.emit(MetricEvent(type="counter", name="orders.total", value=1.23456789))
    assert provider._sentry.metrics.calls[0][1] == ["orders.total", 2.5]
    assert provider._sentry.metrics.calls[1][1] == ["orders.total", 1.23456789]


def test_gauge_maps_to_gauge(provider: SentryProvider) -> None:
    """gauge -> metrics.gauge with attributes passed through."""
    provider.emit(MetricEvent(type="gauge", name="queue.size", value=42.0, tags={"pool": "workers"}))
    assert provider._sentry.metrics.calls[0] == (
        "gauge", ["queue.size", 42.0], {"attributes": {"pool": "workers"}}
    )


def test_gauge_preserves_float_value(provider: SentryProvider) -> None:
    """gauge keeps the float value intact."""
    provider.emit(MetricEvent(type="gauge", name="queue.size", value=1.23456789))
    assert provider._sentry.metrics.calls[0][1] == ["queue.size", 1.23456789]


def test_timer_maps_to_distribution_seconds(provider: SentryProvider) -> None:
    """timer -> metrics.distribution with unit='second'."""
    provider.emit(MetricEvent(type="timer", name="job.duration", value=1.5, tags={"op": "run"}))
    assert provider._sentry.metrics.calls[0] == (
        "distribution", ["job.duration", 1.5], {"unit": "second", "attributes": {"op": "run"}}
    )


def test_close_is_noop(provider: SentryProvider) -> None:
    """close() releases nothing; the Sentry SDK lifecycle is application-owned."""
    provider.close()  # must not raise
    provider.close()  # and is idempotent


def test_deferred_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """bedrock.contrib.metric imports without pulling in sentry_sdk."""
    monkeypatch.delitem(sys.modules, "sentry_sdk", raising=False)
    importlib.import_module("bedrock.contrib.metric")
    assert "sentry_sdk" not in sys.modules


def test_missing_extra_raises_metric_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing SentryProvider without sentry-sdk raises MetricProviderError."""
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    with pytest.raises(MetricProviderError) as excinfo:
        SentryProvider()
    assert "metric-sentry" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ImportError)
