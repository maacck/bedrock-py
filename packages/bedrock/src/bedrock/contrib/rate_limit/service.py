"""Rate-limit service with dynamic backend loading."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass

from pydantic_settings import BaseSettings

from ...logging import get_logger
from ...provider import ProviderCatalog, ProviderError, ProviderNotFoundError, ProviderSpec
from .base import RateLimitBackend
from .entities import RateLimitDecision, RateLimitPolicy
from .exc import (
    InvalidRateLimitArgumentError,
    RateLimitBackendNotConfiguredError,
    RateLimitCloseError,
    RateLimitPolicyConflictError,
)
from .settings import RateLimitSettings

logger = get_logger(__name__)

_BUILTIN_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "redis", "bedrock.contrib.rate_limit.backends.redis:RedisRateLimitBackend", "bedrock-core[rate-limit-redis]"
    ),
)

_BACKEND_REGISTRY: ProviderCatalog[RateLimitBackend] = ProviderCatalog(protocol=RateLimitBackend, specs=_BUILTIN_SPECS)


@dataclass(eq=False)
class _OpenBackend:
    """A backend instance whose sync and async clients may still hold resources."""

    backend: RateLimitBackend
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


def register_backend(name: str, backend_cls: Callable[..., RateLimitBackend]) -> None:
    """Register an imported rate-limit backend class or factory for :meth:`RateLimitService.configure`.

    To defer importing the module that defines it, use :func:`register_backend_lazy`.

    Args:
        name: Unique backend identifier.
        backend_cls: A backend class, or a factory accepting ``settings=``, whose result structurally implements
            :class:`RateLimitBackend`. Inheritance is not required.

    Raises:
        DuplicateRegistrationError: If ``name`` is already registered to anything else, including the built-in
            ``"redis"`` name (raised as :class:`bedrock.provider.ProviderConflictError`). Registering the same
            object again is a no-op.
        InvalidRegisteredClassError: If ``name`` is empty or ``backend_cls`` is not callable (raised as
            :class:`bedrock.provider.InvalidProviderSpecError`).
    """
    _BACKEND_REGISTRY.register(name, backend_cls)


def register_backend_lazy(name: str, path: str, *, install_requirement: str | None = None) -> None:
    """Register a rate-limit backend by ``module:attribute`` path without importing it.

    The module is imported only when :meth:`RateLimitService.configure` selects ``name``.

    Args:
        name: Unique backend identifier.
        path: Trusted path to a backend class or a factory accepting ``settings=``.
        install_requirement: Requirement named in the configuration error when the import fails.

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


