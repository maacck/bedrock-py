"""Exception types for the Bedrock rate-limit system."""

from __future__ import annotations

from ...exc import BedrockExc


class RateLimitError(BedrockExc):
    """Base exception for rate-limit failures."""

    detail: str = "Rate limit operation failed."


class InvalidRateLimitPolicyError(RateLimitError):
    """Raised when a :class:`RateLimitPolicy` is constructed with invalid values."""

    detail: str = "Invalid rate limit policy."


class InvalidRateLimitArgumentError(RateLimitError):
    """Raised when a rate-limit call receives an invalid key or cost."""

    detail: str = "Invalid rate limit argument."


class RateLimitPolicyConflictError(RateLimitError):
    """Raised when two different policies are used under the same name in one service."""

    detail: str = "Another rate limit policy already uses this name."


class RateLimitBackendError(RateLimitError):
    """Raised when the backend cannot evaluate a limit, e.g. Redis is unreachable.

    The message never includes the underlying error; it is attached as ``__cause__`` for logging.
    """

    detail: str = "Rate limit backend is unavailable."


class RateLimitBackendNotConfiguredError(RateLimitError):
    """Raised when the configured backend name is unknown or cannot be loaded."""

    detail: str = "Rate limit backend is not configured."


class RateLimitCloseError(RateLimitError):
    """Raised when closing one or more rate-limit backends failed; every backend was still attempted."""

    detail: str = "Failed to close the rate limit backend."
