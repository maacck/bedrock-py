"""DI container with lifetime management, thread-safe singleton resolution."""

import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar, overload

from ._scope import ScopeManager
from .decorators import build_inject, build_provider
from .exc import DuplicateServiceError, ScopeError, ServiceNotFoundError
from .lifetime import Lifetime

T = TypeVar("T")


@dataclass
class _Registration:
    """Internal record for a service registration."""

    factory: Callable[..., Any]
    lifetime: Lifetime


class Container:
    """Lightweight dependency injection container with lifetime management.

    Supports three service lifetimes: SINGLETON, TRANSIENT, and SCOPED.
    Thread-safe singleton creation via double-checked locking.
    """

    def __init__(self) -> None:
        self._registrations: dict[type | str, _Registration] = {}
        self._singletons: dict[type | str, Any] = {}
        self._scope_manager = ScopeManager()
        self._lock = threading.RLock()
        self.provider = build_provider(self)
        self.inject = build_inject(self)

    def register(
        self,
        key: type | str,
        *,
        factory: Callable[..., Any],
        lifetime: Lifetime = Lifetime.SINGLETON,
    ) -> None:
        """Register a service factory.

        Args:
            key: The type or string key to register under.
            factory: A callable that produces instances.
            lifetime: How instances are managed (default: SINGLETON).

        Raises:
            DuplicateServiceError: If ``key`` is already registered.
        """
        if key in self._registrations:
            raise DuplicateServiceError(f"'{key}' is already registered.")
        self._registrations[key] = _Registration(factory=factory, lifetime=lifetime)

    def register_instance(self, key: type | str, instance: Any) -> None:
        """Register a pre-built instance (always treated as SINGLETON).

        Args:
            key: The type or string key to register under.
            instance: The pre-built object.

        Raises:
            DuplicateServiceError: If ``key`` is already registered.
        """
        if key in self._registrations:
            raise DuplicateServiceError(f"'{key}' is already registered.")
        self._registrations[key] = _Registration(factory=lambda: instance, lifetime=Lifetime.SINGLETON)
        self._singletons[key] = instance

    @overload
    def resolve(self, key: type[T]) -> T: ...

    @overload
    def resolve(self, key: str) -> Any: ...

    def resolve(self, key: type | str) -> Any:
        """Resolve a service by key.

        Args:
            key: The type or string key to resolve.

        Returns:
            An instance of the requested service.

        Raises:
            ServiceNotFoundError: If ``key`` is not registered.
            ScopeError: If a SCOPED service is resolved without an active scope.
        """
        reg = self._registrations.get(key)
        if reg is None:
            raise ServiceNotFoundError(f"'{key}' is not registered.")

        if reg.lifetime is Lifetime.SINGLETON:
            return self._resolve_singleton(key, reg)
        if reg.lifetime is Lifetime.SCOPED:
            return self._resolve_scoped(key, reg)
        return reg.factory()

    def _resolve_singleton(self, key: type | str, reg: _Registration) -> Any:
        """Resolve a singleton with double-checked locking.

        Args:
            key: The service key.
            reg: The registration record.

        Returns:
            The cached singleton instance.
        """
        instance = self._singletons.get(key)
        if instance is not None:
            return instance
        with self._lock:
            instance = self._singletons.get(key)
            if instance is not None:
                return instance
            instance = reg.factory()
            self._singletons[key] = instance
            return instance

    def _resolve_scoped(self, key: type | str, reg: _Registration) -> Any:
        """Resolve a scoped service from the current scope frame.

        Args:
            key: The service key.
            reg: The registration record.

        Returns:
            The scoped instance.

        Raises:
            ScopeError: If no scope is currently active.
        """
        cache_key = f"_scoped_{key}"
        existing = self._scope_manager.get(cache_key)
        if existing is not None:
            return existing
        if not self._scope_manager.active:
            raise ScopeError(f"Cannot resolve scoped service '{key}' without an active scope.")
        instance = reg.factory()
        self._scope_manager.set(cache_key, instance)
        return instance

    def is_registered(self, key: type | str) -> bool:
        """Check whether a service key is registered.

        Args:
            key: The type or string key.

        Returns:
            ``True`` if the key has a registration.
        """
        return key in self._registrations

    @contextmanager
    def override(self, key: type | str, instance: Any):
        """Context manager that temporarily replaces a service.

        Works even if the key was not previously registered (useful for tests).
        On exit, the original registration (if any) is restored.

        Args:
            key: The type or string key to override.
            instance: The temporary instance to return.

        Yields:
            None — this is a context manager.
        """
        had_registration = key in self._registrations
        old_registration = self._registrations.get(key)
        had_singleton = key in self._singletons
        old_singleton = self._singletons.get(key)

        self._registrations[key] = _Registration(factory=lambda: instance, lifetime=Lifetime.SINGLETON)
        self._singletons[key] = instance

        try:
            yield
        finally:
            if had_registration and old_registration is not None:
                self._registrations[key] = old_registration
            else:
                self._registrations.pop(key, None)

            if had_singleton and old_singleton is not None:
                self._singletons[key] = old_singleton
            else:
                self._singletons.pop(key, None)

    @contextmanager
    def scope(self, name: str = "default"):
        """Context manager that enters a new DI scope.

        Scoped services resolved within this block share a single instance.
        On exit, services that implement ``.close()`` are cleaned up.

        Args:
            name: Human-readable name for this scope.

        Yields:
            None — this is a context manager.
        """
        token = self._scope_manager.enter(name)
        try:
            yield
        finally:
            frame = self._scope_manager.exit(token)
            for key, value in frame.items():
                if key.startswith("__"):
                    continue
                if hasattr(value, "close"):
                    value.close()

    def reset(self) -> None:
        """Clear all registrations and cached instances.

        Intended for test teardown.
        """
        self._registrations.clear()
        self._singletons.clear()
