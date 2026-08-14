"""Prometheus pushgateway provider."""

import time
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..events import MetricEvent, MetricType
from ..exc import MetricConfigurationError, MetricProviderError


class PrometheusPushSettings(BaseSettings):
    """Settings for the Prometheus pushgateway backend."""

    model_config = SettingsConfigDict(env_prefix="METRIC_PROMETHEUS_", extra="ignore")

    gateway_url: str = ""
    job: str = "bedrock-app"
    push_interval: float = 1.0


class PrometheusPushProvider:
    """Emit metrics to a Prometheus pushgateway.

    One collector is registered per (metric name, metric type); its label-key
    schema (the sorted set of tag keys) is fixed by the first observation and
    every later event for that metric reuses the same collector, calling
    ``labels(**tags)`` to select the child series for its values. A later
    event that changes the label-key schema raises ``ValueError`` — the
    manager isolates provider errors — instead of attempting a duplicate
    registry registration. Mapping: counter → Counter, gauge → Gauge, timer
    → Histogram (elapsed seconds). The registry is pushed to the gateway on
    the first event and then at most every ``push_interval`` seconds; there
    is no buffering, background push, or bootstrap. Requires the optional
    ``metric-prometheus`` extra; ``prometheus_client`` is imported lazily in
    ``__init__`` so this module (and ``bedrock.contrib.metric``) imports
    without the dependency.
    """

    def __init__(self, settings: PrometheusPushSettings | None = None) -> None:
        self._settings = settings or PrometheusPushSettings()
        if not self._settings.gateway_url:
            raise MetricConfigurationError(
                "PrometheusPushProvider requires gateway_url (set METRIC_PROMETHEUS_GATEWAY_URL "
                "or pass settings)."
            )
        try:
            import prometheus_client  # deferred: optional dependency
        except ImportError as exc:
            raise MetricProviderError(
                "The Prometheus provider requires the optional 'metric-prometheus' extra; "
                "install it with: pip install 'bedrock-core[metric-prometheus]'"
            ) from exc
        self._client: Any = prometheus_client
        self._registry: Any = prometheus_client.CollectorRegistry()
        # key: (name, metric_type) -> (collector, kind, label_keys)
        self._collectors: dict[tuple[str, MetricType], tuple[Any, str, tuple[str, ...]]] = {}
        self._last_push = 0.0

    def _collector(self, event: MetricEvent) -> tuple[Any, str]:
        """Return the (collector, kind) for ``event``, creating it on first observation.

        The label-key schema is established on first observation; a later
        event with a different schema for the same name/type raises a clear
        ``ValueError`` (never a duplicate registry registration).
        """
        key = (event.name, event.type)
        label_keys = tuple(sorted(event.tags))
        cached = self._collectors.get(key)
        if cached is not None:
            collector, kind, established = cached
            if established != label_keys:
                raise ValueError(
                    f"Prometheus metric {event.name!r} ({event.type}) was first observed with label "
                    f"keys {established!r} but an event uses {label_keys!r}; the label-key schema is "
                    "fixed on first observation."
                )
            return collector, kind
        if event.type == "counter":
            collector = self._client.Counter(event.name, "", labelnames=label_keys, registry=self._registry)
            kind = "counter"
        elif event.type == "gauge":
            collector = self._client.Gauge(event.name, "", labelnames=label_keys, registry=self._registry)
            kind = "gauge"
        else:  # timer -> histogram, seconds
            collector = self._client.Histogram(event.name, "", labelnames=label_keys, registry=self._registry)
            kind = "histogram"
        self._collectors[key] = (collector, kind, label_keys)
        return collector, kind

    def emit(self, event: MetricEvent) -> None:
        """Update the collector for ``event`` and push the registry (throttled)."""
        collector, kind = self._collector(event)
        labeled = collector.labels(**event.tags) if event.tags else collector
        if kind == "counter":
            labeled.inc(event.value)
        elif kind == "gauge":
            labeled.set(event.value)
        else:
            labeled.observe(event.value)
        now = time.monotonic()
        if now - self._last_push >= self._settings.push_interval:
            self._client.push_to_gateway(
                self._settings.gateway_url, job=self._settings.job, registry=self._registry
            )
            self._last_push = now

    def close(self) -> None:
        """Nothing to release (gateway pushes are per-emit)."""
        return None
