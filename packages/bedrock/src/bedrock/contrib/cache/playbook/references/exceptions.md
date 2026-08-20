# Exceptions

Import from `bedrock.contrib.cache`. All cache errors subclass `CacheError`, which subclasses `BedrockExc` (`detail` class attribute, optional `msg` override).

```
CacheError
├── BackendNotConfiguredError
├── CacheClearRequiresPrefixError
├── CacheConnectionError
├── CacheSerializationError
└── CacheLockError
    ├── CacheLockAcquisitionError
    └── CacheLockOwnershipError
```

Catch `CacheError` for any cache failure; catch a leaf when the recovery path differs.

## Raised by first-party code

| Exception | When |
|-----------|------|
| `BackendNotConfiguredError` | `configure(name)` with an unknown backend, or the backend class cannot be imported (missing extra, e.g. Redis without `bedrock-core[cache-redis]`). |
| `CacheClearRequiresPrefixError` | Redis `clear()` / `aclear()` when both `CACHE_REDIS_KEY_PREFIX` / `key_prefix` and the `prefix=` argument are empty. Guards against wiping the whole DB. |
| `CacheLockError` | `acquire()` / `aacquire()` on an instance that already holds the lock (not re-entrant). |
| `CacheLockAcquisitionError` | `with lock:` / `async with lock:` when `acquire()` returned `False` (busy, or `blocking_timeout` elapsed). Manual `acquire()` returns `False` instead. |
| `CacheLockOwnershipError` | `release()` / `arelease()` when this instance has no token, or compare-and-delete failed (TTL expired, key overwritten). |

```python
from bedrock.contrib.cache import (
    BackendNotConfiguredError,
    CacheClearRequiresPrefixError,
    CacheError,
    CacheLockAcquisitionError,
    CacheLockError,
    CacheLockOwnershipError,
    cache,
)

try:
    with cache.lock("jobs:sync", expire=10, blocking_timeout=1):
        run_job()
except CacheLockAcquisitionError:
    retry_later()
```

## Defined but not currently raised by first-party backends

| Exception | Intent |
|-----------|--------|
| `CacheConnectionError` | Backend cannot connect. Redis client errors currently propagate as the driver exception. |
| `CacheSerializationError` | Encode/decode failure. orjson / Pydantic errors currently propagate as-is. |

Custom backends and application code may raise these. Catch `CacheError` if you want to cover them later without changing call sites.

## `ValueError` (not a `CacheError`)

| Situation | Message pattern |
|-----------|-----------------|
| `type_` and `coder` both passed to `get` / `get_with_ttl` | `Pass either 'type_' or 'coder', not both.` |
| `value_type` and `coder` both passed to `slot()` | `CacheSlot accepts either 'value_type' or 'coder', not both.` |
| Empty namespace prefix / slot template / lock name / lock prefix | `... cannot be empty.` |
| Lock `expire <= 0` or `sleep <= 0` | `Cache lock expire/sleep must be greater than zero.` |
| Negative `blocking_timeout` | `Cache lock blocking_timeout cannot be negative.` |
| Slot kwargs missing or extra | `Missing/Unexpected cache key parameters: ...` |
| In-memory `incr` on a non-integer value | `Value for key '...' is not an integer.` |

## Not an error

- `get` miss → `default` (`None` unless you pass another value).
- `delete` on a missing key → `False`.
- Manual `lock.acquire()` on contention → `False` (only the context manager raises).
- First cache operation without `configure()` → lazy memory backend, not `BackendNotConfiguredError`.
