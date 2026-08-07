"""Exception types for the Bedrock cache system."""

from __future__ import annotations

from ...exc import BedrockExc


class CacheError(BedrockExc):
    """Base exception for cache operation failures."""

    detail: str = "Cache operation failed."


class CacheConnectionError(CacheError):
    """Raised when the cache backend cannot establish a connection."""

    detail: str = "Failed to connect to the cache backend."


class CacheSerializationError(CacheError):
    """Raised when data cannot be serialized or deserialized."""

    detail: str = "Cache serialization error."


class BackendNotConfiguredError(CacheError):
    """Raised when the cache service is used before configuration."""

    detail: str = "Cache backend is not configured."


class CacheClearRequiresPrefixError(CacheError):
    """Raised when Redis cache clearing lacks a configured key namespace."""

    detail: str = (
        "Redis cache clear requires a non-empty CACHE_REDIS_KEY_PREFIX. "
        "Configure a dedicated prefix before clearing cache keys."
    )


class CacheLockError(CacheError):
    """Base exception for cache lock failures."""

    detail: str = "Cache lock operation failed."


class CacheLockAcquisitionError(CacheLockError):
    """Raised when a lock cannot be acquired."""

    detail: str = "Failed to acquire cache lock."


class CacheLockOwnershipError(CacheLockError):
    """Raised when releasing a lock that is no longer owned."""

    detail: str = "Cache lock is not owned by the current holder."
