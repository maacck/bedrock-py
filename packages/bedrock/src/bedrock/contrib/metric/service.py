"""Metrics manager: singleton facade with hardcoded logging and provider fan-out."""

import inspect
import logging
import time
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .base import MetricProvider
from .events import MetricEvent, MetricType

P = ParamSpec("P")
R = TypeVar("R")

_WARNING_INTERVAL = 100


class _Handle:
    """Base handle carrying name + tags; emits through the manager."""

    def __init__(self, manager: "MetricsManager", metric_type: MetricType, name: str, tags: dict[str, str]) -> None:
        self._manager = manager
        self._metric_type = metric_type
        self._name = name
        self._tags = tags

    def _emit(self, value: float) -> None:
        self._manager._emit(
            MetricEvent(type=self._metric_type, name=self._name, value=value, tags=self._tags)
        )


class Counter(_Handle):
    """A monotonic counter handle; usable as ``@counter`` decorator."""

    def inc(self, delta: int | float = 1) -> None:
        """Increment the counter by ``delta`` (default 1)."""
        self._emit(float(delta))

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Decorate ``func`` (sync or async): each call increments the counter by 1."""

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                try:
                    return await func(*args, **kwargs)
                finally:
                    self.inc()

            return async_wrapper

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return func(*args, **kwargs)
            finally:
                self.inc()

        return wrapper


class Gauge(_Handle):
    """A current-state gauge handle (no decorator)."""

    def set(self, value: int | float) -> None:
        """Set the gauge to ``value``."""
        self._emit(float(value))


class Timer(_Handle):
    """An elapsed-seconds timer: ``observe``, context manager, or decorator."""

    def observe(self, seconds: float) -> None:
        """Record an elapsed duration in seconds."""
        self._emit(float(seconds))

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.observe(time.perf_counter() - self._start)

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Decorate ``func`` (sync or async): record elapsed seconds per call."""

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                start = time.perf_counter()
                try:
                    return await func(*args, **kwargs)
                finally:
                    self.observe(time.perf_counter() - start)

            return async_wrapper

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                self.observe(time.perf_counter() - start)

        return wrapper


class MetricsManager:
    """Unified metrics facade with hardcoded logging and provider fan-out.

    The logger is built in and cannot be removed: every emit writes a log
    line (level via :meth:`set_log_level`, default INFO) before broadcasting
    to registered providers. Provider failures are swallowed with a
    rate-limited warning — instrumentation never raises into business code.
    """

    def __init__(self, log_level: int = logging.INFO) -> None:
        self._providers: list[MetricProvider] = []
        self._logger = logging.getLogger("bedrock.contrib.metric")
        self._log_level = log_level
        self._warning_counts: dict[tuple[int, str], int] = {}

    def set_log_level(self, level: int) -> None:
        """Set the hardcoded logger's level (e.g. ``logging.DEBUG`` on hot paths)."""
        self._log_level = level

    def register_provider(self, provider: MetricProvider) -> None:
        """Register a provider; every event is broadcast to all providers."""
        if not any(existing is provider for existing in self._providers):
            self._providers.append(provider)

    def list_providers(self) -> list[str]:
        """Return the registered provider class names, in order."""
        return [type(provider).__name__ for provider in self._providers]

    def counter(self, name: str, tags: dict[str, str] | None = None) -> Counter:
        """Return a counter handle for ``name``."""
        return Counter(self, "counter", name, dict(tags or {}))

    def gauge(self, name: str, tags: dict[str, str] | None = None) -> Gauge:
        """Return a gauge handle for ``name``."""
        return Gauge(self, "gauge", name, dict(tags or {}))

    def timer(self, name: str, tags: dict[str, str] | None = None) -> Timer:
        """Return a timer handle for ``name`` (decorator / context manager / observe)."""
        return Timer(self, "timer", name, dict(tags or {}))

    def _emit(self, event: MetricEvent) -> None:
        """Log the event (hardcoded), then fan out to every provider, isolating failures."""
        self._logger.log(
            self._log_level,
            "metric %s %s=%s tags=%s",
            event.type,
            event.name,
            event.value,
            event.tags,
        )
        for provider in self._providers:
            try:
                provider.emit(event)
            except Exception as exc:  # never BaseException
                self._warn_failure(provider, event, exc)

    def _warn_failure(self, provider: MetricProvider, event: MetricEvent, exc: Exception) -> None:
        key = (id(provider), event.name)
        count = self._warning_counts.get(key, 0) + 1
        self._warning_counts[key] = count
        if count == 1 or count % _WARNING_INTERVAL == 0:
            self._logger.warning(
                "metric %s failed in %s (%s occurrences): %s", event.name, type(provider).__name__, count, exc
            )

    def close(self) -> None:
        """Release provider resources. The framework never calls this automatically."""
        for provider in self._providers:
            try:
                provider.close()
            except Exception as exc:
                self._logger.warning("metric provider %s failed to close: %s", type(provider).__name__, exc)
        self._providers.clear()
        self._warning_counts.clear()


metrics = MetricsManager()


def register_provider(provider: MetricProvider) -> None:
    """Register a provider on the module singleton."""
    metrics.register_provider(provider)


def list_providers() -> list[str]:
    """List providers registered on the module singleton."""
    return metrics.list_providers()
