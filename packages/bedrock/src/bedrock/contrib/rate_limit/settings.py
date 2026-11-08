"""Capability-level rate-limit configuration."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class RateLimitSettings(BaseSettings):
    """Rate-limit backend selection read from the environment; variables are prefixed with ``RATE_LIMIT_``.

    Backend-specific options keep their own prefixes (``RATE_LIMIT_REDIS_``).

    Attributes:
        backend: Registered backend name used when :meth:`RateLimitService.configure` receives no name
            (``RATE_LIMIT_BACKEND``).
    """

    model_config = SettingsConfigDict(env_prefix="RATE_LIMIT_", extra="ignore")

    backend: str = "redis"
