"""Typed class registration with explicit, on-demand imports."""

from collections.abc import Mapping
from importlib import import_module
from inspect import isabstract
from threading import Lock
from typing import cast

from .exc import (
    ClassLoadError,
    ClassNotRegisteredError,
    DuplicateRegistrationError,
    InvalidClassPathError,
    InvalidRegisteredClassError,
)


class ClassRegistry[T]:
    """A process-local directory of named concrete subclasses of one base.

    Class objects are validated immediately; lazy paths are imported and
    validated on first lookup. Successful lookups cache classes, never instances.
    Entries can be added concurrently but cannot be replaced or removed. Import
    code runs outside the registry lock. Explicit inheritance is required, even
    for Protocol bases; virtual ABC subclasses and structural-only matches are
    not accepted. This does not check method signatures or provider behavior.
    """

    def __init__(self, *, base_class: type[T], lazy_classes: Mapping[str, str] | None = None) -> None:
        """Create a registry without importing any registered implementations.

        Args:
            base_class: The caller-owned base that implementations must inherit.
            lazy_classes: Optional built-in name-to-path declarations. The mapping
                is copied and validated, so later caller mutations have no effect.

        Raises:
            InvalidRegisteredClassError: If base_class is not a class.
            InvalidClassPathError: If an initial import path is malformed.
        """
        if not isinstance(base_class, type):
            raise InvalidRegisteredClassError("The registry's base_class must be a class.")
        self._base_class = base_class
        self._entries: dict[str, type[T] | str] = {}
        self._lock = Lock()
        if lazy_classes is not None:
            for name, path in lazy_classes.items():
                self.register_lazy(name, path)

    def register(self, name: str, cls: type[T]) -> None:
        """Register a class object without constructing or re-importing it.

        Args:
            name: The unique, case-sensitive implementation name.
            cls: A concrete, explicit child of the registry's base class.

        Raises:
            InvalidRegisteredClassError: If cls does not satisfy the class contract.
            DuplicateRegistrationError: If name is already registered.
        """
        self._add(name, self._validate_class(cls, name=name))

    def register_lazy(self, name: str, path: str) -> None:
        """Register a trusted import path without importing its module.

        Args:
            name: The unique, case-sensitive implementation name.
            path: An absolute module:qualified.attribute path. Parent packages
                must also avoid eager imports of optional implementations.

        Raises:
            InvalidClassPathError: If the path is malformed.
            DuplicateRegistrationError: If name is already registered.
        """
        self._split_path(path)
        self._add(name, path)

    def get(self, name: str) -> type[T]:
        """Return a validated class, importing only the selected entry if needed.

        Failed resolutions are not cached. A subsequent explicit lookup tries
        again; there are no automatic retries. Concurrent successful lookups
        publish one class reference. No registry lock is held during import.

        Args:
            name: A previously registered implementation name.

        Returns:
            The concrete subclass, without creating an instance.

        Raises:
            ClassNotRegisteredError: If name is unknown.
            ClassLoadError: If import or attribute lookup fails; retains the cause.
            InvalidRegisteredClassError: If the loaded value violates the contract.
        """
        with self._lock:
            try:
                entry = self._entries[name]
            except KeyError:
                raise ClassNotRegisteredError(f"No class is registered under '{name}'.") from None
        if not isinstance(entry, str):
            return entry

        module_name, attribute_path = self._split_path(entry)
        try:
            value: object = import_module(module_name)
            for attribute in attribute_path.split("."):
                value = getattr(value, attribute)
        except Exception as exc:
            raise ClassLoadError(f"Unable to load class '{name}' from '{entry}': {exc}") from exc
        resolved = self._validate_class(value, name=name, path=entry)

        with self._lock:
            # Registrations are append-only, so this name still denotes the same
            # entry. Another successful lookup may already have published it.
            current = self._entries[name]
            if isinstance(current, str):
                self._entries[name] = resolved
                return resolved
            return current

    def has(self, name: str) -> bool:
        """Return whether name is registered, without loading its implementation.

        Args:
            name: The implementation name to inspect.
        """
        with self._lock:
            return name in self._entries

    def keys(self) -> list[str]:
        """Return a snapshot of registered names without loading implementations."""
        with self._lock:
            return list(self._entries)

    def _add(self, name: str, entry: type[T] | str) -> None:
        """Atomically add an entry without permitting duplicate names."""
        with self._lock:
            if name in self._entries:
                raise DuplicateRegistrationError(f"A class is already registered under '{name}'.")
            self._entries[name] = entry

    def _validate_class(self, value: object, *, name: str, path: str | None = None) -> type[T]:
        """Validate nominal inheritance without invoking Protocol subclass hooks."""
        if (
            not isinstance(value, type)
            or not any(base is self._base_class for base in value.__mro__[1:])
            or isabstract(value)
            or getattr(value, "_is_protocol", False)
        ):
            location = f" from '{path}'" if path is not None else ""
            expected = f"{self._base_class.__module__}.{self._base_class.__qualname__}"
            raise InvalidRegisteredClassError(
                f"Class '{name}'{location} must be a concrete, explicit subclass of '{expected}'."
            )
        return cast(type[T], value)

    @staticmethod
    def _split_path(path: str) -> tuple[str, str]:
        """Require an absolute module path and a qualified attribute path."""
        if isinstance(path, str) and path.count(":") == 1:
            module_name, attribute_path = path.split(":")
            if all(part.isidentifier() for part in module_name.split(".")) and all(
                part.isidentifier() for part in attribute_path.split(".")
            ):
                return module_name, attribute_path
        raise InvalidClassPathError(f"Invalid class path {path!r}; expected 'module:qualified.attribute'.")
