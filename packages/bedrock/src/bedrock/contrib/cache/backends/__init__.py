"""Cache backend implementations."""

from .memory import InMemoryBackend, MemoryCacheSettings

__all__ = [
    "InMemoryBackend",
    "MemoryCacheSettings",
    "get_available_backends",
]


def get_available_backends() -> list[str]:
    """Return list of registered backend names."""
    from ..base import _BACKEND_REGISTRY

    return sorted(_BACKEND_REGISTRY.keys())


def _initialize() -> None:
    """Import optional backends so they can self-register."""
    try:
        from .redis import RedisBackend, RedisCacheSettings  # noqa: F401

        __all__.extend(["RedisBackend", "RedisCacheSettings"])
    except ImportError:
        pass

    try:
        from .memcached import MemcachedBackend, MemcachedCacheSettings  # noqa: F401

        __all__.extend(["MemcachedBackend", "MemcachedCacheSettings"])
    except ImportError:
        pass


_initialize()
