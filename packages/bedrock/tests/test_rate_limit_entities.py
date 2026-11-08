"""Tests for rate-limit policy validation and decision values."""

from __future__ import annotations

from datetime import timedelta

import pytest
from bedrock.contrib.rate_limit import InvalidRateLimitPolicyError, RateLimitDecision, RateLimitPolicy


def test_period_accepts_seconds_or_timedelta() -> None:
    assert RateLimitPolicy(name="p", limit=5, period=900) == RateLimitPolicy(
        name="p", limit=5, period=timedelta(minutes=15)
    )


def test_interval_and_capacity_are_microseconds() -> None:
    policy = RateLimitPolicy(name="login", limit=5, period=timedelta(minutes=15))

    assert policy.interval_us == 180_000_000
    assert policy.capacity_us == 900_000_000


@pytest.mark.parametrize(
    ("name", "limit", "period"),
    [
        ("", 1, 1),
        ("   ", 1, 1),
        ("p", 0, 1),
        ("p", -1, 1),
        ("p", 1, 0),
        ("p", 1, -1),
        ("p", 3, timedelta(microseconds=2)),
    ],
)
def test_invalid_policy_raises_bedrock_error(name: str, limit: int, period: object) -> None:
    with pytest.raises(InvalidRateLimitPolicyError):
        RateLimitPolicy(name=name, limit=limit, period=period)


def test_smallest_valid_period_is_one_microsecond_per_unit() -> None:
    assert RateLimitPolicy(name="p", limit=3, period=timedelta(microseconds=3)).interval_us == 1


@pytest.mark.parametrize(("limit", "period_us"), [(3, 1_000_000), (600_000, 1_000_000), (7, 10)])
def test_non_divisible_interval_never_refills_faster_than_the_period(limit: int, period_us: int) -> None:
    policy = RateLimitPolicy(name="p", limit=limit, period=timedelta(microseconds=period_us))

    assert policy.capacity_us >= period_us
    assert policy.capacity_us - period_us < limit


def test_decision_from_micros_converts_to_seconds_and_clamps_remaining() -> None:
    policy = RateLimitPolicy(name="p", limit=5, period=10)

    decision = RateLimitDecision.from_micros(
        policy, allowed=False, remaining=-1, retry_after_us=1_500_000, reset_after_us=10_000_000
    )

    assert decision == RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after=1.5, reset_after=10.0)