class RateLimitService:
    """Unified rate-limit service with dynamic backend loading.

    Usage::

        from datetime import timedelta
        from bedrock.contrib.rate_limit import (
            RateLimitPolicy,
            rate_limit,
        )

        LOGIN = RateLimitPolicy(
            name="idp.login", limit=5, period=timedelta(minutes=15)
        )

        decision = rate_limit.hit(LOGIN, f"{tenant_id}:{email}")
        if not decision.allowed:
            ...  # raise a domain error carrying decision.retry_after

    The backend named by ``RATE_LIMIT_BACKEND`` (``redis`` when unset) is configured by the module's ``ready``
    hook, or lazily on first use.
    """

    _backend: RateLimitBackend | None

    def __init__(self) -> None:
        self._backend = None
        self._open: list[_OpenBackend] = []
        self._policies: dict[str, RateLimitPolicy] = {}
        self._policies_lock = threading.Lock()

    def configure(self, backend_name: str | None = None, settings: BaseSettings | None = None) -> RateLimitBackend:
        """Configure the service with a registered backend.

        Args:
            backend_name: Registered backend identifier. If ``None``, ``RATE_LIMIT_BACKEND`` selects it,
                defaulting to ``"redis"``.
            settings: Optional settings instance. If ``None``, the backend reads its own default settings.

        Returns:
            The newly configured backend instance.

        Raises:
            RateLimitBackendNotConfiguredError: If the backend name is unknown, cannot be loaded (missing
                dependency named by its install requirement), or does not implement :class:`RateLimitBackend`.
                The provider error is the cause. Any other exception raised by the backend constructor
                propagates unchanged.
        """
        source = ""
        if backend_name is None:
            backend_name = RateLimitSettings().backend
            source = " (from RATE_LIMIT_BACKEND)"
        try:
            self._backend = _BACKEND_REGISTRY.create(backend_name, settings=settings)
        except ProviderNotFoundError as exc:
            available = ", ".join(_BACKEND_REGISTRY.names())
            raise RateLimitBackendNotConfiguredError(
                f"Unknown rate limit backend '{backend_name}'{source}. Available: {available}."
            ) from exc
        except ProviderError as exc:
            raise RateLimitBackendNotConfiguredError(
                f"Failed to load rate limit backend '{backend_name}'{source}: {exc.detail}"
            ) from exc
        self._open.append(_OpenBackend(self._backend))
        logger.info("Rate limit backend configured: {}", backend_name)
        return self._backend

    def get_backend(self) -> RateLimitBackend:
        """Return the current backend, configuring the ``RATE_LIMIT_BACKEND`` backend lazily if needed."""
        if self._backend is None:
            self._backend = self.configure()
        return self._backend

    def list_backends(self) -> list[str]:
        """Return all registered backend names."""
        return list_backends()

    def hit(self, policy: RateLimitPolicy, key: str, cost: int = 1) -> RateLimitDecision:
        """Consume ``cost`` units of ``policy`` for ``key`` if the bucket allows it.

        Denied hits consume nothing. Whether to proceed, wait ``retry_after`` seconds, or fail is the caller's
        decision.

        Args:
            policy: The policy to evaluate.
            key: Caller-chosen identity, e.g. ``f"{tenant_id}:{email}"``. It is hashed before storage.
            cost: Units to consume, from 1 to ``policy.limit``.

        Returns:
            The decision for this hit.

        Raises:
            InvalidRateLimitArgumentError: If ``key`` is empty or ``cost`` is out of range.
            RateLimitPolicyConflictError: If a different policy was already used under ``policy.name``.
            RateLimitBackendError: If the backend cannot evaluate the hit.
        """
        storage_key = self._storage_key(policy, key, cost)
        return self.get_backend().hit(storage_key, policy, cost)

    async def ahit(self, policy: RateLimitPolicy, key: str, cost: int = 1) -> RateLimitDecision:
        """Asynchronous variant of :meth:`hit`."""
        storage_key = self._storage_key(policy, key, cost)
        return await self.get_backend().ahit(storage_key, policy, cost)

    def reset(self, policy: RateLimitPolicy, key: str) -> None:
        """Refill the bucket of ``policy`` for ``key``, e.g. after a successful login.

        Raises:
            InvalidRateLimitArgumentError: If ``key`` is empty.
            RateLimitPolicyConflictError: If a different policy was already used under ``policy.name``.
            RateLimitBackendError: If the backend cannot reset the bucket.
        """
        storage_key = self._storage_key(policy, key, 1)
        self.get_backend().reset(storage_key)

    async def areset(self, policy: RateLimitPolicy, key: str) -> None:
        """Asynchronous variant of :meth:`reset`."""
        storage_key = self._storage_key(policy, key, 1)
        await self.get_backend().areset(storage_key)

    def _storage_key(self, policy: RateLimitPolicy, key: str, cost: int) -> str:
        """Validate a call and return the namespaced, hashed key the backend stores."""
        if not isinstance(key, str) or not key:
            raise InvalidRateLimitArgumentError("Rate limit key must be a non-empty string.")
        if not isinstance(cost, int) or isinstance(cost, bool) or not 1 <= cost <= policy.limit:
            raise InvalidRateLimitArgumentError(
                f"Rate limit cost must be an integer from 1 to {policy.limit} for policy '{policy.name}', got {cost!r}."
            )
        with self._policies_lock:
            known = self._policies.setdefault(policy.name, policy)
        if known != policy:
            raise RateLimitPolicyConflictError(
                f"Rate limit policy '{policy.name}' is already used with limit={known.limit}, "
                f"period={known.period}; policy names must be unique."
            )
        digest = hashlib.blake2b(key.encode(), digest_size=16).hexdigest()
        return f"{policy.name}:{digest}"

    def close(self) -> None:
        """Close the synchronous side of every backend this service has created.

        Backends replaced by :meth:`configure` are closed too. A backend with async resources stays tracked until
        :meth:`aclose` releases them. Every backend is attempted even when one fails; a backend whose close
        failed stays tracked, so the next call attempts it again. The call is safe to repeat.

        Raises:
            RateLimitCloseError: After all attempts, if any backend failed to close.
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

        Backends that declare ``holds_async_resources = False`` are skipped. The call is safe to repeat.

        Raises:
            RateLimitCloseError: After all attempts, if any backend failed to close.
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
        """Return whether :meth:`aclose` still has a backend to release."""
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
            raise RateLimitCloseError(
                f"Failed to close {len(errors)} rate limit backend(s) ({side}): "
                + "; ".join(str(exc) for exc in errors)
            ) from ExceptionGroup("rate limit close failures", errors)


rate_limit = RateLimitService()
