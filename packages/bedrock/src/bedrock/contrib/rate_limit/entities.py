"""Provider-neutral rate-limit values."""

from __future__ import annotations

from datetime import timedelta

from pydantic import ConfigDict, model_validator

from ...entities import BedrockEntity
from .exc import InvalidRateLimitPolicyError

_MICROSECOND = timedelta(microseconds=1)


class RateLimitPolicy(BedrockEntity):
    """A named token bucket: ``limit`` units may be spent at once, then one unit returns every ``period / limit``.

    Policies are usually module-level constants. Two policies with the same ``name`` must be equal, because the
    name is part of every storage key.

    Attributes:
        name: Policy name, e.g. ``"idp.login"``.
        limit: Bucket capacity: the largest burst and the largest single ``cost``.
        period: Time for an empty bucket to refill completely. Accepts a ``timedelta`` or seconds.

    Raises:
        InvalidRateLimitPolicyError: If the name is blank, ``limit`` is not positive, or ``period`` is shorter
            than one microsecond per unit.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    limit: int
    period: timedelta

    @model_validator(mode="after")
    def _validate(self) -> RateLimitPolicy:
        # BedrockExc is not a ValueError, so pydantic lets it propagate instead of wrapping it.
        if not self.name.strip():
            raise InvalidRateLimitPolicyError("Rate limit policy name must be a non-empty string.")
        if self.limit <= 0:
            raise InvalidRateLimitPolicyError(f"Rate limit policy '{self.name}' limit must be positive.")
        if self.period // _MICROSECOND < self.limit:
            raise InvalidRateLimitPolicyError(
                f"Rate limit policy '{self.name}' period must allow at least one microsecond per unit."
            )
        return self

    @property
    def interval_us(self) -> int:
        """Return the microseconds it takes one unit to return to the bucket."""
        return (self.period // _MICROSECOND) // self.limit

    @property
    def capacity_us(self) -> int:
        """Return the microseconds a full burst occupies (``interval_us * limit``)."""
        return self.interval_us * self.limit


class RateLimitDecision(BedrockEntity):
    """Outcome of one rate-limit hit.

    Attributes:
        allowed: Whether the cost was consumed.
        limit: The policy capacity.
        remaining: Units that could still be consumed right now.
        retry_after: Seconds until the same cost would be allowed; ``0.0`` when allowed.
        reset_after: Seconds until the bucket is full again.
    """

    model_config = ConfigDict(frozen=True)

    allowed: bool
    limit: int
    remaining: int
    retry_after: float
    reset_after: float

    @classmethod
    def from_micros(
        cls,
        policy: RateLimitPolicy,
        *,
        allowed: bool,
        remaining: int,
        retry_after_us: int,
        reset_after_us: int,
    ) -> RateLimitDecision:
        """Build a decision from the integer microsecond results a backend computes.

        Args:
            policy: The evaluated policy.
            allowed: Whether the cost was consumed.
            remaining: Units left; negative values are reported as ``0``.
            retry_after_us: Microseconds until the cost would be allowed.
            reset_after_us: Microseconds until the bucket is full.

        Returns:
            The decision with durations in seconds.
        """
        return cls(
            allowed=allowed,
            limit=policy.limit,
            remaining=max(remaining, 0),
            retry_after=retry_after_us / 1_000_000,
            reset_after=reset_after_us / 1_000_000,
        )
