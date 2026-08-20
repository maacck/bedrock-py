# Lifecycle

Two layers: the Bedrock **module** hooks, and the `CacheService` / entry lifetime.

## Module hooks

`bedrock.contrib.cache.bootstrap` implements:

| Hook | Implemented | Effect |
|------|-------------|--------|
| `on_load` | no | — |
| `ready(*, registry, app)` | yes | `cache.configure()` → **memory** backend |
| `on_shutdown(*, registry, app)` | yes | `cache.close()` |

`ready` runs after every module is loaded. `on_shutdown` runs in reverse dependency order when the registry shuts down.

Standalone scripts that only `import cache` never run these hooks. The service still works: the first operation calls `get_backend()` and lazy-configures memory.

## Choosing Redis in a Bedrock app

`ready` always configures memory. To use Redis, reconfigure **after** that hook, typically from your application's `ready`:

```python
# yourapp/bootstrap.py
from bedrock.contrib.cache import cache
from bedrock.contrib.cache.backends.redis import RedisCacheSettings

def ready(*, registry, app) -> None:
    cache.close()
    cache.configure(
        "redis",
        settings=RedisCacheSettings(key_prefix="yourapp:"),
    )
```

Call `close()` first so the memory backend from cache's `ready` is not leaked. Declare a dependency on `bedrock.contrib.cache` so your `ready` runs after it.

Outside the registry:

```python
from bedrock.contrib.cache import cache
from bedrock.contrib.cache.backends.redis import RedisCacheSettings

cache.configure("redis", settings=RedisCacheSettings())
try:
    cache.set("k", "v", ex=60)
finally:
    cache.close()
```

Async shutdown: `await cache.aclose()`.

## Service states

```
unconfigured  --configure()/get_backend()-->  active  --close()/aclose()-->  unconfigured
```

| Method | Behavior |
|--------|----------|
| `configure(name="memory", settings=None)` | Instantiate the named backend and store it. Unknown name → `BackendNotConfiguredError`. **Does not** close a previous backend. |
| `get_backend()` | Return the active backend, or `configure("memory")` if none. |
| `close()` | `backend.close()` then `_backend = None`. Safe if already unconfigured. |
| `aclose()` | Async close, then `_backend = None`. |

Calling `configure()` twice without `close()` drops the previous client (Redis connection pool included). Don't.

After `close()`, the next operation lazy-configures memory again unless you `configure(...)` first.

## Entry lifetime (TTL)

`set` / `aset` / slot writes take exactly one of:

| Argument | Meaning |
|----------|---------|
| `ex` | Relative expiry, seconds |
| `px` | Relative expiry, milliseconds |
| `ea` | Absolute Unix timestamp |
| all `None` | No expiry (until `delete`, eviction, process exit, or Redis `maxmemory`) |

Slot `ttl` is the default `ex` when the write does not pass `ex`/`px`/`ea`. `get_or_load` goes through `set`, so that default applies.

`get_with_ttl` returns `(value, remaining_seconds)`. Remaining is `None` when the key is missing or has no expiry.

Memory backend: expiry is tracked with `time.monotonic()`. Size cap is `CACHE_MEMORY_MAX_SIZE` (default 10000; `0` = unlimited). Eviction drops expired keys, then oldest keys.

Redis backend: expiry is Redis TTL. `CACHE_REDIS_KEY_PREFIX` is prepended to every key. Prefer a dedicated prefix in shared Redis.

Locks have a separate TTL: `cache.lock(..., expire=30)` is the lock-key lifetime, not the cached-value TTL.

## Tests and scripts

```python
from bedrock.contrib.cache.service import CacheService

def test_something() -> None:
    cache = CacheService()          # isolated; does not touch the global singleton
    cache.set("k", "v", ex=5)
```

Use a new `CacheService()` in tests. The global `cache` singleton is process-wide.
