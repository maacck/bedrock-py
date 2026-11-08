"""Errors raised by provider registration, resolution, and creation."""

from ..common.registry.exc import DuplicateRegistrationError, InvalidRegisteredClassError
from ..exc import BedrockExc


class ProviderError(BedrockExc):
    """Base error for provider catalog operations."""

    detail: str = "Provider operation failed."


# ponytail: also inherits the ClassRegistry error so cache/storage `register_backend` callers that catch the 0.3.0
# type keep working; drop the second base in the next breaking release.
class InvalidProviderSpecError(ProviderError, InvalidRegisteredClassError):
    """Raised when a registration is malformed: empty name, bad import path, or non-callable factory."""

    detail: str = "Invalid provider registration."


# ponytail: same compatibility bridge as InvalidProviderSpecError.
class ProviderConflictError(ProviderError, DuplicateRegistrationError):
    """Raised when a different provider is already registered under the same name."""

    detail: str = "A different provider is already registered under this name."


class ProviderNotFoundError(ProviderError):
    """Raised when no provider is registered under the requested name."""

    detail: str = "No provider is registered under this name."


class ProviderLoadError(ProviderError):
    """Raised when a provider cannot be imported, resolved, or import its own dependencies."""

    detail: str = "The provider could not be loaded."


class InvalidProviderError(ProviderError):
    """Raised when a factory returns an object that does not implement the catalog protocol."""

    detail: str = "The provider does not implement the required protocol."
