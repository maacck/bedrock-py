"""Tests for cache-backed lock behavior."""

from __future__ import annotations

import asyncio

import pytest
from bedrock.contrib.cache import CacheLockAcquisitionError, CacheLockOwnershipError
from bedrock.contrib.cache.service import CacheService


class TestCacheLock:
    """Tests for sync and async cache lock usage."""

    def test_with_context_manager_acquires_and_releases(self) -> None:
        cache = CacheService()
        first = cache.lock("jobs:sync", expire=5, blocking=False)
        contender = cache.lock("jobs:sync", expire=5, blocking=False)

        with first:
            assert first.locked() is True
            assert contender.acquire() is False

        assert first.locked() is False
        assert contender.acquire() is True
        contender.release()

    def test_with_raises_when_lock_is_busy(self) -> None:
        cache = CacheService()

        with cache.lock("jobs:busy", expire=5, blocking=False):
            with pytest.raises(CacheLockAcquisitionError):
                with cache.lock("jobs:busy", expire=5, blocking=False):
                    pass

    def test_release_raises_when_lock_is_no_longer_owned(self) -> None:
        cache = CacheService()
        lock = cache.lock("jobs:owned", expire=5, blocking=False)

        assert lock.acquire() is True
        cache.get_backend().set(lock.key, b"someone-else", ex=5)

        with pytest.raises(CacheLockOwnershipError):
            lock.release()

    def test_extend_resets_ttl_while_owned(self) -> None:
        cache = CacheService()
        lock = cache.lock("jobs:long", expire=2, blocking=False)

        assert lock.acquire() is True
        lock.extend(expire=60)

        assert 2 < cache.get_backend().get_with_ttl(lock.key)[1] <= 60
        lock.release()

    def test_extend_raises_and_keeps_other_owner_ttl_when_lock_was_taken(self) -> None:
        cache = CacheService()
        lock = cache.lock("jobs:taken", expire=5, blocking=False)

        assert lock.acquire() is True
        cache.get_backend().set(lock.key, b"someone-else", ex=5)

        with pytest.raises(CacheLockOwnershipError):
            lock.extend(expire=60)
        value, ttl = cache.get_backend().get_with_ttl(lock.key)
        assert value == b"someone-else"
        assert ttl <= 5

    def test_extend_raises_when_not_acquired(self) -> None:
        cache = CacheService()

        with pytest.raises(CacheLockOwnershipError):
            cache.lock("jobs:idle", expire=5).extend()

    def test_async_with_context_manager(self) -> None:
        async def scenario() -> None:
            cache = CacheService()
            first = cache.lock("jobs:async", expire=5, blocking=False)
            contender = cache.lock("jobs:async", expire=5, blocking=False)

            async with first:
                assert await first.alocked() is True
                assert await contender.aacquire() is False

            assert await first.alocked() is False
            assert await contender.aacquire() is True
            await contender.arelease()

        asyncio.run(scenario())

    def test_aextend_resets_ttl_and_detects_lost_lock(self) -> None:
        async def scenario() -> None:
            cache = CacheService()
            lock = cache.lock("jobs:async-long", expire=2, blocking=False)

            assert await lock.aacquire() is True
            await lock.aextend(expire=60)
            assert 2 < (await cache.get_backend().aget_with_ttl(lock.key))[1] <= 60

            await cache.get_backend().adelete(lock.key)
            with pytest.raises(CacheLockOwnershipError):
                await lock.aextend()

        asyncio.run(scenario())
