from decimal import Decimal
from typing import Any, Protocol, TypeVar

import orjson
from pydantic import BaseModel, TypeAdapter

_T = TypeVar("_T")


class CacheCoder(Protocol[_T]):
    """Protocol for cache coders used to serialize typed cache values."""

    @classmethod
    def encode(cls, value: _T) -> bytes: ...

    @classmethod
    def decode(cls, value: bytes) -> _T: ...


def default_encoder(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


class Coder:
    @classmethod
    def encode(cls, value: Any) -> bytes:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        return orjson.dumps(value, default=default_encoder)

    @classmethod
    def decode(cls, value: bytes) -> Any:
        return orjson.loads(value)

    @classmethod
    def decode_as_type(cls, value: bytes, *, type_: Any | None) -> Any:
        """Decode value to the specific given type

        The default implementation uses the Pydantic model system to convert the value.

        """
        result = cls.decode(value)
        if type_ is not None:
            return TypeAdapter(type_).validate_python(result)
        return result


class BytesCoder:
    """Identity coder for callers that intentionally cache raw bytes."""

    @classmethod
    def encode(cls, value: bytes) -> bytes:
        return value

    @classmethod
    def decode(cls, value: bytes) -> bytes:
        return value
