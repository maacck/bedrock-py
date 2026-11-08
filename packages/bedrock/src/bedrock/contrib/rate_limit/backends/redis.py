"""Redis rate-limit backend: GCRA evaluated atomically in one Lua script."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..base import RateLimitBackend
from ..entities import RateLimitDecision, RateLimitPolicy
from ..exc import RateLimitBackendError

# KEYS[1]: storage key. ARGV: interval_us, capacity_us, cost. Uses the Redis server clock so every application
# instance agrees on time, and integer microseconds because Redis truncates Lua numbers it returns.
# Microseconds since the epoch (~1.7e15) stay below 2^53, so Lua doubles hold them exactly.
_GCRA_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])
local interval = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local tat = tonumber(redis.call('GET', KEYS[1]) or now)
if tat < now then tat = now end
local new_tat = tat + interval * cost
local allow_at = new_tat - capacity
if now < allow_at then
  return {0, math.floor((capacity - (tat - now)) / interval), allow_at - now, tat - now}
end
redis.call('SET', KEYS[1], string.format('%d', new_tat), 'PX', math.ceil((new_tat - now) / 1000))
return {1, math.floor((capacity - (new_tat - now)) / interval), 0, new_tat - now}
"""


class RedisRateLimitSettings(BaseSettings):
    """Settings for the Redis rate-limit backend.

    Environment variables are prefixed with ``RATE_LIMIT_REDIS_``. They are independent of the cache's
    ``CACHE_REDIS_*`` settings; point both at the same URL to share a server.

    Attributes:
        url: Redis connection URL (e.g. ``redis://localhost:6379/0``).
        db: Redis database number.
        password: Redis authentication password.
        key_prefix: Prefix prepended to every rate-limit key.
        max_connections: Maximum number of connections in each pool.
    """

    model_config = SettingsConfigDict(env_prefix="RATE_LIMIT_REDIS_", extra="ignore")

    url: str = "redis://localhost:6379/0"
    db: int = 0
    password: str | None = None
    key_prefix: str = "rate_limit:"
    max_connections: int = 50


class RedisRateLimitBackend(RateLimitBackend):
    """Redis backend supporting both sync and async evaluation.

    Requires Redis 5 or newer (the script reads ``TIME`` before writing). Both clients are created at
    instantiation time; neither connects until first use.

    Args:
        settings: Configuration for this backend.
    """

    def __init__(self, settings: RedisRateLimitSettings | None = None) -> None:
        import redis
        import redis.asyncio

        self._settings = settings or RedisRateLimitSettings()
        self._redis_error = redis.RedisError
        options = {
            "db": self._settings.db,
            "password": self._settings.password,
            "max_connections": self._settings.max_connections,
            "decode_responses": False,
        }
        self._client = redis.from_url(self._settings.url, **options)
        self._async_client = redis.asyncio.from_url(self._settings.url, **options)
        self._script = self._client.register_script(_GCRA_SCRIPT)
        self._ascript = self._async_client.register_script(_GCRA_SCRIPT)

    @property
    def settings(self) -> RedisRateLimitSettings:
        """Return the settings used to configure this backend."""
        return self._settings

    def _key(self, storage_key: str) -> str:
        return f"{self._settings.key_prefix}{storage_key}"

    @staticmethod
    def _decision(policy: RateLimitPolicy, result: list[int]) -> RateLimitDecision:
        allowed, remaining, retry_after_us, reset_after_us = result
        return RateLimitDecision.from_micros(
            policy,
            allowed=bool(allowed),
            remaining=remaining,
            retry_after_us=retry_after_us,
            reset_after_us=reset_after_us,
        )

    def hit(self, storage_key: str, policy: RateLimitPolicy, cost: int) -> RateLimitDecision:
        """Consume ``cost`` units for ``storage_key`` if the bucket allows it."""
        try:
            result = self._script(keys=[self._key(storage_key)], args=[policy.interval_us, policy.capacity_us, cost])
        except self._redis_error as exc:
            raise RateLimitBackendError() from exc
        return self._decision(policy, result)

    async def ahit(self, storage_key: str, policy: RateLimitPolicy, cost: int) -> RateLimitDecision:
        """Asynchronous variant of :meth:`hit`."""
        try:
            result = await self._ascript(
                keys=[self._key(storage_key)], args=[policy.interval_us, policy.capacity_us, cost]
            )
        except self._redis_error as exc:
            raise RateLimitBackendError() from exc
        return self._decision(policy, result)

    def reset(self, storage_key: str) -> None:
        """Refill the bucket for ``storage_key``."""
        try:
            self._client.delete(self._key(storage_key))
        except self._redis_error as exc:
            raise RateLimitBackendError() from exc

    async def areset(self, storage_key: str) -> None:
        """Asynchronous variant of :meth:`reset`."""
        try:
            await self._async_client.delete(self._key(storage_key))
        except self._redis_error as exc:
            raise RateLimitBackendError() from exc

    def close(self) -> None:
        """Close sync connections."""
        self._client.close()

    async def aclose(self) -> None:
        """Close async connections."""
        # redis-py 5.0.1 deprecated close() in favor of aclose(); the supported floor (4.2) only has close().
        close = getattr(self._async_client, "aclose", None) or self._async_client.close
        await close()
