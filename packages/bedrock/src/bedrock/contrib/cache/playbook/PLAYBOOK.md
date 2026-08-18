# Bedrock Cache

Pluggable cache with in-memory and Redis backends, distributed locks, and typed namespaces. Values are serialized transparently (orjson) so Python objects round-trip without manual encoding. Configure a backend before use.

## Quick Reference

| Concern | Import |
|---------|--------|
| Cache singleton | `from bedrock.contrib.cache import cache` |
| Register a backend | `from bedrock.contrib.cache import register_backend` |
| Distributed lock | `from bedrock.contrib.cache import CacheLock` |
| Typed namespace | `from bedrock.contrib.cache import CacheNamespace` |

## Setup

```python
from bedrock.contrib.cache import cache

cache.configure("memory")  # default backend; no extra required
cache.configure("redis")   # requires: uv add bedrock-core[cache-redis]

cache.set("user:1", {"name": "Alice"}, ttl=300)
value = cache.get("user:1")  # -> {"name": "Alice"}
```

## Capabilities

Every operation has a sync method and an `a`-prefixed async pair (`get`/`aget`, `set`/`aset`, `delete`/`adelete`, ...).

- `get(key, default=None, type_=None, coder=None)` — decode as a specific type or custom `CacheCoder`.
- `set(key, value, ttl=...)` / `get_or_set(...)` — populate-on-miss with a callable.
- `add(...)` — set only when the key is absent (atomic).
- `compare_and_delete(key, expected)` — delete only when the value matches.
- `get_many` / `set_many`, `incr` / `decr`, `list(prefix)`.
- `clear(prefix=None)` — Redis raises `CacheClearRequiresPrefixError` when neither the configured prefix nor an explicit `prefix` is set (guards against `flushdb()`).
- `namespace(prefix)` — namespaced, typed views.

## Backends

| Backend | Name | Extra | Env prefix |
|---------|------|-------|------------|
| In-memory | `"memory"` | none | `CACHE_MEMORY_` |
| Redis | `"redis"` | `cache-redis` | `CACHE_REDIS_` |

Memory settings: `max_size` (default 10000), `default_ttl` (default 300). Redis settings: `url`, `db`, `password`, `key_prefix`, `max_connections`.

## Distributed Locks

```python
lock = cache.lock("inventory:order-42", timeout=30)
with lock:
    ...  # mutual exclusion across processes (Redis)
```

`acquire(blocking_timeout=None)` returns `bool`; `aacquire` is the async form. `release()` raises `CacheLockOwnershipError` when the token no longer matches (expired or acquired elsewhere). Usable as `with` / `async with`.

## Exceptions

All inherit from `CacheError` (a `BedrockExc`): `BackendNotConfiguredError`, `CacheConnectionError`, `CacheSerializationError`, `CacheLockError` (with `CacheLockAcquisitionError`, `CacheLockOwnershipError`), `CacheClearRequiresPrefixError`.

## Anti-Patterns

- Don't call `clear()` on a shared Redis without a prefix — it can flush the whole database.
- Don't hold a lock longer than its `timeout`.
- Operations before `configure()` raise `BackendNotConfiguredError`.

## See Also

- Guide: `docs-web/content/docs/en/(bedrock)/guides/cache.mdx`
