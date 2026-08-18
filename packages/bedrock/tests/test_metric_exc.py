"""Tests for the metrics exception hierarchy."""

from bedrock.contrib.metric.exc import (
    MetricConfigurationError,
    MetricError,
    MetricProviderError,
)
from bedrock.exc import BedrockExc


def test_all_metric_exceptions_inherit_metric_error() -> None:
    """Every metric exception must derive from MetricError."""
    for exc_cls in (MetricConfigurationError, MetricProviderError):
        assert issubclass(exc_cls, MetricError)
        assert issubclass(exc_cls, BedrockExc)


def test_metric_error_sets_default_detail() -> None:
    """A bare MetricError carries its class-level detail message."""
    exc = MetricError()
    assert exc.detail
    assert str(exc) == exc.detail


def test_metric_error_msg_overrides_detail() -> None:
    """Passing msg to the constructor overrides the default detail."""
    exc = MetricConfigurationError(msg="Missing gateway_url.")
    assert exc.detail == "Missing gateway_url."
    assert str(exc) == exc.detail
