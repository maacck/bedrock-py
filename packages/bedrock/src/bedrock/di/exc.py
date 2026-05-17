"""DI container exception hierarchy."""

from bedrock.exc import BedrockExc


class DIError(BedrockExc):
    """Base exception for all dependency injection errors."""

    detail: str = "Dependency injection error."


class ServiceNotFoundError(DIError):
    """Raised when a requested service key has no registration."""

    detail: str = "Service not found in container."


class DuplicateServiceError(DIError):
    """Raised when attempting to register a key that already exists."""

    detail: str = "Service already registered in container."


class ScopeError(DIError):
    """Raised when a scoped service is resolved outside an active scope."""

    detail: str = "Scope operation error."
