"""In-memory cache backend implementation."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..base import CacheBackend
from ..entities import CacheEntry


class MemoryCacheSettings(BaseSettings):
    """Settings for the in-memory cache backend.

    Attributes:
        max_size: Maximum number of entries before eviction. Zero means unlimited.
        default_ttl: Default TTL in seconds for cached entries.
    """

    model_config = SettingsConfigDict(env_prefix="CACHE_MEMORY_", extra="ignore")

    max_size: int = 10000
    default_ttl: int = 300


class InMemoryBackend(CacheBackend):
    """Thread-safe in-memory cache backend with TTL support.

    Client (the internal store and locks) is created at instantiation time.

    Args:
        settings: Configuration for this backend.
    """

    def __init__(self, settings: MemoryCacheSettings | None = None) -> None:
        self._settings = settings or MemoryCacheSettings()
        self._store: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        self._sync_lock = threading.RLock()

    @property
    def settings(self) -> MemoryCacheSettings:
        """Return the settings used to configure this backend."""
        return self._settings

    # -- Core operations --------------------------------------------------

    @staticmethod
    def _resolve_expires_at(ex: int | None = None, ea: float | None = None) -> float | None:
        if ex is not None:
            return time.monotonic() + ex
        if ea is not None:
            return ea
        return None

    def get(self, key: str, default: bytes | None = None) -> bytes | None:
        """Retrieve a cached value by key."""
        entry = self._store.get(key)
        if entry is None:
            return default
        if entry.is_expired:
            del self._store[key]
            return default
        return entry.value

    async def aget(self, key: str, default: bytes | None = None) -> bytes | None:
        """Asynchronous variant of :meth:`get`."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return default
            if entry.is_expired:
                del self._store[key]
                return default
            return entry.value

    def get_with_ttl(self, key: str) -> tuple[bytes | None, int | None]:
        """Retrieve a cached value along with its remaining TTL."""
        entry = self._store.get(key)
        if entry is None:
            return None, None
        if entry.is_expired:
            del self._store[key]
            return None, None
        if entry.expires_at is None:
            return entry.value, None
        remaining = int(entry.expires_at - time.monotonic())
        return entry.value, max(remaining, 0)

    async def aget_with_ttl(self, key: str) -> tuple[bytes | None, int | None]:
        """Asynchronous variant of :meth:`get_with_ttl`."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, None
            if entry.is_expired:
                del self._store[key]
                return None, None
            if entry.expires_at is None:
                return entry.value, None
            remaining = int(entry.expires_at - time.monotonic())
            return entry.value, max(remaining, 0)

    def set(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> None:
        """Store a value in the cache."""
        expires_at = self._resolve_expires_at(ex=ex, ea=ea)
        self._store[key] = CacheEntry(value=value, expires_at=expires_at)
        if self._settings.max_size > 0 and len(self._store) > self._settings.max_size:
            self._evict()

    async def aset(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> None:
        """Asynchronous variant of :meth:`set`."""
        expires_at = self._resolve_expires_at(ex=ex, ea=ea)
        async with self._lock:
            self._store[key] = CacheEntry(value=value, expires_at=expires_at)
            if self._settings.max_size > 0 and len(self._store) > self._settings.max_size:
                self._evict_locked()

    def add(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> bool:
        """Store a value only when the key does not already exist."""
        expires_at = self._resolve_expires_at(ex=ex, ea=ea)
        with self._sync_lock:
            entry = self._store.get(key)
            if entry is not None and not entry.is_expired:
                return False
            if entry is not None and entry.is_expired:
                del self._store[key]
            self._store[key] = CacheEntry(value=value, expires_at=expires_at)
            if self._settings.max_size > 0 and len(self._store) > self._settings.max_size:
                self._evict()
            return True

    async def aadd(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> bool:
        """Asynchronous variant of :meth:`add`."""
        expires_at = self._resolve_expires_at(ex=ex, ea=ea)
        async with self._lock:
            entry = self._store.get(key)
            if entry is not None and not entry.is_expired:
                return False
            if entry is not None and entry.is_expired:
                del self._store[key]
            self._store[key] = CacheEntry(value=value, expires_at=expires_at)
            if self._settings.max_size > 0 and len(self._store) > self._settings.max_size:
                self._evict_locked()
            return True

    def delete(self, key: str) -> bool:
        """Remove a key from the cache."""
        entry = self._store.get(key)
        if entry is None:
            return False
        if entry.is_expired:
            del self._store[key]
            return False
        del self._store[key]
        return True

    async def adelete(self, key: str) -> bool:
        """Asynchronous variant of :meth:`delete`."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            if entry.is_expired:
                del self._store[key]
                return False
            del self._store[key]
            return True

    def compare_and_delete(self, key: str, expected: bytes) -> bool:
        """Delete a key only when its current value matches *expected*."""
        with self._sync_lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            if entry.is_expired:
                del self._store[key]
                return False
            if entry.value != expected:
                return False
            del self._store[key]
            return True

    async def acompare_and_delete(self, key: str, expected: bytes) -> bool:
        """Asynchronous variant of :meth:`compare_and_delete`."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            if entry.is_expired:
                del self._store[key]
                return False
            if entry.value != expected:
                return False
            del self._store[key]
            return True

    def exists(self, key: str) -> bool:
        """Check whether a key exists in the cache."""
        entry = self._store.get(key)
        if entry is None:
            return False
        if entry.is_expired:
            del self._store[key]
            return False
        return True

    async def aexists(self, key: str) -> bool:
        """Asynchronous variant of :meth:`exists`."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False
            if entry.is_expired:
                del self._store[key]
                return False
            return True

    def clear(self, prefix: str | None = None) -> int:
        """Remove all entries from the cache, optionally filtered by prefix."""
        if prefix is None:
            count = len(self._store)
            self._store.clear()
            return count
        to_delete = [k for k in self._store if k.startswith(prefix)]
        for k in to_delete:
            del self._store[k]
        return len(to_delete)

    async def aclear(self, prefix: str | None = None) -> int:
        """Asynchronous variant of :meth:`clear`."""
        async with self._lock:
            if prefix is None:
                count = len(self._store)
                self._store.clear()
                return count
            to_delete = [k for k in self._store if k.startswith(prefix)]
            for k in to_delete:
                del self._store[k]
            return len(to_delete)

    # -- Batch operations -------------------------------------------------

    def get_many(self, keys: list[str]) -> dict[str, bytes]:
        """Retrieve multiple keys in one call."""
        result: dict[str, bytes] = {}
        for key in keys:
            entry = self._store.get(key)
            if entry is not None and not entry.is_expired:
                result[key] = entry.value
            elif entry is not None and entry.is_expired:
                del self._store[key]
        return result

    async def aget_many(self, keys: list[str]) -> dict[str, bytes]:
        """Asynchronous variant of :meth:`get_many`."""
        async with self._lock:
            result: dict[str, bytes] = {}
            for key in keys:
                entry = self._store.get(key)
                if entry is not None and not entry.is_expired:
                    result[key] = entry.value
                elif entry is not None and entry.is_expired:
                    del self._store[key]
            return result

    def set_many(self, mapping: dict[str, bytes], ex: int | None = None, ea: float | None = None) -> None:
        """Store multiple key-value pairs in one call."""
        for key, value in mapping.items():
            self.set(key, value, ex=ex, ea=ea)

    async def aset_many(self, mapping: dict[str, bytes], ex: int | None = None, ea: float | None = None) -> None:
        """Asynchronous variant of :meth:`set_many`."""
        for key, value in mapping.items():
            await self.aset(key, value, ex=ex, ea=ea)

    # -- Convenience operations -------------------------------------------

    def get_or_set(
        self, key: str, default_provider: callable[[], bytes] | bytes, ex: int | None = None, ea: float | None = None
    ) -> bytes | None:
        """Get a cached value, or set and return ``default`` if missing."""
        value = self.get(key)
        if value is not None:
            return value
        value = default_provider() if callable(default_provider) else default_provider
        self.set(key, value, ex=ex, ea=ea)
        return value

    async def aget_or_set(
        self,
        key: str,
        default_provider: callable[[], bytes] | bytes | callable[[], Awaitable[bytes]],
        ex: int | None = None,
        ea: float | None = None,
    ) -> bytes | None:
        """Asynchronous variant of :meth:`get_or_set`."""
        async with self._lock:
            value = await self._aget_locked(key)
            if value is not None:
                return value
            if callable(default_provider):
                if asyncio.iscoroutinefunction(default_provider):
                    value = await default_provider()
                else:
                    value = default_provider()
            else:
                value = default_provider
            await self._aset_locked(key, value, ex=ex, ea=ea)
            return value

    # -- Key listing ------------------------------------------------------

    def list(self, prefix: str, limit: int = 100) -> list[str]:
        """List cache keys matching a prefix."""
        return [k for k in self._store if k.startswith(prefix)][:limit]

    async def alist(self, prefix: str, limit: int = 100) -> list[str]:
        """Asynchronous variant of :meth:`list`."""
        async with self._lock:
            return [k for k in self._store if k.startswith(prefix)][:limit]

    # -- Counters ---------------------------------------------------------

    def incr(self, key: str, delta: int = 1) -> int:
        """Increment a numeric cache value."""
        entry = self._store.get(key)
        if entry is None or entry.is_expired:
            if entry is not None and entry.is_expired:
                del self._store[key]
            self.set(key, str(delta).encode())
            return delta
        try:
            current = self._decode_int(entry.value)
        except ValueError as err:
            raise ValueError(f"Value for key '{key}' is not an integer.") from err
        new_value = current + delta
        self.set(key, str(new_value).encode())
        return new_value

    async def aincr(self, key: str, delta: int = 1) -> int:
        """Asynchronous variant of :meth:`incr`."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None or entry.is_expired:
                if entry is not None and entry.is_expired:
                    del self._store[key]
                await self._aset_locked(key, str(delta).encode())
                return delta
            try:
                current = self._decode_int(entry.value)
            except ValueError as err:
                raise ValueError(f"Value for key '{key}' is not an integer.") from err
            new_value = current + delta
            await self._aset_locked(key, str(new_value).encode())
            return new_value

    def decr(self, key: str, delta: int = 1) -> int:
        """Decrement a numeric cache value."""
        return self.incr(key, -delta)

    async def adecr(self, key: str, delta: int = 1) -> int:
        """Asynchronous variant of :meth:`decr`."""
        return await self.aincr(key, -delta)

    @staticmethod
    def _decode_int(data: bytes) -> int:
        return int(data.decode())

    # -- Lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Close backend. No-op for in-memory."""
        pass

    async def aclose(self) -> None:
        """Close async backend. No-op for in-memory."""
        pass

    # -- Internal helpers -------------------------------------------------

    def _aget_locked(self, key: str) -> bytes | None:
        """Get a value (caller holds lock)."""
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.is_expired:
            del self._store[key]
            return None
        return entry.value

    async def _aset_locked(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> None:
        """Set a value (caller holds lock)."""
        expires_at = self._resolve_expires_at(ex=ex, ea=ea)
        self._store[key] = CacheEntry(value=value, expires_at=expires_at)
        if self._settings.max_size > 0 and len(self._store) > self._settings.max_size:
            self._evict_locked()

    def _evict(self) -> None:
        """Remove expired entries, then oldest entries if needed."""
        expired = [k for k, v in self._store.items() if v.is_expired]
        for k in expired:
            del self._store[k]
        if len(self._store) > self._settings.max_size:
            keys_to_remove = list(self._store.keys())[: self._settings.max_size // 2]
            for k in keys_to_remove:
                del self._store[k]

    def _evict_locked(self) -> None:
        """Same as _evict but caller holds the lock."""
        expired = [k for k, v in self._store.items() if v.is_expired]
        for k in expired:
            del self._store[k]
        if len(self._store) > self._settings.max_size:
            keys_to_remove = list(self._store.keys())[: self._settings.max_size // 2]
            for k in keys_to_remove:
                del self._store[k]
