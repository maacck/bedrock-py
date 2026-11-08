"""GCRA behavior of the Redis rate-limit backend against a real Redis server.

Set ``BEDROCK_TEST_REDIS_URL`` (e.g. ``redis://localhost:6379/15``) to run; otherwise every test is skipped.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest

redis = pytest.importorskip("redis")

from bedrock.contrib.rate_limit import (  # noqa: E402
    RateLimitBackendError,
    RateLimitPolicy,
    RateLimitService,
    RedisRateLimitSettings,
)

REDIS_URL = os.environ.get("BEDROCK_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="BEDROCK_TEST_REDIS_URL is not set")

POLICY = RateLimitPolicy(name="login", limit=5, period=timedelta(minutes=15))


@pytest.fixture
def prefix() -> Iterator[str]:
    value = f"test-rl-{uuid.uuid4().hex}:"
    yield value
    client = redis.Redis.from_url(REDIS_URL)
    keys = list(client.scan_iter(match=f"{value}*"))
    if keys:
        client.delete(*keys)
    client.close()


@pytest.fixture
def service(prefix: str) -> Iterator[RateLimitService]:
    svc = RateLimitService()
    svc.configure("redis", settings=RedisRateLimitSettings(url=REDIS_URL, key_prefix=prefix))
    yield svc
    svc.close()
    if svc.has_async_resources():
        asyncio.run(svc.aclose())


def test_burst_then_denial_reports_retry_and_reset(service: RateLimitService) -> None:
    decisions = [service.hit(POLICY, "alice") for _ in range(6)]

    assert [d.allowed for d in decisions] == [True] * 5 + [False]
    assert [d.remaining for d in decisions] == [4, 3, 2, 1, 0, 0]
    denied = decisions[-1]
    assert 0 < denied.retry_after <= 180
    assert denied.retry_after == pytest.approx(180, abs=0.05)
    assert denied.reset_after == pytest.approx(900, abs=0.05)
    assert decisions[0].retry_after == 0


def test_denied_hits_consume_nothing(service: RateLimitService) -> None:
    for _ in range(5):
        service.hit(POLICY, "alice")
    first = service.hit(POLICY, "alice")
    second = service.hit(POLICY, "alice")

    assert not first.allowed and not second.allowed
    assert second.reset_after <= first.reset_after


def test_weighted_cost(service: RateLimitService) -> None:
    assert service.hit(POLICY, "alice", cost=3).remaining == 2
    assert not service.hit(POLICY, "alice", cost=3).allowed
    assert service.hit(POLICY, "alice", cost=2).remaining == 0
    assert service.hit(POLICY, "bob", cost=5).allowed


def test_keys_and_policies_are_independent(service: RateLimitService) -> None:
    other = RateLimitPolicy(name="api", limit=5, period=timedelta(minutes=15))
    service.hit(POLICY, "alice", cost=5)

    assert service.hit(POLICY, "bob").remaining == 4
    assert service.hit(other, "alice").remaining == 4


def test_reset_refills_the_bucket(service: RateLimitService) -> None:
    service.hit(POLICY, "alice", cost=5)
    service.reset(POLICY, "alice")

    assert service.hit(POLICY, "alice").remaining == 4


def test_units_return_over_time(service: RateLimitService) -> None:
    fast = RateLimitPolicy(name="fast", limit=2, period=timedelta(milliseconds=200))
    service.hit(fast, "alice", cost=2)
    assert not service.hit(fast, "alice").allowed

    time.sleep(0.12)  # one 100 ms interval has passed
    assert service.hit(fast, "alice").allowed
    assert not service.hit(fast, "alice").allowed

    time.sleep(0.25)  # longer than the full period
    assert service.hit(fast, "alice", cost=2).allowed


@pytest.mark.asyncio
async def test_async_matches_sync(service: RateLimitService) -> None:
    sync = [service.hit(POLICY, "sync-user") for _ in range(6)]
    async_ = [await service.ahit(POLICY, "async-user") for _ in range(6)]
    await service.areset(POLICY, "async-user")
    after_reset = await service.ahit(POLICY, "async-user")
    await service.aclose()

    assert [(d.allowed, d.remaining) for d in async_] == [(d.allowed, d.remaining) for d in sync]
    assert after_reset.remaining == 4


def test_stored_key_is_prefixed_hashed_and_expires(service: RateLimitService, prefix: str) -> None:
    service.hit(POLICY, "alice@example.com")
    client = redis.Redis.from_url(REDIS_URL)
    keys = [key.decode() for key in client.scan_iter(match=f"{prefix}*")]

    assert len(keys) == 1
    assert keys[0].startswith(f"{prefix}login:")
    assert "alice" not in keys[0]
    assert 0 < client.pttl(keys[0]) <= 180_000
    client.close()


def test_concurrent_hits_never_exceed_the_limit(service: RateLimitService) -> None:
    policy = RateLimitPolicy(name="burst", limit=10, period=60)
    allowed: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(50)

    def worker() -> None:
        start.wait()
        decision = service.hit(policy, "shared")
        with lock:
            allowed.append(decision.allowed)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(allowed) == 10


def test_unreachable_redis_raises_a_sanitized_backend_error() -> None:
    svc = RateLimitService()
    svc.configure("redis", settings=RedisRateLimitSettings(url="redis://127.0.0.1:1/0"))

    with pytest.raises(RateLimitBackendError) as info:
        svc.hit(POLICY, "alice")

    assert "127.0.0.1" not in str(info.value)
    assert isinstance(info.value.__cause__, redis.RedisError)
    svc.close()
