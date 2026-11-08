# Bedrock Cache

Pluggable cache with in-memory and Redis backends, typed namespaces/slots, custom coders, and cache-backed locks. Values are serialized by a coder (default: orjson). Every mutating/read API has a sync method and an `a`-prefixed async pair (`get`/`aget`, `set`/`aset`, `lock.acquire`/`lock.aacquire`, ...).

## Quick Reference

| Concern | Import |
|---------|--------|
| Cache singleton | `from bedrock.contrib.cache import cache` |
| Register a backend | `from bedrock.contrib.cache import register_backend, register_backend_lazy` |
| Typed namespace / slot | `from bedrock.contrib.cache import CacheNamespace, CacheSlot` |
| Coder protocol | `from bedrock.contrib.cache import CacheCoder` |
| Default / bytes coder | `from bedrock.contrib.cache.coder import Coder, BytesCoder` |
| Distributed lock | `from bedrock.contrib.cache import CacheLock` |
| Exceptions | `from bedrock.contrib.cache import CacheError, ...` |

## When to Read References

| Task | File |
|------|------|
| Typed keys, `CacheSlot.get_or_load` | `references/slots.md` |
| `cache.lock(...)` / `CacheLock` | `references/locks.md` |
| `type_`, `CacheCoder`, `BytesCoder` | `references/coders.md` |
| Exception hierarchy and when each is raised | `references/exceptions.md` |
| Module hooks, `configure`/`close`, entry TTL | `references/lifecycle.md` |

Read a reference with:

```bash
bedrock app playbook bedrock.contrib.cache references/slots.md
```

## Setup

```python
from bedrock.contrib.cache import cache

cache.configure()          # backend named by CACHE_BACKEND (default "memory"); also used lazily on first operation
cache.configure("redis")   # explicit name ignores CACHE_BACKEND; requires: uv add bedrock-core[cache-redis]
# INFO: Cache backend configured: memory / redis

cache.set("user:1", {"name": "Alice"}, ex=300)
value = cache.get("user:1")  # -> {"name": "Alice"}
```

Inside a Bedrock app, the cache module's `ready` hook calls `cache.configure()`, so set `CACHE_BACKEND=redis` (plus `CACHE_REDIS_*`) to use Redis without code. An unknown name fails startup with `BackendNotConfiguredError` naming `CACHE_BACKEND`. See `references/lifecycle.md`.

## Custom Backend Registration

Register extensions in `bootstrap.py:on_load` with `register_backend(name, backend_cls)` (an imported class, or a factory taking `settings=`) or `register_backend_lazy(name, "module:attribute", install_requirement=...)` (imported only when `configure()` selects it). The backend implements `CacheBackend` by inheritance or structurally; `configure()` checks member names only, so cover custom backends with contract tests. Registering the same object or path again is a no-op; a different backend under a taken name, including `memory` or `redis`, raises `DuplicateRegistrationError`. Malformed registrations raise `InvalidRegisteredClassError`; both errors are exported from `bedrock.common.registry`.

Backends live in a `bedrock.provider.ProviderCatalog[CacheBackend]` that imports built-in paths on demand. `configure()` translates an unknown name, a failed import (the message names the install requirement, e.g. `bedrock-core[cache-redis]`), or an object that does not implement `CacheBackend` to `BackendNotConfiguredError` with the provider error as the cause. Other constructor errors propagate unchanged. The catalog does not own instances; the service does.

## Core API

`cache` is a `CacheService`. Expiration uses `ex` (seconds), `px` (milliseconds), or `ea` (Unix timestamp) — mutually exclusive. Omit all three to store without expiry.

| Method | Notes |
|--------|-------|
| `get(key, default=None, type_=None, coder=None)` | Miss returns `default`. Pass `type_` **or** `coder`, not both. |
| `set(key, value, ex=None, px=None, ea=None, coder=None)` | Encode then store. |
| `delete(key) → bool` / `exists(key) → bool` | |
| `get_with_ttl(key, type_=None, coder=None) → (value, ttl)` | `ttl` is remaining seconds, or `None` if no expiry / missing. |
| `get_many(keys)` / `set_many(mapping, ex=..., coder=...)` | |
| `incr(key, delta=1)` / `decr(key, delta=1)` | Integer counters. |
| `list(prefix, limit=100)` | Keys matching prefix. |
| `clear(prefix=None) → int` | Redis requires a prefix; see exceptions. |
| `namespace(prefix) → CacheNamespace` | Typed key family. Prefer this over raw strings. |
| `lock(name, expire=30, ...) → CacheLock` | See `references/locks.md`. |
| `close()` / `aclose()` | `close()` releases the sync client of every backend the service created (replaced ones included); `aclose()` releases the async client. Redis needs both. `has_async_resources()` tells whether `aclose()` still has work. |

