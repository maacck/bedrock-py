"""Metrics module exceptions."""

from bedrock.exc import BedrockExc


class MetricError(BedrockExc):
    """Base exception for the metrics contrib module."""

    detail: str = "Metrics operation failed."


class MetricConfigurationError(MetricError):
    """Raised when a metrics provider is configured incorrectly."""

    detail: str = "Invalid metrics provider configuration."


class MetricProviderError(MetricError):
    """Raised when a metrics provider cannot be initialized."""

    detail: str = "Failed to initialize the metrics provider."
