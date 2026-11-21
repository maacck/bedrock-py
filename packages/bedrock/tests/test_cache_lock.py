"""Tests for cache-backed lock behavior."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from bedrock.contrib.cache import CacheLockAcquisitionError, CacheLockOwnershipError
from bedrock.contrib.cache.backends import memory as memory_module
from bedrock.contrib.cache.backends.memory import MemoryCacheSettings
from bedrock.contrib.cache.entities import CacheEntry
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

    @pytest.mark.parametrize(
        ("renewal", "reader"),
        [
            ("extend", "get"),
            ("extend", "get_with_ttl"),
            ("extend", "exists"),
            ("extend", "get_many"),
            ("extend", "set"),
            ("aextend", "aget"),
            ("aextend", "aget_with_ttl"),
            ("aextend", "aexists"),
            ("aextend", "aget_many"),
            ("aextend", "aset"),
            ("extend", "aexists"),
            ("aextend", "exists"),
        ],
    )
    def test_expiry_cleanup_cannot_delete_a_successfully_renewed_lock(
        self, monkeypatch: pytest.MonkeyPatch, renewal: str, reader: str
    ) -> None:
        """A reader racing a renewal cannot admit a second lock holder."""
        clock = [100.0]
        monkeypatch.setattr(memory_module.time, "monotonic", lambda: clock[0])
        cache = CacheService()
        backend = cache.configure("memory", settings=MemoryCacheSettings(max_size=2))
        holder = cache.lock("jobs:renewal-race", expire=1, blocking=False)
        contender = cache.lock("jobs:renewal-race", expire=30, blocking=False)
        assert holder.acquire()
        backend.set("expired-filler", b"old", ex=1)
        original_entry = backend._store[holder.key]
        original_expiry = CacheEntry.is_expired.fget
        original_lock = backend._sync_lock
        renewal_checked = threading.Event()
        reader_started = threading.Event()
        renewal_done = threading.Event()
        role = threading.local()

        class ObservedLock:
            """Expose a reader's lock attempt without changing mutual exclusion."""

            def __enter__(self) -> None:
                if getattr(role, "name", None) == "reader":
                    reader_started.set()
                original_lock.acquire()

            def __exit__(self, *exc: object) -> None:
                original_lock.release()

        def scheduled_expiry(entry: CacheEntry) -> bool:
            expired = original_expiry(entry)
            if entry is original_entry:
                if getattr(role, "name", None) == "renewal":
                    assert not expired
                    renewal_checked.set()
                    assert reader_started.wait(5), "reader never attempted access"
                elif getattr(role, "name", None) == "reader" and expired:
                    # Also exposes the old unlocked reader path: it inspected the
                    # stale entry, then resumes deletion after renewal completes.
                    reader_started.set()
                    assert renewal_done.wait(5), "renewal never completed"
            return expired

        def renew() -> None:
            role.name = "renewal"
            try:
                if renewal == "aextend":
                    asyncio.run(holder.aextend(expire=30))
                else:
                    holder.extend(expire=30)
            finally:
                renewal_done.set()

        def read_or_evict() -> None:
            role.name = "reader"
            if reader in {"set", "aset"}:
                args = ("fresh", b"value")
            elif reader in {"get_many", "aget_many"}:
                args = ([holder.key],)
            else:
                args = (holder.key,)
            result = getattr(backend, reader)(*args)
            if reader.startswith("a"):
                asyncio.run(result)

        monkeypatch.setattr(backend, "_sync_lock", ObservedLock())
        monkeypatch.setattr(CacheEntry, "is_expired", property(scheduled_expiry))
        with ThreadPoolExecutor(max_workers=2) as pool:
            renewed = pool.submit(renew)
            assert renewal_checked.wait(5), "renewal never inspected the old entry"
            clock[0] = 102.0
            read = pool.submit(read_or_evict)
            renewed.result(timeout=5)
            read.result(timeout=5)

        assert holder.locked()
        assert contender.acquire() is False
        holder.release()
        cache.close()
