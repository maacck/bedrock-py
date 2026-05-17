"""Bedrock dependency injection container."""

from .container import Container
from .exc import DIError, DuplicateServiceError, ScopeError, ServiceNotFoundError
from .lifetime import Lifetime

container = Container()
provider = container.provider
inject = container.inject

__all__ = [
    "Container",
    "Lifetime",
    "container",
    "provider",
    "inject",
    "DIError",
    "ServiceNotFoundError",
    "DuplicateServiceError",
    "ScopeError",
]
