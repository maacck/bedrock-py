"""Public Bedrock cache API.

Usage::

    from bedrock.contrib.cache import cache

    cache.set("key", "value", ttl=300)
    value = cache.get("key")

    await cache.aset("key", "value")
    value = await cache.aget("key")
"""

from __future__ import annotations

from .base import CacheBackend
from .coder import CacheCoder
from .exc import (
    BackendNotConfiguredError,
    CacheConnectionError,
    CacheError,
    CacheLockAcquisitionError,
    CacheLockError,
    CacheLockOwnershipError,
    CacheSerializationError,
)
from .lock import CacheLock
from .schema import CacheNamespace, CacheSlot
from .service import CacheService, cache, register_backend

__all__ = [
    "BackendNotConfiguredError",
    "CacheBackend",
    "CacheCoder",
    "CacheConnectionError",
    "CacheError",
    "CacheLock",
    "CacheLockAcquisitionError",
    "CacheLockError",
    "CacheLockOwnershipError",
    "CacheNamespace",
    "CacheSerializationError",
    "CacheService",
    "CacheSlot",
    "cache",
    "register_backend",
]
