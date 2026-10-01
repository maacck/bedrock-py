# Distributed Locks

`cache.lock(...)` returns a `CacheLock` backed by the active cache backend. Acquisition uses atomic `add`; release uses compare-and-delete of a per-acquire token.

Cross-process exclusion requires Redis. The in-memory backend only coordinates threads/tasks in **this** process.

## Create

```python
from bedrock.contrib.cache import cache

lock = cache.lock(
    "inventory:order-42",
    expire=30,                 # lock key TTL in seconds (required > 0)
    blocking=True,             # wait if held
    blocking_timeout=5,        # seconds to wait; None = wait forever
    sleep=0.1,                 # retry delay while waiting
    prefix="lock",             # namespace; key becomes "lock:inventory:order-42"
)
```

| Parameter | Default | Notes |
|-----------|---------|-------|
| `name` | required | Logical name. Empty after stripping `:` raises `ValueError`. |
| `expire` | `30` | Backend TTL. When it elapses the lock is gone even if the holder is still in the critical section. |
| `blocking` | `True` | `False` → `acquire()` returns immediately. |
| `blocking_timeout` | `None` | Max wait when blocking. `None` waits indefinitely. |
| `sleep` | `0.1` | Delay between attempts. Must be `> 0`. |
| `prefix` | `"lock"` | Namespace for the lock key. |

Do not import `CacheLock` and construct it by hand unless you already have a `CacheService`. Prefer `cache.lock(...)`.

## Context manager (preferred)

Failed acquire raises `CacheLockAcquisitionError`. Leaving the block always calls `release()` / `arelease()`.

```python
from bedrock.contrib.cache import CacheLockAcquisitionError, cache

with cache.lock("jobs:sync", expire=30, blocking_timeout=5):
    run_job()

async with cache.lock("jobs:async", expire=30, blocking_timeout=5):
    await run_job()

# non-blocking: raises immediately if busy
try:
    with cache.lock("jobs:busy", expire=5, blocking=False):
        run_job()
except CacheLockAcquisitionError:
    skip()
```

## Manual acquire / release

`acquire()` / `aacquire()` return `bool`. They do **not** raise on contention.

```python
from bedrock.contrib.cache import CacheLockOwnershipError, cache

lock = cache.lock("jobs:manual", expire=30, blocking=False)

if lock.acquire():
    try:
        run_job()
    finally:
        try:
            lock.release()
        except CacheLockOwnershipError:
            # expired or overwritten; this instance no longer owns it
            ...
```

`acquire(blocking=..., blocking_timeout=..., sleep=...)` overrides the instance defaults for that call.

## API

| Method | Returns / raises |
|--------|------------------|
| `acquire(...)` / `aacquire(...)` | `True` if owned, `False` if not. Raises `CacheLockError` if **this instance** already holds it. |
| `release()` / `arelease()` | Raises `CacheLockOwnershipError` if not acquired, or if the stored token no longer matches. |
| `locked()` / `alocked()` | Whether the lock **key** exists (any holder). |
| `key` | Fully qualified cache key. |

## Semantics

- One `CacheLock` instance is not re-entrant. A second `acquire()` on the same instance raises `CacheLockError`.
- Two instances with the same `name` contend for the same key.
- `release()` first forgets the local token, then compare-and-deletes. If the key expired or another writer overwrote it, you get `CacheLockOwnershipError`.
- `locked()` is not "I hold it"; it is "the key exists".
- Keep the critical section shorter than `expire`. There is no watchdog / auto-extend.

## Anti-patterns

- Don't use memory-backend locks to coordinate Celery workers or multiple gunicorn processes.
- Don't swallow `CacheLockAcquisitionError` from a `with` block if the job must run exactly once — retry or fail.
- Don't call `release()` on a lock you never acquired.
- Don't reuse one instance for a second critical section after a failed `release()` without creating a new `cache.lock(...)`.
