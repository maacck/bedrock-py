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
