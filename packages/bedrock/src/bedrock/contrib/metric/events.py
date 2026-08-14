"""Metric event value object."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

MetricType = Literal["counter", "gauge", "timer"]


@dataclass(frozen=True)
class MetricEvent:
    """One metric observation, broadcast to every registered provider.

    ``value`` is a number: counter increments, gauge current state, timer
    elapsed seconds. ``tags`` are opaque string attributes (StatsD tags /
    Sentry tags / Prometheus labels). ``timestamp`` is always UTC.
    """

    type: MetricType
    name: str
    value: float
    tags: dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
