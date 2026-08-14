"""Bedrock Metrics — application metrics instrumentation with pluggable providers."""

from .backends.statsd import StatsDProvider, StatsDSettings
from .base import MetricProvider
from .events import MetricEvent, MetricType
from .exc import MetricConfigurationError, MetricError, MetricProviderError
from .service import Counter, Gauge, MetricsManager, Timer, list_providers, metrics, register_provider

__all__ = [
    "Counter",
    "Gauge",
    "MetricConfigurationError",
    "MetricError",
    "MetricEvent",
    "MetricProvider",
    "MetricProviderError",
    "MetricType",
    "MetricsManager",
    "StatsDProvider",
    "StatsDSettings",
    "Timer",
    "list_providers",
    "metrics",
    "register_provider",
]
