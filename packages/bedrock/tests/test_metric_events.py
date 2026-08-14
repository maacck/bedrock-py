"""Tests for the metric event value object."""

from bedrock.contrib.metric.events import MetricEvent


def test_event_defaults() -> None:
    """A bare event carries type/name/value with empty tags and a UTC timestamp."""
    event = MetricEvent(type="counter", name="a.b", value=1.0)
    assert event.type == "counter"
    assert event.name == "a.b"
    assert event.value == 1.0
    assert event.tags == {}
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset() is not None


def test_event_is_frozen() -> None:
    """Events are immutable value objects."""
    event = MetricEvent(type="gauge", name="x", value=2.0)
    try:
        event.value = 3.0  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("MetricEvent must be immutable")


def test_event_tags_roundtrip() -> None:
    """Tags are preserved."""
    event = MetricEvent(type="timer", name="t", value=0.5, tags={"k": "v"})
    assert event.tags == {"k": "v"}
