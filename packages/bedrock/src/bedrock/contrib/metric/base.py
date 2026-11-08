"""Metrics provider protocol."""

from typing import Protocol

from .events import MetricEvent


class MetricProvider(Protocol):
    """Contract every metrics provider must implement.

    ``emit`` receives every metric event (fan-out); it MUST NOT raise into
    the caller — the manager wraps calls in try/except, but providers should
    still fail softly. ``close`` releases provider resources.

    A provider may also define any of these optional methods, which the manager discovers by name on
    shutdown: ``flush()`` (called before ``close``), and the coroutine functions ``aflush()`` and
    ``aclose()`` for providers that own asynchronous clients. ``aflush`` is awaited before ``aclose``.
    """

    def emit(self, event: MetricEvent) -> None:
        """Record ``event`` with the external system."""
        ...

    def close(self) -> None:
        """Release provider resources (e.g. sockets)."""
        ...
