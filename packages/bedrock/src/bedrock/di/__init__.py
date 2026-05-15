"""Bedrock dependency injection container."""

from .container import Container
from .decorators import inject, provider
from .exc import DIError, DuplicateServiceError, ScopeError, ServiceNotFoundError
from .lifetime import Lifetime

container = Container()

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
