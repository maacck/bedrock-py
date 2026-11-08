"""Cache service with dynamic backend loading."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings

from ...logging import get_logger
from ...provider import ProviderCatalog, ProviderError, ProviderNotFoundError, ProviderSpec
from .base import CacheBackend
from .coder import CacheCoder, Coder
from .exc import BackendNotConfiguredError, CacheCloseError
from .settings import CacheSettings

logger = get_logger(__name__)

if TYPE_CHECKING:
    from .lock import CacheLock
    from .schema import CacheNamespace

_BUILTIN_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("memory", "bedrock.contrib.cache.backends.memory:InMemoryBackend"),
    ProviderSpec("redis", "bedrock.contrib.cache.backends.redis:RedisBackend", "bedrock-core[cache-redis]"),
)

_BACKEND_REGISTRY: ProviderCatalog[CacheBackend] = ProviderCatalog(protocol=CacheBackend, specs=_BUILTIN_SPECS)


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


def register_backend(name: str, backend_cls: Callable[..., CacheBackend]) -> None:
    """Register an imported cache backend class or factory for :meth:`CacheService.configure`.

    The module defining ``backend_cls`` is already imported. To defer that import, and the optional SDK it
    pulls in, use :func:`register_backend_lazy`.

    Args:
        name: Unique backend identifier.
        backend_cls: A backend class, or a factory accepting ``settings=``, whose result structurally
            implements :class:`CacheBackend`. Inheritance is not required; the result is checked by
            :meth:`CacheService.configure`.

    Raises:
        DuplicateRegistrationError: If ``name`` is already registered to anything else, including the
            built-in ``"memory"`` and ``"redis"`` names (raised as :class:`bedrock.provider.ProviderConflictError`).
            Registering the same object again is a no-op.
        InvalidRegisteredClassError: If ``name`` is empty or ``backend_cls`` is not callable (raised as
            :class:`bedrock.provider.InvalidProviderSpecError`).
    """
    _BACKEND_REGISTRY.register(name, backend_cls)


def register_backend_lazy(name: str, path: str, *, install_requirement: str | None = None) -> None:
    """Register a cache backend by ``module:attribute`` path without importing it.

    The module is imported only when :meth:`CacheService.configure` selects ``name``.

    Args:
        name: Unique backend identifier.
        path: Trusted path to a backend class or a factory accepting ``settings=``.
        install_requirement: Requirement named in the configuration error when the import fails,
            e.g. ``"acme-cache[redis]"``.

    Raises:
        DuplicateRegistrationError: If ``name`` is already registered to anything else (raised as
            :class:`bedrock.provider.ProviderConflictError`). Registering an equal path again is a no-op.
        InvalidRegisteredClassError: If the name, path, or requirement is malformed (raised as
            :class:`bedrock.provider.InvalidProviderSpecError`).
    """
    _BACKEND_REGISTRY.register_lazy(name, path, install_requirement=install_requirement)


def list_backends() -> list[str]:
    """Return all registered backend names in sorted order, without importing any backend."""
    return _BACKEND_REGISTRY.names()


class CacheService:
    """Unified cache service with dynamic backend loading.

    Provides a single entry point for all cache operations.
    The backend is loaded lazily on first use or when explicitly configured.

    Usage::

        from bedrock.contrib.cache import cache

        # Auto-configured with the CACHE_BACKEND backend (memory when unset)
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
        backend_name: str | None = None,
        settings: BaseSettings | None = None,
    ) -> CacheBackend:
        """Configure the cache service with a specific backend.

        Args:
            backend_name: Registered backend identifier (``"memory"``, ``"redis"``, or a custom name). If
                ``None``, the ``CACHE_BACKEND`` environment variable selects it, defaulting to ``"memory"``.
            settings: Optional settings instance. If ``None``, the backend reads its own default settings.

        Returns:
            The newly configured backend instance.

        Raises:
            BackendNotConfiguredError: If the backend name is unknown, cannot be loaded (missing dependency
                named by its install requirement), or does not implement :class:`CacheBackend`. The
                provider error is the cause. Any other exception raised by the backend constructor
                propagates unchanged.
        """
        source = ""
        if backend_name is None:
            backend_name = CacheSettings().backend
            source = " (from CACHE_BACKEND)"
        try:
            self._backend = _BACKEND_REGISTRY.create(backend_name, settings=settings)
        except ProviderNotFoundError as exc:
            available = ", ".join(_BACKEND_REGISTRY.names())
            raise BackendNotConfiguredError(
                f"Unknown cache backend '{backend_name}'{source}. Available: {available}. "
                f"Install optional dependencies for additional backends."
            ) from exc
        except ProviderError as exc:
            raise BackendNotConfiguredError(
                f"Failed to load cache backend '{backend_name}'{source}: {exc.detail}"
            ) from exc
        self._open.append(_OpenBackend(self._backend))
        logger.info("Cache backend configured: {}", backend_name)
        return self._backend

    def get_backend(self) -> CacheBackend:
        """Return the current backend, configuring the ``CACHE_BACKEND`` backend lazily if needed."""
        if self._backend is None:
            self._backend = self.configure()
        return self._backend

    def list_backends(self) -> list[str]:
        """Return all registered backend names."""
        return list_backends()

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
