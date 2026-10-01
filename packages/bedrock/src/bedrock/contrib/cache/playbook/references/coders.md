# Coders

Backends store `bytes`. `CacheService` encodes on write and decodes on read. A slot can pin a `value_type` or a `coder` so callers don't pass it every time.

## Default: `Coder`

Used when neither `type_` nor `coder` is passed.

- `encode`: Pydantic `BaseModel` → `model_dump(mode="json")`, then `orjson.dumps`. `Decimal` becomes `str`.
- `decode`: `orjson.loads` → Python objects (`dict`, `list`, `str`, `int`, ...).

```python
from bedrock.contrib.cache import cache

cache.set("user:123", {"name": "Alice"})
cache.get("user:123")  # {"name": "Alice"}
```

## Decode to a type: `type_`

`get(..., type_=T)` runs `Coder.decode` then Pydantic `TypeAdapter(T).validate_python`.

```python
from pydantic import BaseModel
from bedrock.contrib.cache import cache

class User(BaseModel):
    name: str
    email: str

cache.set("user:123", {"name": "Alice", "email": "alice@example.com"})
user = cache.get("user:123", type_=User)  # User
```

Prefer pinning this on a slot:

```python
user_slot = cache.namespace("users").slot("user:{user_id}", value_type=User, ttl=300)
user_slot.set(User(name="Alice", email="a@b.c"), user_id="123")
user_slot.get(user_id="123")  # User
```

`type_` works for scalars and typing constructs too (`int`, `dict[str, int]`, ...).

## Custom `CacheCoder`

`CacheCoder` is a `Protocol`. Implement classmethods `encode` / `decode`. Inheritance is optional.

```python
from bedrock.contrib.cache import CacheCoder, cache

class MsgPackCoder:
    @classmethod
    def encode(cls, value: dict) -> bytes:
        import msgpack
        return msgpack.packb(value)

    @classmethod
    def decode(cls, value: bytes) -> dict:
        import msgpack
        return msgpack.unpackb(value)

cache.set("data:key", {"foo": "bar"}, coder=MsgPackCoder)
cache.get("data:key", coder=MsgPackCoder)
```

On a slot, pass `coder=` instead of `value_type=`:

```python
data_slot = cache.namespace("data").slot("blob:{id}", coder=MsgPackCoder)
```

Pass **either** `type_` **or** `coder` on a service call, and **either** `value_type` **or** `coder` on `slot()`. Both → `ValueError`.

## Raw bytes: `BytesCoder`

Identity coder for values that are already `bytes`.

```python
from bedrock.contrib.cache.coder import BytesCoder
from bedrock.contrib.cache import cache

token_slot = cache.namespace("tokens").slot("raw:{token_id}", coder=BytesCoder)
token_slot.set(b"opaque-token", token_id="abc")
token_slot.get(token_id="abc")  # b"opaque-token"
```

Without `BytesCoder`, `bytes` go through orjson and will not round-trip as `bytes`.

## Rules

- Encode and decode must use the **same** coder. Mixing `Coder` write with `MsgPackCoder` read corrupts the value.
- `set_many(..., coder=...)` applies that coder to every value in the mapping.
- First-party backends never see Python objects — only the encoded `bytes`.
