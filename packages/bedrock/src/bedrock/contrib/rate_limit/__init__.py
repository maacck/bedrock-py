"""Public Bedrock rate-limit API.

Usage::

    from datetime import timedelta
    from bedrock.contrib.rate_limit import (
        RateLimitPolicy,
        rate_limit,
    )

    LOGIN = RateLimitPolicy(
        name="idp.login", limit=5, period=timedelta(minutes=15)
    )

    decision = rate_limit.hit(LOGIN, f"{tenant_id}:{email}")
    decision = await rate_limit.ahit(LOGIN, key, cost=1)
    rate_limit.reset(LOGIN, key)
"""

from typing import TYPE_CHECKING

from .base import RateLimitBackend
from .entities import RateLimitDecision, RateLimitPolicy
from .exc import (
    InvalidRateLimitArgumentError,
    InvalidRateLimitPolicyError,
    RateLimitBackendError,
    RateLimitBackendNotConfiguredError,
    RateLimitCloseError,
    RateLimitError,
    RateLimitPolicyConflictError,
)
from .service import RateLimitService, list_backends, rate_limit, register_backend, register_backend_lazy
from .settings import RateLimitSettings

if TYPE_CHECKING:
    from .backends.redis import RedisRateLimitBackend, RedisRateLimitSettings

_LAZY_EXPORTS = {
    "RedisRateLimitBackend": ".backends.redis",
    "RedisRateLimitSettings": ".backends.redis",
}


def __getattr__(name: str) -> object:
    """Import backend classes on first access so importing the package loads no backend module."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)


__all__ = [
    # Exceptions
    "InvalidRateLimitArgumentError",
    "InvalidRateLimitPolicyError",
    "RateLimitBackendError",
    "RateLimitBackendNotConfiguredError",
    "RateLimitCloseError",
    "RateLimitError",
    "RateLimitPolicyConflictError",
    # Entities
    "RateLimitDecision",
    "RateLimitPolicy",
    # Backends and settings
    "RateLimitBackend",
    "RateLimitSettings",
    "RedisRateLimitBackend",
    "RedisRateLimitSettings",
    # Service and helpers
    "RateLimitService",
    "list_backends",
    "register_backend",
    "register_backend_lazy",
    # Singleton
    "rate_limit",
]
