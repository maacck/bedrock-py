"""Typed cache namespaces and key slots."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from string import Formatter
from typing import TYPE_CHECKING, Any, TypeVar

from .coder import CacheCoder

if TYPE_CHECKING:
    from .service import CacheService

_T = TypeVar("_T")
_FORMATTER = Formatter()
_MISSING = object()


def _normalize_segment(segment: str) -> str:
    return segment.strip(":")


def _extract_fields(template: str) -> tuple[str, ...]:
    fields: list[str] = []
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if field_name:
            fields.append(field_name)
    return tuple(fields)


class CacheNamespace:
    """Namespace for a related family of cache keys."""

    def __init__(self, prefix: str, *, cache_service: CacheService) -> None:
        normalized = _normalize_segment(prefix)
        if not normalized:
            raise ValueError("Cache namespace prefix cannot be empty.")
        self._prefix = normalized
        self._cache_service = cache_service

    @property
    def prefix(self) -> str:
        """Return the normalized namespace prefix."""
        return self._prefix

    def build_key(self, *segments: str) -> str:
        """Build a fully qualified cache key within this namespace."""
        parts = [self._prefix]
        parts.extend(_normalize_segment(segment) for segment in segments if segment)
        return ":".join(parts)

    def _scan_prefix(self) -> str:
        """Return the prefix used for namespace-wide scans."""
        return f"{self._prefix}:"

    def slot(
        self,
        template: str,
        *,
        value_type: Any | None = None,
        coder: type[CacheCoder[_T]] | None = None,
        ttl: int | None = None,
    ) -> CacheSlot[_T]:
        """Create a typed key slot under this namespace."""
        return CacheSlot(
            namespace=self,
            template=template,
            value_type=value_type,
            coder=coder,
            ttl=ttl,
        )

    def clear(self) -> int:
        """Delete all keys in this namespace."""
        return self._cache_service.clear(self._scan_prefix())

    async def aclear(self) -> int:
        """Asynchronous variant of :meth:`clear`."""
        return await self._cache_service.aclear(self._scan_prefix())

    def list(self, limit: int = 100) -> list[str]:
        """List keys in this namespace."""
        return self._cache_service.list(self._scan_prefix(), limit=limit)

    async def alist(self, limit: int = 100) -> list[str]:
        """Asynchronous variant of :meth:`list`."""
        return await self._cache_service.alist(self._scan_prefix(), limit=limit)


class CacheSlot[T]:
    """Typed cache key template with a stable namespace and default policy."""

    def __init__(
        self,
        *,
        namespace: CacheNamespace,
        template: str,
        value_type: Any | None = None,
        coder: type[CacheCoder[T]] | None = None,
        ttl: int | None = None,
    ) -> None:
        normalized = _normalize_segment(template)
        if not normalized:
            raise ValueError("Cache slot template cannot be empty.")
        if value_type is not None and coder is not None:
            raise ValueError("CacheSlot accepts either 'value_type' or 'coder', not both.")
        self._namespace = namespace
        self._template = normalized
        self._value_type = value_type
        self._coder = coder
        self._ttl = ttl
        self._fields = _extract_fields(normalized)

    @property
    def namespace(self) -> CacheNamespace:
        """Return the namespace that owns this slot."""
        return self._namespace

    @property
    def template(self) -> str:
        """Return the normalized key template."""
        return self._template

    def build_key(self, **params: Any) -> str:
        """Render and fully qualify the slot key."""
        expected = set(self._fields)
        provided = set(params)

        missing = sorted(expected - provided)
        if missing:
            raise ValueError(f"Missing cache key parameters: {', '.join(missing)}.")

        unexpected = sorted(provided - expected)
        if unexpected:
            raise ValueError(f"Unexpected cache key parameters: {', '.join(unexpected)}.")

        return self._namespace.build_key(self._template.format(**params))

    def get(self, default: Any = None, **params: Any) -> T | Any:
        """Read the cached value for this slot."""
        return self._namespace._cache_service.get(
            self.build_key(**params),
            default=default,
            type_=self._value_type,
            coder=self._coder,
        )

    async def aget(self, default: Any = None, **params: Any) -> T | Any:
        """Asynchronous variant of :meth:`get`."""
        return await self._namespace._cache_service.aget(
            self.build_key(**params),
            default=default,
            type_=self._value_type,
            coder=self._coder,
        )

    def get_or_load(
        self,
        loader: Callable[[], T],
        *,
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        **params: Any,
    ) -> T:
        """Return the cached value, or load, store, and return it on a miss."""
        value = self.get(default=_MISSING, **params)
        if value is not _MISSING:
            return value
        loaded = loader()
        self.set(loaded, ex=ex, px=px, ea=ea, **params)
        return loaded

    async def aget_or_load(
        self,
        loader: Callable[[], T] | Callable[[], Awaitable[T]],
        *,
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        **params: Any,
    ) -> T:
        """Asynchronous variant of :meth:`get_or_load`."""
        value = await self.aget(default=_MISSING, **params)
        if value is not _MISSING:
            return value
        loaded = loader()
        if inspect.isawaitable(loaded):
            loaded = await loaded
        await self.aset(loaded, ex=ex, px=px, ea=ea, **params)
        return loaded

    def get_with_ttl(self, **params: Any) -> tuple[T | Any, int | None]:
        """Read the cached value and remaining TTL for this slot."""
        return self._namespace._cache_service.get_with_ttl(
            self.build_key(**params),
            type_=self._value_type,
            coder=self._coder,
        )

    async def aget_with_ttl(self, **params: Any) -> tuple[T | Any, int | None]:
        """Asynchronous variant of :meth:`get_with_ttl`."""
        return await self._namespace._cache_service.aget_with_ttl(
            self.build_key(**params),
            type_=self._value_type,
            coder=self._coder,
        )

    def set(
        self,
        value: T,
        *,
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        **params: Any,
    ) -> None:
        """Write a value for this slot."""
        resolved_ex = self._ttl if ex is None and px is None and ea is None else ex
        self._namespace._cache_service.set(
            self.build_key(**params),
            value,
            ex=resolved_ex,
            px=px,
            ea=ea,
            coder=self._coder,
        )

    async def aset(
        self,
        value: T,
        *,
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        **params: Any,
    ) -> None:
        """Asynchronous variant of :meth:`set`."""
        resolved_ex = self._ttl if ex is None and px is None and ea is None else ex
        await self._namespace._cache_service.aset(
            self.build_key(**params),
            value,
            ex=resolved_ex,
            px=px,
            ea=ea,
            coder=self._coder,
        )

    def delete(self, **params: Any) -> bool:
        """Delete the cached value for this slot."""
        return self._namespace._cache_service.delete(self.build_key(**params))

    async def adelete(self, **params: Any) -> bool:
        """Asynchronous variant of :meth:`delete`."""
        return await self._namespace._cache_service.adelete(self.build_key(**params))

    def exists(self, **params: Any) -> bool:
        """Return whether this slot currently has a value."""
        return self._namespace._cache_service.exists(self.build_key(**params))

    async def aexists(self, **params: Any) -> bool:
        """Asynchronous variant of :meth:`exists`."""
        return await self._namespace._cache_service.aexists(self.build_key(**params))
