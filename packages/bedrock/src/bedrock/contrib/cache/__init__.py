"""Public Bedrock cache API.

Usage::

    from bedrock.contrib.cache import cache

    cache.set("key", "value", ttl=300)
    value = cache.get("key")

    await cache.aset("key", "value")
    value = await cache.aget("key")
"""

from __future__ import annotations

from . import backends
from .backends import (
    InMemoryBackend,
    MemoryCacheSettings,
    get_available_backends,
)
from .base import CacheBackend
from .coder import BytesCoder, CacheCoder
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
    "BytesCoder",
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
    "InMemoryBackend",
    "MemoryCacheSettings",
    "backends",
    "cache",
    "get_available_backends",
    "register_backend",
]


def __getattr__(name: str):
    if name in ("RedisBackend", "RedisCacheSettings"):
        from .backends.redis import RedisBackend, RedisCacheSettings

        globals()[name] = locals().get(name) or (RedisBackend if name == "RedisBackend" else RedisCacheSettings)
        return globals()[name]

    if name in ("MemcachedBackend", "MemcachedCacheSettings"):
        from .backends.memcached import MemcachedBackend, MemcachedCacheSettings

        globals()[name] = locals().get(name) or (
            MemcachedBackend if name == "MemcachedBackend" else MemcachedCacheSettings
        )
        return globals()[name]

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
