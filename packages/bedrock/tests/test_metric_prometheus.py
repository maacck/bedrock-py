"""Tests for the Prometheus pushgateway provider."""

import importlib
import sys
import types

import pytest

from bedrock.contrib.metric.backends.prometheus_push import PrometheusPushProvider, PrometheusPushSettings
from bedrock.contrib.metric.events import MetricEvent
from bedrock.contrib.metric.exc import MetricConfigurationError, MetricProviderError


class FakeRegistry:
    """Captures pushed payloads instead of touching a real gateway."""

    def __init__(self) -> None:
        self.pushed: list[tuple[str, str, object]] = []

    def push(self, gateway: str, job: str, registry: object) -> None:
        self.pushed.append((gateway, job, registry))


class _FakeCounter:
    """Recording fake for prometheus_client.Counter (labels -> inc)."""

    instances = 0

    def __init__(self) -> None:
        _FakeCounter.instances += 1
        self.labels_calls: list[dict[str, str]] = []
        self.inc_values: list[float] = []

    def labels(self, **tags: str) -> "_FakeCounter":
        self.labels_calls.append(tags)
        return self

    def inc(self, value: float) -> None:
        self.inc_values.append(value)


class _FakeGauge:
    """Recording fake for prometheus_client.Gauge (labels -> set)."""

    instances = 0

    def __init__(self) -> None:
        _FakeGauge.instances += 1
        self.labels_calls: list[dict[str, str]] = []
        self.set_values: list[float] = []

    def labels(self, **tags: str) -> "_FakeGauge":
        self.labels_calls.append(tags)
        return self

    def set(self, value: float) -> None:
        self.set_values.append(value)


class _FakeHistogram:
    """Recording fake for prometheus_client.Histogram (labels -> observe)."""

    instances = 0

    def __init__(self) -> None:
        _FakeHistogram.instances += 1
        self.labels_calls: list[dict[str, str]] = []
        self.observe_values: list[float] = []

    def labels(self, **tags: str) -> "_FakeHistogram":
        self.labels_calls.append(tags)
        return self

    def observe(self, value: float) -> None:
        self.observe_values.append(value)


@pytest.fixture(autouse=True)
def _reset_fakes() -> None:
    """Each test starts with zero collectors constructed."""
    _FakeCounter.instances = 0
    _FakeGauge.instances = 0
    _FakeHistogram.instances = 0


@pytest.fixture
def make_provider(monkeypatch: pytest.MonkeyPatch):
    """Build a provider backed by recording fakes; returns (provider, registry)."""

    def _make(**settings_overrides: object) -> tuple[PrometheusPushProvider, FakeRegistry]:
        registry = FakeRegistry()
        fake_client = types.SimpleNamespace(
            Counter=lambda *args, **kwargs: _FakeCounter(),
            Gauge=lambda *args, **kwargs: _FakeGauge(),
            Histogram=lambda *args, **kwargs: _FakeHistogram(),
            CollectorRegistry=lambda: registry,
            push_to_gateway=registry.push,
        )
        monkeypatch.setitem(sys.modules, "prometheus_client", fake_client)
        settings = PrometheusPushSettings(gateway_url="http://gw:9091", **settings_overrides)
        return PrometheusPushProvider(settings=settings), registry

    return _make


@pytest.fixture
def provider(make_provider) -> PrometheusPushProvider:
    """A provider with push_interval 0.0 (every emit pushes)."""
    return make_provider(push_interval=0.0)[0]


def test_settings_defaults() -> None:
    """Default settings: gateway_url empty, job bedrock-app, 1.0s interval."""
    settings = PrometheusPushSettings()
    assert settings.gateway_url == ""
    assert settings.job == "bedrock-app"
    assert settings.push_interval == 1.0


def test_settings_read_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """METRIC_PROMETHEUS_* env vars override settings."""
    monkeypatch.setenv("METRIC_PROMETHEUS_GATEWAY_URL", "http://env-gw:9091")
    monkeypatch.setenv("METRIC_PROMETHEUS_JOB", "env-job")
    monkeypatch.setenv("METRIC_PROMETHEUS_UNKNOWN", "ignored")
    settings = PrometheusPushSettings()
    assert settings.gateway_url == "http://env-gw:9091"
    assert settings.job == "env-job"
    assert settings.push_interval == 1.0  # unknown env vars are ignored


def test_missing_gateway_url_raises_configuration_error() -> None:
    """Construction without gateway_url fails fast with a human-readable error."""
    with pytest.raises(MetricConfigurationError, match="gateway_url"):
        PrometheusPushProvider(settings=PrometheusPushSettings())


def test_counter_maps_to_counter(provider: PrometheusPushProvider) -> None:
    """counter -> Counter collector; inc receives the event value."""
    provider.emit(MetricEvent(type="counter", name="orders.total", value=2.0, tags={"c": "web"}))
    collector, kind, label_keys = provider._collectors["orders.total"]
    assert kind == "counter"
    assert label_keys == ("c",)
    assert isinstance(collector, _FakeCounter)
    assert collector.labels_calls == [{"c": "web"}]
    assert collector.inc_values == [2.0]


def test_gauge_maps_to_gauge(provider: PrometheusPushProvider) -> None:
    """gauge -> Gauge collector; set receives the event value."""
    provider.emit(MetricEvent(type="gauge", name="queue.size", value=5.0, tags={"pool": "workers"}))
    collector, kind, label_keys = provider._collectors["queue.size"]
    assert kind == "gauge"
    assert label_keys == ("pool",)
    assert isinstance(collector, _FakeGauge)
    assert collector.labels_calls == [{"pool": "workers"}]
    assert collector.set_values == [5.0]


