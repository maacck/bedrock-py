"""Regression tests for cache backend expiration and safe clearing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest
from bedrock.contrib.cache.backends import memory as memory_module
from bedrock.contrib.cache.backends.memory import InMemoryBackend
from bedrock.contrib.cache.backends.redis import RedisBackend, RedisCacheSettings
from bedrock.contrib.cache.exc import CacheClearRequiresPrefixError


def _redis_backend(key_prefix: str, client: Mock | None = None, async_client: object | None = None) -> RedisBackend:
    """Create a Redis backend without opening a network connection."""
    backend = object.__new__(RedisBackend)
    backend._settings = RedisCacheSettings(key_prefix=key_prefix)
    backend._client = client or Mock()
    backend._async_client = async_client or Mock()
    return backend


class TestInMemoryExpiration:
    """Ensure all in-memory expiration modes use the monotonic clock."""

    def test_ex_px_and_ea_have_consistent_expiration_and_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"monotonic": 500.0, "wall": 1_000.0}
        monkeypatch.setattr(memory_module.time, "monotonic", lambda: clock["monotonic"])
        monkeypatch.setattr(memory_module.time, "time", lambda: clock["wall"])
        backend = InMemoryBackend()

        backend.set("relative", b"ex", ex=2)
        backend.set("milliseconds", b"px", px=1_500)
        backend.set("absolute", b"ea", ea=clock["wall"] + 1)

        assert backend.get_with_ttl("relative") == (b"ex", 2)
        assert backend.get_with_ttl("milliseconds") == (b"px", 1)
        assert backend.get_with_ttl("absolute") == (b"ea", 1)
        assert backend._store["absolute"].expires_at == 501.0

        clock["monotonic"] = 501.1

        assert backend.get("absolute") is None
        assert backend.get_with_ttl("absolute") == (None, None)
        assert backend.get("milliseconds") == b"px"

        clock["monotonic"] = 502.1

        assert backend.get("relative") is None
        assert backend.get("milliseconds") is None

    def test_async_ea_uses_the_same_monotonic_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"monotonic": 200.0, "wall": 1_500.0}
        monkeypatch.setattr(memory_module.time, "monotonic", lambda: clock["monotonic"])
        monkeypatch.setattr(memory_module.time, "time", lambda: clock["wall"])
        backend = InMemoryBackend()

        async def scenario() -> None:
            await backend.aset("absolute", b"value", ea=clock["wall"] + 1)
            assert await backend.aget_with_ttl("absolute") == (b"value", 1)
            clock["monotonic"] = 201.1
            assert await backend.aget("absolute") is None

        asyncio.run(scenario())


class _AsyncRedisClient:
    """Minimal asynchronous Redis client mock for namespace clear tests."""

    def __init__(self, keys: list[bytes]) -> None:
        self._keys = keys
        self.delete = AsyncMock(side_effect=lambda *keys: len(keys))
        self.flushdb = AsyncMock()
        self.scan_calls: list[tuple[bytes, int]] = []

    async def scan_iter(self, *, match: bytes, count: int):
        """Yield configured keys while recording the requested scan scope."""
        self.scan_calls.append((match, count))
        for key in self._keys:
            yield key


class TestRedisClear:
    """Ensure Redis clear is namespace-scoped and never flushes a database."""

    def test_clear_requires_a_configured_prefix_and_never_flushes(self) -> None:
        client = Mock()
        backend = _redis_backend("", client=client)

        with pytest.raises(CacheClearRequiresPrefixError, match="CACHE_REDIS_KEY_PREFIX"):
            backend.clear()

        client.flushdb.assert_not_called()
        client.scan_iter.assert_not_called()

    def test_clear_deletes_only_the_configured_namespace_in_a_batch(self) -> None:
        client = Mock()
        keys = [f"app:users:{index}".encode() for index in range(101)]
        client.scan_iter.return_value = iter(keys)
        client.delete.side_effect = [100, 1]
        backend = _redis_backend("app:", client=client)

        assert backend.clear(prefix="users:") == 101

        client.scan_iter.assert_called_once_with(match=b"app:users:*", count=100)
        assert client.delete.call_args_list == [call(*keys[:100]), call(keys[100])]
        client.flushdb.assert_not_called()

    def test_aclear_requires_a_configured_prefix_and_never_flushes(self) -> None:
        async_client = _AsyncRedisClient([])
        backend = _redis_backend("", async_client=async_client)

        async def scenario() -> None:
            with pytest.raises(CacheClearRequiresPrefixError, match="CACHE_REDIS_KEY_PREFIX"):
                await backend.aclear()

        asyncio.run(scenario())
        async_client.flushdb.assert_not_awaited()
        assert async_client.scan_calls == []

    def test_aclear_deletes_only_the_configured_namespace_in_a_batch(self) -> None:
        async_client = _AsyncRedisClient([b"app:users:1", b"app:users:2"])
        backend = _redis_backend("app:", async_client=async_client)

        async def scenario() -> None:
            assert await backend.aclear(prefix="users:") == 2

        asyncio.run(scenario())
        assert async_client.scan_calls == [(b"app:users:*", 100)]
        async_client.delete.assert_awaited_once_with(b"app:users:1", b"app:users:2")
        async_client.flushdb.assert_not_awaited()
