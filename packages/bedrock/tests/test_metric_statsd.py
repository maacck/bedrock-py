"""Tests for the StatsD provider wire format."""

import socket

from bedrock.contrib.metric.backends.statsd import StatsDProvider, StatsDSettings
from bedrock.contrib.metric.events import MetricEvent


class FakeSocket:
    """Records sent payloads instead of touching the network."""

    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, data: bytes, addr: tuple[str, int]) -> int:
        self.sent.append((data, addr))
        return len(data)

    def close(self) -> None:
        self.closed = True


def test_counter_format(monkeypatch) -> None:
    """counter -> name:value|c."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings(host="h", port=8125))
    provider.emit(MetricEvent(type="counter", name="a.b", value=2.0))
    (data, addr), = fake.sent
    assert data == b"a.b:2|c"
    assert addr == ("h", 8125)
    provider.close()


def test_counter_preserves_fractional_delta(monkeypatch) -> None:
    """counter preserves fractional deltas instead of truncating."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings())
    provider.emit(MetricEvent(type="counter", name="c", value=2.5))
    assert fake.sent[0][0] == b"c:2.5|c"
    provider.close()


def test_gauge_format(monkeypatch) -> None:
    """gauge -> name:value|g."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings())
    provider.emit(MetricEvent(type="gauge", name="g", value=42.0))
    assert fake.sent[0][0] == b"g:42|g"
    provider.close()


def test_timer_converts_to_milliseconds(monkeypatch) -> None:
    """timer seconds -> integer ms -> name:value|ms."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings())
    provider.emit(MetricEvent(type="timer", name="t", value=1.5))
    assert fake.sent[0][0] == b"t:1500|ms"
    provider.close()


def test_tags_datadog_syntax_sorted(monkeypatch) -> None:
    """tags -> |#k:v,k2:v2 with sorted keys."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings())
    provider.emit(MetricEvent(type="counter", name="c", value=1.0, tags={"b": "2", "a": "1"}))
    assert fake.sent[0][0] == b"c:1|c|#a:1,b:2"
    provider.close()


def test_prefix_applied(monkeypatch) -> None:
    """prefix is prepended to the metric name."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings(prefix="app."))
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    assert fake.sent[0][0] == b"app.c:1|c"
    provider.close()


def test_lazy_socket_creation(monkeypatch) -> None:
    """socket is only created on first emit, not at construction."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings())
    assert provider._socket is None
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    assert provider._socket is fake
    provider.close()


def test_close_closes_socket(monkeypatch) -> None:
    """close() releases the UDP socket."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings())
    provider.emit(MetricEvent(type="counter", name="c", value=1.0))
    provider.close()
    assert fake.closed is True


def test_close_idempotent(monkeypatch) -> None:
    """close() may be called multiple times safely."""
    fake = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *a, **k: fake)
    provider = StatsDProvider(settings=StatsDSettings())
    provider.close()
    provider.close()
    assert fake.closed is False
