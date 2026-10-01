# Namespaces and Slots

Use `cache.namespace(prefix)` and `namespace.slot(template)` instead of hardcoded key strings.

## Namespace

```python
from bedrock.contrib.cache import cache

users = cache.namespace("users")
key = users.build_key("profile", "123")  # "users:profile:123"

users.list(limit=50)
users.clear()  # deletes keys under "users:"
```

| Method | Notes |
|--------|-------|
| `build_key(*segments)` | Joins `prefix` + segments with `:`. Empty segments are skipped. Leading/trailing `:` stripped. |
| `slot(template, value_type=None, coder=None, ttl=None)` | Typed key template. Pass `value_type` **or** `coder`, not both. |
| `list(limit=100)` / `alist(...)` | Keys in this namespace. |
| `clear()` / `aclear()` | Delete keys with prefix `"{namespace}:"`. |

Empty prefix raises `ValueError`.

## Slot

A `CacheSlot` binds a format template, optional value type/coder, and a default TTL (`ttl` → `ex` on write).

```python
from pydantic import BaseModel
from bedrock.contrib.cache import cache

class User(BaseModel):
    name: str
    email: str

user_slot = cache.namespace("users").slot("user:{user_id}", value_type=User, ttl=300)

user_slot.set(User(name="Alice", email="alice@example.com"), user_id="123")
user = user_slot.get(user_id="123")          # User instance
exists = user_slot.exists(user_id="123")
user_slot.delete(user_id="123")

key = user_slot.build_key(user_id="123")     # "users:user:123"
```

Template fields are extracted from `{name}` placeholders. Every call that touches a key must pass **exactly** those kwargs:

```python
slot.build_key()                         # ValueError: Missing cache key parameters: user_id.
slot.build_key(user_id="1", extra="x")   # ValueError: Unexpected cache key parameters: extra.
```

### Slot API

| Method | Notes |
|--------|-------|
| `get(default=None, **params)` / `aget(...)` | Decode with the slot's `value_type` or `coder`. |
| `get_or_load(loader, *, ex=None, px=None, ea=None, **params)` | See below. |
| `aget_or_load(loader, ...)` | Async; `loader` may be sync or awaitable. |
| `set(value, *, ex=None, px=None, ea=None, **params)` | Uses slot `ttl` as `ex` unless `ex`/`px`/`ea` is passed. |
| `delete(**params)` / `exists(**params)` | |
| `get_with_ttl(**params) → (value, ttl)` | |
| `build_key(**params)` | Fully qualified key. |

`ttl=None` on the slot plus no `ex`/`px`/`ea` on `set` means the entry does not expire.

## `get_or_load`

Return the cached value. On miss, call `loader()`, store the result, return it.

```python
def fetch_user(user_id: str) -> User:
    ...

user = user_slot.get_or_load(lambda: fetch_user("123"), user_id="123")

# async: sync or async loader
user = await user_slot.aget_or_load(lambda: fetch_user("123"), user_id="123")
user = await user_slot.aget_or_load(fetch_user_async, user_id="123")
```

Rules:

- `loader` takes **no arguments**. Close over ids in a lambda, or use a zero-arg callable.
- Key params stay on the slot call (`user_id=...`), not on the loader.
- Miss is detected with a private sentinel, so cached `0`, `False`, `""`, and empty collections are hits and do **not** re-run `loader`.
- The loaded value is written through `set` / `aset`, so the slot default `ttl` applies unless this call passes `ex`/`px`/`ea`.
- `aget_or_load` awaits the loader result when it is awaitable.
- Not a stampede lock. Concurrent misses can each call `loader`. Use `cache.lock(...)` around the load if you need single-flight.

## Custom coder on a slot

```python
from bedrock.contrib.cache.coder import BytesCoder

token_slot = cache.namespace("tokens").slot("raw:{token_id}", coder=BytesCoder)
token_slot.set(b"opaque-token", token_id="abc")
assert token_slot.get(token_id="abc") == b"opaque-token"
```

Do not pass both `value_type` and `coder` to `slot()` — `ValueError`.
