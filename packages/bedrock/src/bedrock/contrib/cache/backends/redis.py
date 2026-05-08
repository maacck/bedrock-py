"""Redis cache backend implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..base import CacheBackend

try:
    import redis
    import redis.asyncio
except ImportError as exc:
    raise ImportError(
        "The 'redis' package is required for RedisCacheBackend. Install with 'pip install redis'."
    ) from exc

_COMPARE_AND_DELETE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class RedisCacheSettings(BaseSettings):
    """Settings for the Redis cache backend.

    Environment variables are prefixed with ``CACHE_REDIS_``.

    Attributes:
        url: Redis connection URL (e.g. ``redis://localhost:6379/0``).
        db: Redis database number.
        password: Redis authentication password.
        key_prefix: Global prefix prepended to all cache keys.
        max_connections: Maximum number of connections in the pool.
    """

    model_config = SettingsConfigDict(env_prefix="CACHE_REDIS_", extra="ignore")

    url: str = "redis://localhost:6379/0"
    db: int = 0
    password: str | None = None
    key_prefix: str = ""
    max_connections: int = 50


class RedisBackend(CacheBackend):
    """Redis-based cache backend supporting both sync and async operations.

    Both sync and async clients are created at instantiation time.

    Args:
        settings: Configuration for this backend.
    """

    def __init__(self, settings: RedisCacheSettings | None = None) -> None:
        self._settings = settings or RedisCacheSettings()

        self._client = redis.from_url(
            self._settings.url,
            db=self._settings.db,
            password=self._settings.password,
            max_connections=self._settings.max_connections,
            decode_responses=False,
        )
        self._async_client = redis.asyncio.from_url(
            self._settings.url,
            db=self._settings.db,
            password=self._settings.password,
            max_connections=self._settings.max_connections,
            decode_responses=False,
        )

    @property
    def settings(self) -> RedisCacheSettings:
        """Return the settings used to configure this backend."""
        return self._settings

    def _make_key(self, key: str) -> bytes:
        """Create a prefixed key."""
        full_key = f"{self._settings.key_prefix}{key}"
        return full_key.encode()

    # -- Core operations --------------------------------------------------

    def get(self, key: str, default: bytes | None = None) -> bytes | None:
        """Retrieve a cached value by key."""
        data = self._client.get(self._make_key(key))
        return data if data is not None else default

    async def aget(self, key: str, default: bytes | None = None) -> bytes | None:
        """Asynchronous variant of :meth:`get`."""
        data = await self._async_client.get(self._make_key(key))
        return data if data is not None else default

    def get_with_ttl(self, key: str) -> tuple[bytes | None, int | None]:
        """Retrieve a cached value along with its remaining TTL."""
        raw_key = self._make_key(key)
        pipe = self._client.pipeline()
        pipe.get(raw_key)
        pipe.ttl(raw_key)
        data, ttl = pipe.execute()
        if data is None:
            return None, None
        return data, (ttl if ttl > 0 else 0)

    async def aget_with_ttl(self, key: str) -> tuple[bytes | None, int | None]:
        """Asynchronous variant of :meth:`get_with_ttl`."""
        raw_key = self._make_key(key)
        data = await self._async_client.get(raw_key)
        if data is None:
            return None, None
        ttl = await self._async_client.ttl(raw_key)
        return data, (ttl if ttl > 0 else 0)

    def set(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> None:
        """Store a value in the cache."""
        raw_key = self._make_key(key)
        if ex is not None:
            self._client.setex(raw_key, ex, value)
        elif ea is not None:
            self._client.set(raw_key, value, exat=int(ea))
        else:
            self._client.set(raw_key, value)

    async def aset(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> None:
        """Asynchronous variant of :meth:`set`."""
        raw_key = self._make_key(key)
        if ex is not None:
            await self._async_client.setex(raw_key, ex, value)
        elif ea is not None:
            await self._async_client.set(raw_key, value, exat=int(ea))
        else:
            await self._async_client.set(raw_key, value)

    def add(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> bool:
        """Store a value only when the key does not already exist."""
        raw_key = self._make_key(key)
        if ex is not None:
            return bool(self._client.set(raw_key, value, ex=ex, nx=True))
        if ea is not None:
            return bool(self._client.set(raw_key, value, exat=int(ea), nx=True))
        return bool(self._client.set(raw_key, value, nx=True))

    async def aadd(self, key: str, value: bytes, ex: int | None = None, ea: float | None = None) -> bool:
        """Asynchronous variant of :meth:`add`."""
        raw_key = self._make_key(key)
        if ex is not None:
            return bool(await self._async_client.set(raw_key, value, ex=ex, nx=True))
        if ea is not None:
            return bool(await self._async_client.set(raw_key, value, exat=int(ea), nx=True))
        return bool(await self._async_client.set(raw_key, value, nx=True))

    def delete(self, key: str) -> bool:
        """Remove a key from the cache."""
        return bool(self._client.delete(self._make_key(key)))

    async def adelete(self, key: str) -> bool:
        """Asynchronous variant of :meth:`delete`."""
        return bool(await self._async_client.delete(self._make_key(key)))

    def compare_and_delete(self, key: str, expected: bytes) -> bool:
        """Delete a key only when its current value matches *expected*."""
        return bool(
            self._client.eval(
                _COMPARE_AND_DELETE_SCRIPT,
                1,
                self._make_key(key),
                expected,
            )
        )

    async def acompare_and_delete(self, key: str, expected: bytes) -> bool:
        """Asynchronous variant of :meth:`compare_and_delete`."""
        return bool(
            await self._async_client.eval(
                _COMPARE_AND_DELETE_SCRIPT,
                1,
                self._make_key(key),
                expected,
            )
        )

    def exists(self, key: str) -> bool:
        """Check whether a key exists in the cache."""
        return bool(self._client.exists(self._make_key(key)))

    async def aexists(self, key: str) -> bool:
        """Asynchronous variant of :meth:`exists`."""
        return bool(await self._async_client.exists(self._make_key(key)))

    def clear(self, prefix: str | None = None) -> int:
        """Remove all entries from the cache, optionally filtered by prefix."""
        if prefix is not None:
            pattern = f"{self._settings.key_prefix}{prefix}*".encode()
            deleted = 0
            for key in self._client.scan_iter(match=pattern):
                deleted += self._client.delete(key)
            return deleted
        if self._settings.key_prefix:
            pattern = f"{self._settings.key_prefix}*".encode()
            deleted = 0
            for key in self._client.scan_iter(match=pattern):
                deleted += self._client.delete(key)
            return deleted
        return self._client.flushdb()

    async def aclear(self, prefix: str | None = None) -> int:
        """Asynchronous variant of :meth:`clear`."""
        if prefix is not None:
            pattern = f"{self._settings.key_prefix}{prefix}*".encode()
            deleted = 0
            async for key in self._async_client.scan_iter(match=pattern):
                deleted += await self._async_client.delete(key)
            return deleted
        if self._settings.key_prefix:
            pattern = f"{self._settings.key_prefix}*".encode()
            deleted = 0
            async for key in self._async_client.scan_iter(match=pattern):
                deleted += await self._async_client.delete(key)
            return deleted
        return await self._async_client.flushdb()

    # -- Batch operations -------------------------------------------------

    def get_many(self, keys: list[str]) -> dict[str, bytes]:
        """Retrieve multiple keys in one call."""
        mapped_keys = [self._make_key(k) for k in keys]
        values = self._client.mget(mapped_keys)
        result: dict[str, bytes] = {}
        for key, data in zip(keys, values, strict=False):
            if data is not None:
                result[key] = data
        return result

    async def aget_many(self, keys: list[str]) -> dict[str, bytes]:
        """Asynchronous variant of :meth:`get_many`."""
        mapped_keys = [self._make_key(k) for k in keys]
        values = await self._async_client.mget(mapped_keys)
        result: dict[str, bytes] = {}
        for key, data in zip(keys, values, strict=False):
            if data is not None:
                result[key] = data
        return result

    def set_many(self, mapping: dict[str, bytes], ex: int | None = None, ea: float | None = None) -> None:
        """Store multiple key-value pairs in one call."""
        pipe = self._client.pipeline()
        for key, value in mapping.items():
            raw_key = self._make_key(key)
            if ex is not None:
                pipe.setex(raw_key, ex, value)
            elif ea is not None:
                pipe.set(raw_key, value, exat=int(ea))
            else:
                pipe.set(raw_key, value)
        pipe.execute()

    async def aset_many(self, mapping: dict[str, bytes], ex: int | None = None, ea: float | None = None) -> None:
        """Asynchronous variant of :meth:`set_many`."""
        pipe = self._async_client.pipeline()
        for key, value in mapping.items():
            raw_key = self._make_key(key)
            if ex is not None:
                pipe.setex(raw_key, ex, value)
            elif ea is not None:
                pipe.set(raw_key, value, exat=int(ea))
            else:
                pipe.set(raw_key, value)
        await pipe.execute()

    # -- Convenience operations -------------------------------------------

    def get_or_set(
        self,
        key: str,
        default_provider: Callable[[], bytes] | bytes,
        ex: int | None = None,
        ea: float | None = None,
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
        default_provider: Callable[[], bytes] | bytes | Callable[[], Awaitable[bytes]],
        ex: int | None = None,
        ea: float | None = None,
    ) -> bytes | None:
        """Asynchronous variant of :meth:`get_or_set`."""
        value = await self.aget(key)
        if value is not None:
            return value
        if callable(default_provider):
            if asyncio.iscoroutinefunction(default_provider):
                value = await default_provider()
            else:
                value = default_provider()
        else:
            value = default_provider
        await self.aset(key, value, ex=ex, ea=ea)
        return value

    # -- Key listing ------------------------------------------------------

    def list(self, prefix: str, limit: int = 100) -> list[str]:
        """List cache keys matching a prefix."""
        pattern = f"{self._settings.key_prefix}{prefix}*".encode()
        prefix_len = len(self._settings.key_prefix.encode())
        keys: list[str] = []
        for raw_key in self._client.scan_iter(match=pattern):
            key_str = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            keys.append(key_str[prefix_len:])
            if len(keys) >= limit:
                break
        return keys

    async def alist(self, prefix: str, limit: int = 100) -> list[str]:
        """Asynchronous variant of :meth:`list`."""
        pattern = f"{self._settings.key_prefix}{prefix}*".encode()
        prefix_len = len(self._settings.key_prefix.encode())
        keys: list[str] = []
        async for raw_key in self._async_client.scan_iter(match=pattern):
            key_str = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            keys.append(key_str[prefix_len:])
            if len(keys) >= limit:
                break
        return keys

    # -- Counters ---------------------------------------------------------

    def incr(self, key: str, delta: int = 1) -> int:
        """Increment a numeric cache value."""
        try:
            return self._client.incrby(self._make_key(key), delta)
        except Exception as exc:
            raise ValueError(f"Cannot increment key '{key}': {exc}") from exc

    async def aincr(self, key: str, delta: int = 1) -> int:
        """Asynchronous variant of :meth:`incr`."""
        try:
            return await self._async_client.incrby(self._make_key(key), delta)
        except Exception as exc:
            raise ValueError(f"Cannot increment key '{key}': {exc}") from exc

    def decr(self, key: str, delta: int = 1) -> int:
        """Decrement a numeric cache value."""
        return self.incr(key, -delta)

    async def adecr(self, key: str, delta: int = 1) -> int:
        """Asynchronous variant of :meth:`decr`."""
        return await self.aincr(key, -delta)

    # -- Lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Close sync connections."""
        self._client.close()

    async def aclose(self) -> None:
        """Close async connections."""
        await self._async_client.close()
