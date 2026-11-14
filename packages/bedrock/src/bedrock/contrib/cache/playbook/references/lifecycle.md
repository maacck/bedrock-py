# Lifecycle

Two layers: the Bedrock **module** hooks, and the `CacheService` / entry lifetime.

## Module hooks

`bedrock.contrib.cache.bootstrap` implements:

| Hook | Implemented | Effect |
|------|-------------|--------|
| `on_load` | no | — |
| `ready(*, registry, app)` | yes | `cache.configure()` → backend named by `CACHE_BACKEND` (default **memory**) |
| `on_shutdown(*, registry, app)` | yes | `cache.close()`; returns `cache.aclose()` if a backend still holds async clients, which the registry awaits |

`ready` runs after every module is loaded. `on_shutdown` runs in reverse dependency order when the registry shuts down.

Standalone scripts that only `import cache` never run these hooks. The service still works: the first operation calls `get_backend()`, which configures the `CACHE_BACKEND` backend (memory when unset).

## Choosing Redis in a Bedrock app

Set the environment; no code is needed:

```bash
CACHE_BACKEND=redis
CACHE_REDIS_KEY_PREFIX=yourapp:
```

`ready` then configures Redis directly. A backend your module registers in `on_load` can be named the same way. If the settings must be built in code, reconfigure **after** cache's `ready`, from your application's `ready`:

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

Call `close()` first so the backend from cache's `ready` is not kept open until shutdown. Declare a dependency on `bedrock.contrib.cache` so your `ready` runs after it.

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

Async shutdown: `close()` releases the sync client and `await cache.aclose()` the async one (Redis owns both). Registry shutdown does both: `bedrock.shutdown()` runs the async close on Bedrock's process-wide runtime loop, `await bedrock.ashutdown()` on the running loop. In a running loop, a sync `registry.shutdown()` raises `ModuleAsyncRequiredError` instead when async clients are open; the module stays retryable. Memory-only apps shut down from any context.

## Service states

```
unconfigured  --configure()/get_backend()-->  active  --close()/aclose()-->  unconfigured
```

| Method | Behavior |
|--------|----------|
| `configure(name=None, settings=None)` | Instantiate the named backend and store it; `None` reads `CACHE_BACKEND` (default `"memory"`). Logs `Cache backend configured: {name}` at INFO. Unknown name → `BackendNotConfiguredError` (the message says `from CACHE_BACKEND` when the name came from the environment). The previous backend is kept for shutdown rather than closed now. |
| `get_backend()` | Return the active backend, or `configure()` if none. |
| `close()` | Close the sync side of the current and replaced backends, then `_backend = None`. Safe if already unconfigured. Raises `CacheCloseError` after attempting every backend. |
| `aclose()` | Close the async side of the current and replaced backends (those with `holds_async_resources`), then `_backend = None`. |
| `has_async_resources()` | `True` while `aclose()` still has a backend to release. |

Calling `configure()` twice without `close()` keeps the previous client open until `close()` / `aclose()` or registry shutdown; release it earlier if connections matter.

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
