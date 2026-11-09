"""Rate-limit backend protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic_settings import BaseSettings

from .entities import RateLimitDecision, RateLimitPolicy


@runtime_checkable
class RateLimitBackend(Protocol):
    """Protocol that every rate-limit backend implements.

    A backend evaluates :class:`RateLimitPolicy` as a GCRA token bucket, atomically per key: integer microseconds,
    ``limit`` units available at once, one unit returning every ``policy.interval_us``, denied hits consuming
    nothing. ``storage_key`` is already namespaced by policy name and hashed by the service.

    Backends registered with :func:`bedrock.contrib.rate_limit.register_backend` or
    :func:`bedrock.contrib.rate_limit.register_backend_lazy` may subclass this protocol or implement it
    structurally; :meth:`RateLimitService.configure` checks that every member exists.

    ``close()`` releases the synchronous client and ``aclose()`` the asynchronous one. A backend that holds no
    asynchronous resource sets ``holds_async_resources = False`` so shutdown does not need to await it.
    """

    holds_async_resources: bool = True

    @property
    def settings(self) -> BaseSettings: ...

    def hit(self, storage_key: str, policy: RateLimitPolicy, cost: int) -> RateLimitDecision: ...
    async def ahit(self, storage_key: str, policy: RateLimitPolicy, cost: int) -> RateLimitDecision: ...

    def reset(self, storage_key: str) -> None: ...
    async def areset(self, storage_key: str) -> None: ...

    def close(self) -> None: ...
    async def aclose(self) -> None: ...