There is no service-level `get_or_set`. Populate-on-miss belongs on a slot: `CacheSlot.get_or_load`. Atomic set-if-absent (`add`) is a backend primitive used by locks, not a `CacheService` method.

## Namespaces, slots, locks, coders

```python
from pydantic import BaseModel
from bedrock.contrib.cache import cache

class User(BaseModel):
    name: str
    email: str

users = cache.namespace("users")
user_slot = users.slot("user:{user_id}", value_type=User, ttl=300)

user = user_slot.get_or_load(
    lambda: User(name="Alice", email="alice@example.com"),
    user_id="123",
)

with cache.lock("jobs:invoice-42", expire=30, blocking_timeout=5):
    run_job()
```

- Slots: `references/slots.md`
- Locks: `references/locks.md`
- Coders: `references/coders.md`

## Backends

| Backend | Name | Extra | Env prefix | Settings |
|---------|------|-------|------------|----------|
| In-memory | `"memory"` | none | `CACHE_MEMORY_` | `max_size=10000` |
| Redis | `"redis"` | `cache-redis` | `CACHE_REDIS_` | `url`, `db`, `password`, `key_prefix`, `max_connections` |

```python
from bedrock.contrib.cache.backends.redis import RedisCacheSettings

cache.configure("redis", settings=RedisCacheSettings(url="redis://localhost:6379/1", key_prefix="myapp:"))
```

Memory is process-local. Use Redis for multiple processes. Redis `clear()` scans a prefix; it never `FLUSHDB`.

## Lifecycle

| Hook / method | What happens |
|---------------|----------------|
| `ready` | `cache.configure()` → backend named by `CACHE_BACKEND` (default memory) |
| `on_shutdown` | `cache.close()`, then returns `cache.aclose()` when a backend still holds async clients (Redis); the registry awaits it before the module counts as shut down |
| `on_load` | not implemented |
| `cache.configure(name=None, settings=None)` | Replace the active backend; `None` reads `CACHE_BACKEND` (default `"memory"`); logs the backend name at INFO |
| `cache.get_backend()` | Lazy `configure()` if unset |
| `cache.close()` / `aclose()` | Close the sync / async clients; failures raise `CacheCloseError` after every backend was attempted |

Details: `references/lifecycle.md`.

## Exceptions

All inherit from `CacheError` (`BedrockExc`):

| Exception | Typical cause |
|-----------|----------------|
| `BackendNotConfiguredError` | Unknown backend name, or backend class failed to import |
| `CacheClearRequiresPrefixError` | Redis `clear()` with empty `key_prefix` and no `prefix=` |
| `CacheLockError` | Same `CacheLock` instance acquired twice |
| `CacheLockAcquisitionError` | `with` / `async with` could not acquire |
| `CacheLockOwnershipError` | `release()` when not held, expired, or stolen |
| `CacheConnectionError` | Reserved for connection failures |
| `CacheSerializationError` | Reserved for coder failures |
| `ValueError` | `type_`+`coder`, bad lock/slot/namespace params |

Details: `references/exceptions.md`.

## Anti-Patterns

- Reconfiguring keeps the previous backend tracked, so shutdown still closes it, but it keeps its connections until then. Call `close()` / `aclose()` first to release them early.
- Shut down through one family: `bedrock.shutdown()` or `await bedrock.ashutdown()`. A sync `shutdown()` inside a running event loop raises `ModuleAsyncRequiredError` when Redis async clients are open.
- Don't call Redis `clear()` without `CACHE_REDIS_KEY_PREFIX` or an explicit `prefix=`.
- Don't use the in-memory backend for cross-process locks.
- Don't hold a lock longer than `expire` — the key vanishes and `release()` raises `CacheLockOwnershipError`.
- Don't pass both `type_` and `coder`.
- Don't concatenate raw cache keys. Use `namespace` / `slot`.
- Don't import `bedrock.cache`. Canonical path is `bedrock.contrib.cache`.
- Don't treat a cached `0` / `False` / `""` as a miss. `get_or_load` uses a sentinel, not truthiness.

## See Also

- Human guide: `docs-web/content/docs/en/(bedrock)/guides/cache.mdx`
