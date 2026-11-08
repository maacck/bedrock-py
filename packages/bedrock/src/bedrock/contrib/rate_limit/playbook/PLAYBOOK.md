# Bedrock Rate Limit

Token-bucket rate limiting for commands, jobs, and outbound calls, independent of any web framework. Policies are named buckets; callers consume units for a key and receive a decision with retry metadata. The built-in backend is Redis; another backend can replace it.

## Quick Reference

| Concern | Import |
|---------|--------|
| Rate-limit singleton | `from bedrock.contrib.rate_limit import rate_limit` |
| Define a policy | `from bedrock.contrib.rate_limit import RateLimitPolicy` |
| Decision value | `from bedrock.contrib.rate_limit import RateLimitDecision` |
| Register a backend | `from bedrock.contrib.rate_limit import register_backend, register_backend_lazy` |
| List backends | `from bedrock.contrib.rate_limit import list_backends` |

## Setup

Required: install the extra and point the backend at Redis 5 or newer.

```bash
uv add "bedrock-core[rate-limit-redis]"
RATE_LIMIT_REDIS_URL=redis://redis:6379/0
```

Add `bedrock.contrib.rate_limit` to your module's `depends_on`. Its `ready` hook configures the backend named by `RATE_LIMIT_BACKEND` (default `redis`); the first call does the same when the module was never readied. Without the extra, `ready` fails with `RateLimitBackendNotConfiguredError` naming `bedrock-core[rate-limit-redis]`. Redis is not contacted until the first call.

## Usage

```python
from datetime import timedelta
from bedrock.contrib.rate_limit import RateLimitPolicy, rate_limit

LOGIN = RateLimitPolicy(name="idp.login", limit=5, period=timedelta(minutes=15))

decision = rate_limit.hit(LOGIN, f"{tenant_id}:{email}")      # cost=1
if not decision.allowed:
    raise LoginThrottledError(retry_after=decision.retry_after)  # your domain error
rate_limit.reset(LOGIN, f"{tenant_id}:{email}")                  # e.g. after a successful login

decision = await rate_limit.ahit(LOGIN, key, cost=2)             # async; areset() too
```

- `RateLimitPolicy(name, limit, period)` — `limit` units may be spent at once; one unit returns every `period / limit`. `period` accepts a `timedelta` or seconds. Define policies once as module constants; two policies with the same `name` must be equal.
- `hit(policy, key, cost=1)` → `RateLimitDecision(allowed, limit, remaining, retry_after, reset_after)`. Durations are seconds (`float`). Denied hits consume nothing. `cost` must be an integer from 1 to `limit`.
- `reset(policy, key)` refills the bucket.
- Keys are hashed (BLAKE2b) before storage, so emails or API keys never reach Redis. Put the tenant in the key yourself: `f"{tenant_id}:{subject}"`.
- Synchronous code that needs the async API uses `bedrock.run_async(...)`, not `asyncio.run()`.

## Backends

| Backend | Name | Extra | Env prefix |
|---------|------|-------|------------|
| Redis | `"redis"` (default) | `rate-limit-redis` | `RATE_LIMIT_REDIS_` |

Redis settings: `url` (`redis://localhost:6379/0`), `db`, `password`, `key_prefix` (`rate_limit:`), `max_connections` (`50`). They are independent of `CACHE_REDIS_*`; use the same URL to share a server. One Lua script evaluates each hit atomically with the Redis server clock, so every application instance agrees.

## Custom Backend Registration

Register in `bootstrap.py:on_load` with `register_backend(name, backend_cls)` (a class, or a factory taking `settings=`) or `register_backend_lazy(name, "module:attribute", install_requirement=...)`, then set `RATE_LIMIT_BACKEND=<name>`. The backend implements `RateLimitBackend` (`hit`/`ahit(storage_key, policy, cost)`, `reset`/`areset(storage_key)`, `close`/`aclose`, `settings`, `holds_async_resources`) and must evaluate the GCRA token bucket atomically per key. `configure()` checks member names only; cover custom backends with contract tests.

`close()` / `aclose()` release the current backend and any backend `configure()` replaced; the module's `on_shutdown` calls both.

## Exceptions

All inherit from `RateLimitError` (a `BedrockExc`):

- `InvalidRateLimitPolicyError` — blank name, non-positive `limit`, or `period` shorter than 1 µs per unit.
- `InvalidRateLimitArgumentError` — empty key, or `cost` outside 1..`limit`.
- `RateLimitPolicyConflictError` — a different policy was already used under the same name.
- `RateLimitBackendError` — the backend failed (e.g. Redis unreachable). The message never includes connection details; the driver error is `__cause__`.
- `RateLimitBackendNotConfiguredError` — unknown backend name or missing dependency.
- `RateLimitCloseError` — closing a backend failed.

## Failure Policy

The library does not choose between failing open and failing closed:

```python
try:
    decision = rate_limit.hit(LOGIN, key)
except RateLimitBackendError:
    raise ServiceUnavailableError()   # fail closed: protect credentials
```

```python
try:
    allowed = rate_limit.hit(VENDOR, provider_id).allowed
except RateLimitBackendError:
    allowed = True                    # fail open: keep serving
```

## Anti-Patterns

- Don't build a policy per call with varying parameters under one name — it raises `RateLimitPolicyConflictError`.
- Don't use a key without the tenant in multi-tenant code — tenants would share one bucket.
- Don't map decisions to HTTP inside services — return or raise domain errors; adapters translate them.

## See Also

- Guide: `docs-web/content/docs/en/(bedrock)/guides/rate-limit.mdx`
