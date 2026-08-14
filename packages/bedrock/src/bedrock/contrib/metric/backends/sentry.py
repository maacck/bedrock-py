"""Sentry provider — forwards metrics to sentry_sdk.metrics."""

import types

from ..events import MetricEvent
from ..exc import MetricProviderError


class SentryProvider:
    """Emit metrics through ``sentry_sdk.metrics``.

    Mapping: counter → ``count``, gauge → ``gauge``, timer →
    ``distribution`` (unit ``"second"``). Attributes (tags) are passed
    through unchanged; float values are preserved as-is. Requires the
    optional ``metric-sentry`` extra; ``sentry_sdk`` is imported lazily in
    ``__init__`` so the module imports without the dependency. The Sentry SDK
    lifecycle is application-owned, so ``close`` releases nothing.
    """

    def __init__(self) -> None:
        try:
            import sentry_sdk  # deferred: optional dependency
        except ImportError as exc:
            raise MetricProviderError(
                "The Sentry provider requires the optional 'metric-sentry' extra; "
                "install it with: pip install 'bedrock-core[metric-sentry]'"
            ) from exc
        self._sentry: types.ModuleType = sentry_sdk

    def emit(self, event: MetricEvent) -> None:
        """Forward ``event`` to the Sentry metrics API."""
        attributes = dict(event.tags)
        if event.type == "counter":
            self._sentry.metrics.count(event.name, event.value, attributes=attributes)
        elif event.type == "gauge":
            self._sentry.metrics.gauge(event.name, event.value, attributes=attributes)
        else:  # timer
            self._sentry.metrics.distribution(event.name, event.value, unit="second", attributes=attributes)

    def close(self) -> None:
        """Nothing to release (sentry_sdk is application-managed)."""
        return None
