"""Capability-level cache configuration."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class CacheSettings(BaseSettings):
    """Cache selection read from the environment; variables are prefixed with ``CACHE_``.

    Backend-specific options keep their own prefixes (``CACHE_MEMORY_``, ``CACHE_REDIS_``).

    Attributes:
        backend: Registered backend name used when :meth:`CacheService.configure` receives no name
            (``CACHE_BACKEND``).
    """

    model_config = SettingsConfigDict(env_prefix="CACHE_", extra="ignore")

    backend: str = "memory"
