"""Cache service with dynamic backend loading."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings

from ...common.registry import ClassRegistry, ClassRegistryError
from ...logging import get_logger
from .base import CacheBackend
from .coder import CacheCoder, Coder
from .exc import BackendNotConfiguredError, CacheCloseError

logger = get_logger(__name__)

if TYPE_CHECKING:
    from .lock import CacheLock
    from .schema import CacheNamespace

_BACKEND_REGISTRY = ClassRegistry[CacheBackend](
    base_class=CacheBackend,
    lazy_classes={
        "memory": "bedrock.contrib.cache.backends.memory:InMemoryBackend",
        "redis": "bedrock.contrib.cache.backends.redis:RedisBackend",
    },
)


@dataclass(eq=False)
class _OpenBackend:
    """A backend instance whose sync and async clients may still hold resources."""

    backend: CacheBackend
    sync_open: bool = True
    async_open: bool = True

    @property
    def async_pending(self) -> bool:
        """Return whether the async side still has something to close."""
        return self.async_open and getattr(self.backend, "holds_async_resources", True)

    @property
    def finished(self) -> bool:
        """Return whether both sides are closed."""
        return not self.sync_open and not self.async_pending


def register_backend(name: str, backend_cls: type[CacheBackend]) -> None:
    """Register a cache backend with the global registry.

    Args:
        name: Unique backend identifier (e.g. ``"redis"``).
        backend_cls: A concrete class that explicitly subclasses :class:`CacheBackend`.
            Structural conformance alone is not enough.

    Raises:
        DuplicateRegistrationError: If ``name`` is already registered, including
            the built-in ``"memory"`` and ``"redis"`` names.
        InvalidRegisteredClassError: If ``backend_cls`` is not a concrete
            subclass of :class:`CacheBackend`.
    """
    _BACKEND_REGISTRY.register(name, backend_cls)


def list_backends() -> list[str]:
    """Return all registered backend names."""
    return sorted(_BACKEND_REGISTRY.keys())


class CacheService:
    """Unified cache service with dynamic backend loading.

    Provides a single entry point for all cache operations.
    The backend is loaded lazily on first use or when explicitly configured.

    Usage::

        from bedrock.contrib.cache import cache

        # Auto-configured with memory backend
        cache.set("key", "value")

        # Or explicitly configure
        cache.configure("redis")
    """

    _backend: CacheBackend | None

    def __init__(self) -> None:
        self._backend = None
        self._open: list[_OpenBackend] = []

    def configure(
        self,
        backend_name: str = "memory",
        settings: BaseSettings | None = None,
    ) -> CacheBackend:
        """Configure the cache service with a specific backend.

        Args:
            backend_name: Backend identifier (``"memory"``, ``"redis"``).
            settings: Optional settings instance. If ``None``, default settings are used.

        Returns:
            The newly configured backend instance.

        Raises:
            BackendNotConfiguredError: If the backend name is unknown, or its class
                cannot be loaded or is not a valid :class:`CacheBackend` subclass.
        """
        if not _BACKEND_REGISTRY.has(backend_name):
            available = ", ".join(sorted(_BACKEND_REGISTRY.keys()))
            raise BackendNotConfiguredError(
                f"Unknown cache backend '{backend_name}'. Available: {available}. "
                f"Install optional dependencies for additional backends."
            )

        try:
            backend_cls = _BACKEND_REGISTRY.get(backend_name)
        except ClassRegistryError as exc:
            raise BackendNotConfiguredError(
                f"Failed to load cache backend '{backend_name}': {exc.detail} "
                f"Install the required optional dependencies."
            ) from exc

        self._backend = backend_cls(settings=settings)
        self._open.append(_OpenBackend(self._backend))
        logger.info("Cache backend configured: {}", backend_name)
        return self._backend

    def get_backend(self) -> CacheBackend:
        """Return the current backend, initializing lazily if needed."""
        if self._backend is None:
            self._backend = self.configure("memory")
        return self._backend

    def list_backends(self) -> list[str]:
        """Return all registered backend names."""
        return sorted(_BACKEND_REGISTRY.keys())

    @staticmethod
    def _decode_value(
        value: bytes,
        *,
        type_: Any | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> Any:
        if type_ is not None and coder is not None:
            raise ValueError("Pass either 'type_' or 'coder', not both.")
        if coder is not None:
            return coder.decode(value)
        if type_ is not None:
            return Coder.decode_as_type(value, type_=type_)
        return Coder.decode(value)

    @staticmethod
    def _encode_value(
        value: Any,
        *,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> bytes:
        active_coder = coder or Coder
        return active_coder.encode(value)

    def get(
        self,
        key: str,
        default: Any = None,
        type_: Any | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> Any:
        """Retrieve a cached value by key."""
        result = self.get_backend().get(key)
        if result is None:
            return default
        return self._decode_value(result, type_=type_, coder=coder)

    async def aget(
        self,
        key: str,
        default: Any = None,
        type_: Any | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> Any:
        """Asynchronous variant of :meth:`get`."""
        result = await self.get_backend().aget(key)
        if result is None:
            return default
        return self._decode_value(result, type_=type_, coder=coder)

    def get_with_ttl(
        self,
        key: str,
        type_: Any | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> tuple[Any, int | None]:
        """Retrieve a cached value along with its remaining TTL."""
        result, ttl = self.get_backend().get_with_ttl(key)
        if result is None:
            return None, ttl
        return self._decode_value(result, type_=type_, coder=coder), ttl

    async def aget_with_ttl(
        self,
        key: str,
        type_: Any | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> tuple[Any, int | None]:
        """Asynchronous variant of :meth:`get_with_ttl`."""
        result, ttl = await self.get_backend().aget_with_ttl(key)
        if result is None:
            return None, ttl
        return self._decode_value(result, type_=type_, coder=coder), ttl

    def set(
        self,
        key: str,
        value: Any,
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key.
            value: Value to store.
            ex: Expiration time in seconds. Mutually exclusive with ``px`` and ``ea``.
            px: Expiration time in milliseconds. Mutually exclusive with ``ex`` and ``ea``.
            ea: Absolute Unix timestamp for expiration. Mutually exclusive with ``ex`` and ``px``.
        """
        self.get_backend().set(key, self._encode_value(value, coder=coder), ex=ex, px=px, ea=ea)

    async def aset(
        self,
        key: str,
        value: Any,
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> None:
        """Asynchronous variant of :meth:`set`.

        Args:
            key: Cache key.
            value: Value to store.
            ex: Expiration time in seconds. Mutually exclusive with ``px`` and ``ea``.
            px: Expiration time in milliseconds. Mutually exclusive with ``ex`` and ``ea``.
            ea: Absolute Unix timestamp for expiration.
        """
        await self.get_backend().aset(
            key,
            self._encode_value(value, coder=coder),
            ex=ex,
            px=px,
            ea=ea,
        )

    def delete(self, key: str) -> bool:
        """Remove a key from the cache."""
        return self.get_backend().delete(key)

    async def adelete(self, key: str) -> bool:
        """Asynchronous variant of :meth:`delete`."""
        return await self.get_backend().adelete(key)

    def exists(self, key: str) -> bool:
        """Check whether a key exists in the cache."""
        return self.get_backend().exists(key)

    async def aexists(self, key: str) -> bool:
        """Asynchronous variant of :meth:`exists`."""
        return await self.get_backend().aexists(key)

    def clear(self, prefix: str | None = None) -> int:
        """Remove all entries from the cache, optionally filtered by prefix."""
        return self.get_backend().clear(prefix)

    async def aclear(self, prefix: str | None = None) -> int:
        """Asynchronous variant of :meth:`clear`."""
        return await self.get_backend().aclear(prefix)

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Retrieve multiple keys in one call."""
        raw = self.get_backend().get_many(keys)
        return {k: Coder.decode(v) for k, v in raw.items() if v is not None}

    async def aget_many(self, keys: list[str]) -> dict[str, Any]:
        """Asynchronous variant of :meth:`get_many`."""
        raw = await self.get_backend().aget_many(keys)
        return {k: Coder.decode(v) for k, v in raw.items() if v is not None}

    def set_many(
        self,
        mapping: dict[str, Any],
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> None:
        """Store multiple key-value pairs in one call."""
        encoded_mapping = {key: self._encode_value(value, coder=coder) for key, value in mapping.items()}
        self.get_backend().set_many(encoded_mapping, ex=ex, px=px, ea=ea)

    async def aset_many(
        self,
        mapping: dict[str, Any],
        ex: int | None = None,
        px: int | None = None,
        ea: float | None = None,
        coder: type[CacheCoder[Any]] | None = None,
    ) -> None:
        """Asynchronous variant of :meth:`set_many`."""
        encoded_mapping = {key: self._encode_value(value, coder=coder) for key, value in mapping.items()}
        await self.get_backend().aset_many(encoded_mapping, ex=ex, px=px, ea=ea)

    def list(self, prefix: str, limit: int = 100) -> list[str]:
        """List cache keys matching a prefix."""
        return self.get_backend().list(prefix, limit)

    async def alist(self, prefix: str, limit: int = 100) -> list[str]:
        """Asynchronous variant of :meth:`list`."""
        return await self.get_backend().alist(prefix, limit)

    def incr(self, key: str, delta: int = 1) -> int:
        """Increment a numeric cache value."""
        return self.get_backend().incr(key, delta)

    async def aincr(self, key: str, delta: int = 1) -> int:
        """Asynchronous variant of :meth:`incr`."""
        return await self.get_backend().aincr(key, delta)

    def decr(self, key: str, delta: int = 1) -> int:
        """Decrement a numeric cache value."""
        return self.get_backend().decr(key, delta)

    async def adecr(self, key: str, delta: int = 1) -> int:
        """Asynchronous variant of :meth:`decr`."""
        return await self.get_backend().adecr(key, delta)

    def close(self) -> None:
        """Close the synchronous side of every backend this service has created.

        Backends that were replaced by :meth:`configure` are closed too, so no client is stranded.
        A backend with async resources stays tracked until :meth:`aclose` releases them; see
        :meth:`has_async_resources`. Every backend is attempted even when one fails; a backend whose
        close failed stays current or tracked, so the next call attempts it again. The call is safe to repeat.

        Raises:
            CacheCloseError: After all attempts, if any backend failed to close.
        """
        self._track_current()
        failed: list[tuple[_OpenBackend, Exception]] = []
        for entry in list(self._open):
            if not entry.sync_open:
                continue
            try:
                entry.backend.close()
            except Exception as exc:
                failed.append((entry, exc))
            else:
                entry.sync_open = False
        self._settle(failed, "sync")

    async def aclose(self) -> None:
        """Close the asynchronous side of every backend this service has created.

        Counterpart of :meth:`close` for async clients, such as the Redis asyncio client. Backends
        that declare ``holds_async_resources = False`` are skipped. A backend whose sync side is still
        open stays tracked until :meth:`close` releases it. The call is safe to repeat.

        Raises:
            CacheCloseError: After all attempts, if any backend failed to close.
        """
        self._track_current()
        failed: list[tuple[_OpenBackend, Exception]] = []
        for entry in list(self._open):
            if not entry.async_pending:
                continue
            try:
                await entry.backend.aclose()
            except Exception as exc:
                failed.append((entry, exc))
            else:
                entry.async_open = False
        self._settle(failed, "async")

    def has_async_resources(self) -> bool:
        """Return whether :meth:`aclose` still has a backend to release.

        Lifecycle hooks use this to decide whether shutdown needs to await anything.
        """
        self._track_current()
        return any(entry.async_pending for entry in self._open)

    def _track_current(self) -> None:
        """Make sure a backend assigned without :meth:`configure` is tracked for closing."""
        if self._backend is not None and all(entry.backend is not self._backend for entry in self._open):
            self._open.append(_OpenBackend(self._backend))

    def _settle(self, failed: list[tuple[_OpenBackend, Exception]], side: str) -> None:
        """Forget finished backends, drop the current one unless its close failed, and report failures."""
        self._open = [entry for entry in self._open if not entry.finished]
        if self._backend is not None and all(entry.backend is not self._backend for entry, _ in failed):
            self._backend = None
        if failed:
            errors = [exc for _, exc in failed]
            raise CacheCloseError(
                f"Failed to close {len(errors)} cache backend(s) ({side}): " + "; ".join(str(exc) for exc in errors)
            ) from ExceptionGroup("cache close failures", errors)

    def namespace(self, prefix: str) -> CacheNamespace:
        """Create a typed cache namespace bound to this service."""
        from .schema import CacheNamespace

        return CacheNamespace(prefix=prefix, cache_service=self)

    def lock(
        self,
        name: str,
        *,
        expire: int = 30,
        blocking: bool = True,
        blocking_timeout: float | None = None,
        sleep: float = 0.1,
        prefix: str = "lock",
    ) -> CacheLock:
        """Create a cache-backed distributed lock.

        Args:
            name: Logical lock name within the chosen namespace.
            expire: Lock TTL in seconds.
            blocking: Whether acquisition should wait for the lock.
            blocking_timeout: Maximum number of seconds to wait while
                acquiring the lock.
            sleep: Delay between acquisition attempts while waiting.
            prefix: Namespace prefix used to build the internal lock key.

        Returns:
            A configured :class:`bedrock.contrib.cache.lock.CacheLock`
            instance bound to this cache service.
        """
        from .lock import CacheLock

        return CacheLock(
            cache_service=self,
            name=name,
            expire=expire,
            blocking=blocking,
            blocking_timeout=blocking_timeout,
            sleep=sleep,
            prefix=prefix,
        )


cache = CacheService()