def test_timer_maps_to_histogram_seconds(provider: PrometheusPushProvider) -> None:
    """timer -> Histogram collector; observe receives elapsed seconds."""
    provider.emit(MetricEvent(type="timer", name="job.duration", value=0.5))
    collector, kind, label_keys = provider._collectors["job.duration"]
    assert kind == "histogram"
    assert label_keys == ()
    assert isinstance(collector, _FakeHistogram)
    assert collector.labels_calls == []
    assert collector.observe_values == [0.5]


def test_same_label_schema_reuses_one_collector(provider: PrometheusPushProvider) -> None:
    """Same name/type/label-key schema -> one collector; values create child series."""
    provider.emit(MetricEvent(type="counter", name="c", value=1.0, tags={"k": "v1"}))
    provider.emit(MetricEvent(type="counter", name="c", value=2.0, tags={"k": "v2"}))
    assert _FakeCounter.instances == 1  # registered exactly once, never duplicated
    collector = provider._collectors["c"][0]
    assert collector.labels_calls == [{"k": "v1"}, {"k": "v2"}]
    assert collector.inc_values == [1.0, 2.0]


def test_label_key_schema_is_sorted_key_based(provider: PrometheusPushProvider) -> None:
    """Tag insertion order does not matter; the key schema is sorted tag keys."""
    provider.emit(MetricEvent(type="gauge", name="g", value=1.0, tags={"b": "1", "a": "1"}))
    provider.emit(MetricEvent(type="gauge", name="g", value=2.0, tags={"a": "2", "b": "2"}))
    assert _FakeGauge.instances == 1
    collector = provider._collectors["g"][0]
    assert collector.labels_calls == [{"b": "1", "a": "1"}, {"a": "2", "b": "2"}]


def test_mismatched_label_schema_raises_without_duplicate_registration(
    make_provider,
) -> None:
    """A different label-key schema for the same name/type raises ValueError, never re-registers."""
    provider, registry = make_provider(push_interval=0.0)
    provider.emit(MetricEvent(type="counter", name="c", value=1.0, tags={"a": "1"}))
    with pytest.raises(ValueError, match="label"):
        provider.emit(MetricEvent(type="counter", name="c", value=1.0, tags={"b": "2"}))
    assert _FakeCounter.instances == 1  # no second registration was attempted
    assert len(registry.pushed) == 1  # the failing event never reached the gateway


def test_type_change_raises_without_duplicate_registration(make_provider) -> None:
    """A different metric type for the same name raises ValueError, never re-registers."""
    provider, registry = make_provider(push_interval=0.0)
    provider.emit(MetricEvent(type="counter", name="c", value=1.0, tags={"a": "1"}))
    with pytest.raises(ValueError, match="type"):
        provider.emit(MetricEvent(type="gauge", name="c", value=2.0, tags={"a": "2"}))
    assert _FakeCounter.instances == 1  # only the counter was ever constructed
    assert _FakeGauge.instances == 0  # the gauge was never constructed (no duplicate registration)
    assert len(registry.pushed) == 1  # the rejected event never reached the gateway


def test_first_event_pushes_with_gateway_and_job(make_provider) -> None:
    """The first emit pushes to gateway_url with the configured job."""
    provider, registry = make_provider(push_interval=10.0)
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    assert len(registry.pushed) == 1
    assert registry.pushed[0] == ("http://gw:9091", "bedrock-app", provider._registry)


def test_first_event_pushes_even_when_monotonic_below_interval(
    monkeypatch: pytest.MonkeyPatch, make_provider
) -> None:
    """A never-pushed provider always pushes the first event, even if monotonic < interval."""
    monkeypatch.setattr("time.monotonic", lambda: 0.25)
    provider, registry = make_provider(push_interval=1.0)
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    assert len(registry.pushed) == 1  # first event is never throttled
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    assert len(registry.pushed) == 1  # 0.0s elapsed < 1.0s interval -> suppressed


def test_push_throttled_until_interval_elapses(
    monkeypatch: pytest.MonkeyPatch, make_provider
) -> None:
    """Later pushes are suppressed until push_interval has elapsed since the last push."""
    ticks = iter([100.0, 101.0, 120.0, 121.0])
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    provider, registry = make_provider(push_interval=10.0)
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))  # t=100 -> push
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))  # t=101 -> suppressed
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))  # t=120 -> push
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))  # t=121 -> suppressed
    assert len(registry.pushed) == 2


def test_zero_interval_pushes_every_event(provider: PrometheusPushProvider) -> None:
    """push_interval 0.0 pushes on every emit."""
    registry = provider._registry
    assert isinstance(registry, FakeRegistry)
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    assert len(registry.pushed) == 3


def test_close_is_noop(make_provider) -> None:
    """close() releases nothing and is idempotent (gateway pushes are per-emit)."""
    provider, _ = make_provider(push_interval=10.0)
    provider.close()
    provider.close()


def test_deferred_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """bedrock.contrib.metric imports without pulling in prometheus_client."""
    monkeypatch.delitem(sys.modules, "prometheus_client", raising=False)
    importlib.import_module("bedrock.contrib.metric")
    assert "prometheus_client" not in sys.modules


def test_missing_extra_raises_metric_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing the provider without prometheus-client raises MetricProviderError."""
    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    with pytest.raises(MetricProviderError) as excinfo:
        PrometheusPushProvider(settings=PrometheusPushSettings(gateway_url="http://gw:9091"))
    assert "metric-prometheus" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ImportError)
