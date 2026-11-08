"""Named providers that can be registered explicitly or lazily and are imported only when used."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import import_module
from threading import Lock
from typing import cast

from .exc import (
    InvalidProviderError,
    InvalidProviderSpecError,
    ProviderConflictError,
    ProviderError,
    ProviderLoadError,
    ProviderNotFoundError,
)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Lazy registration record: a provider available by name whose module is imported only when used.

    Attributes:
        name: Unique, case-sensitive provider name.
        path: Trusted ``module:qualified.attribute`` path to a class or factory function.
        install_requirement: Requirement named in load errors, e.g. ``bedrock-core[cache-redis]``.
    """

    name: str
    path: str
    install_requirement: str | None = None

    def __post_init__(self) -> None:
        """Validate the record without importing anything.

        Raises:
            InvalidProviderSpecError: If the name, path, or install requirement is malformed.
        """
        _validate_name(self.name)
        _split_path(self.path)
        if self.install_requirement is not None and (
            not isinstance(self.install_requirement, str) or not self.install_requirement.strip()
        ):
            raise InvalidProviderSpecError(f"Provider '{self.name}' install_requirement must be a non-empty string.")


# A registered entry: a lazy spec, or the class or factory registered explicitly.
type _Entry = ProviderSpec | Callable[..., object]


class ProviderCatalog[P]:
    """Append-only directory of named providers bound to one protocol.

    Two registration modes: :meth:`register` records an already-imported class or factory;
    :meth:`register_lazy` records only a path, and nothing is imported until the name is resolved.
    Listing never imports. :meth:`resolve` returns the registered class or factory without calling it,
    so the application can construct the provider itself (for example with credentials it loaded).
    :meth:`create` calls the factory outside the lock and checks the result with
    ``isinstance(result, protocol)``. Resolved lazy factories are cached; instances never are, and the
    catalog does not own them. For a ``@runtime_checkable`` Protocol the check only proves the members
    exist, not their signatures or whether they are async.
    """

    def __init__(self, *, protocol: type[P], specs: Iterable[ProviderSpec] = ()) -> None:
        """Create a catalog without importing any provider.

        Args:
            protocol: The class or ``@runtime_checkable`` Protocol that created providers must implement.
            specs: Lazy registrations to record up front, such as built-in providers.

        Raises:
            ProviderError: If ``protocol`` cannot be used with ``isinstance``.
            InvalidProviderSpecError: If an item of ``specs`` is not a ProviderSpec.
            ProviderConflictError: If two items of ``specs`` disagree about the same name.
        """
        try:
            isinstance(object(), protocol)
        except TypeError as exc:
            raise ProviderError(
                f"Catalog protocol '{getattr(protocol, '__qualname__', protocol)}' must be a class; "
                "Protocols must be decorated with @runtime_checkable."
            ) from exc
        self._protocol = protocol
        self._entries: dict[str, _Entry] = {}
        self._factories: dict[str, Callable[..., object]] = {}
        self._lock = Lock()
        for spec in specs:
            if not isinstance(spec, ProviderSpec):
                raise InvalidProviderSpecError(f"Expected a ProviderSpec, got {type(spec).__qualname__}.")
            self._add(spec.name, spec)

    def register(self, name: str, factory: Callable[..., P]) -> None:
        """Explicit mode: record an imported class or factory. Registering the same object again is a no-op.

        Args:
            name: Unique, case-sensitive provider name.
            factory: A class or function returning an object that implements the catalog protocol.

        Raises:
            InvalidProviderSpecError: If ``name`` is empty or ``factory`` is not callable.
            ProviderConflictError: If ``name`` is already registered to anything else, including a lazy path.
        """
        _validate_name(name)
        if not callable(factory):
            raise InvalidProviderSpecError(f"Provider '{name}' factory must be callable.")
        self._add(name, factory)

    def register_lazy(self, name: str, path: str, *, install_requirement: str | None = None) -> None:
        """Lazy mode: record a path without importing it. Registering an equal spec again is a no-op.

        Args:
            name: Unique, case-sensitive provider name.
            path: Trusted ``module:qualified.attribute`` path to a class or factory function.
            install_requirement: Requirement named in load errors, e.g. ``"acme-cache[redis]"``.

        Raises:
            InvalidProviderSpecError: If the name, path, or install requirement is malformed.
            ProviderConflictError: If ``name`` is already registered to anything else, including an explicit factory.
        """
        self._add(name, ProviderSpec(name, path, install_requirement))

    def has(self, name: str) -> bool:
        """Return whether ``name`` is registered, without importing anything.

        Args:
            name: The provider name to look up.
        """
        with self._lock:
            return name in self._entries

    def names(self) -> list[str]:
        """Return the registered names in sorted order, without importing anything."""
        with self._lock:
            return sorted(self._entries)

    def resolve(self, name: str) -> Callable[..., P]:
        """Return the class or factory registered under ``name`` without calling or validating it.

        Lazy entries are imported on first use and the resolved callable is cached. The caller constructs the
        provider and owns the result; use :meth:`create` to get an instance checked against the catalog protocol.

        Args:
            name: A registered provider name.

        Raises:
            ProviderNotFoundError: If ``name`` is unknown; the message lists available names.
            ProviderLoadError: If a lazy path cannot be imported or resolved, or resolves to a non-callable.
                The message names the install requirement when there is one; the original error is the cause.
        """
        entry = self._entry(name)
        return cast(Callable[..., P], self._resolve(entry) if isinstance(entry, ProviderSpec) else entry)

    def create(self, name: str, /, **kwargs: object) -> P:
        """Call the factory registered under ``name`` with ``kwargs`` and validate the result.

        Args:
            name: A registered provider name.
            **kwargs: Arguments for the factory, such as application-provided settings.

        Returns:
            A new provider instance owned by the caller.

        Raises:
            ProviderNotFoundError: If ``name`` is unknown; the message lists available names.
            ProviderLoadError: If a lazy path cannot be imported or resolved, resolves to a non-callable,
                or the factory raises ImportError (missing optional dependency). The original error is the cause.
            InvalidProviderError: If the result is not an instance of the catalog protocol.

        Any other exception raised by the factory propagates unchanged.
        """
        entry = self._entry(name)
        factory = self._resolve(entry) if isinstance(entry, ProviderSpec) else entry
        try:
            provider = factory(**kwargs)
        except ImportError as exc:
            raise ProviderLoadError(_load_message(name, entry, exc)) from exc
        if not isinstance(provider, self._protocol):
            expected = f"{self._protocol.__module__}.{self._protocol.__qualname__}"
            raise InvalidProviderError(
                f"Provider '{name}' returned {type(provider).__qualname__}, which does not implement '{expected}'."
            )
        return cast(P, provider)

    def _entry(self, name: str) -> _Entry:
        """Return the entry for ``name`` or raise ProviderNotFoundError."""
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            available = ", ".join(self.names()) or "none"
            raise ProviderNotFoundError(f"No provider is registered under '{name}'. Available: {available}.")
        return entry

    def _add(self, name: str, entry: _Entry) -> None:
        """Atomically add ``entry``; an identical entry is a no-op and anything else conflicts."""
        with self._lock:
            current = self._entries.setdefault(name, entry)
        if isinstance(current, ProviderSpec) or isinstance(entry, ProviderSpec):
            same = current == entry
        else:
            same = current is entry
        if not same:
            raise ProviderConflictError(f"A different provider is already registered under '{name}'.")

    def _resolve(self, spec: ProviderSpec) -> Callable[..., object]:
        """Import and cache the callable named by ``spec``; the import runs outside the lock."""
        with self._lock:
            cached = self._factories.get(spec.name)
        if cached is not None:
            return cached
        module_name, attribute_path = _split_path(spec.path)
        try:
            value: object = import_module(module_name)
            for attribute in attribute_path.split("."):
                value = getattr(value, attribute)
        except Exception as exc:
            raise ProviderLoadError(_load_message(spec.name, spec, exc)) from exc
        if not callable(value):
            raise ProviderLoadError(f"Provider '{spec.name}' path '{spec.path}' does not name a callable.")
        with self._lock:
            # Entries are append-only, so the name still denotes the same spec.
            return self._factories.setdefault(spec.name, value)


def _load_message(name: str, entry: _Entry, exc: BaseException) -> str:
    """Describe a load failure, naming the install requirement of a lazy entry when it has one."""
    if isinstance(entry, ProviderSpec):
        message = f"Unable to load provider '{name}' from '{entry.path}': {exc}."
        if entry.install_requirement is not None:
            message += f" Install '{entry.install_requirement}'."
        return message
    return f"Unable to load provider '{name}' ({getattr(entry, '__qualname__', repr(entry))}): {exc}."


def _validate_name(name: object) -> None:
    """Require a non-empty string name."""
    if not isinstance(name, str) or not name:
        raise InvalidProviderSpecError("Provider name must be a non-empty string.")


def _split_path(path: object) -> tuple[str, str]:
    """Require an absolute module path and a qualified attribute path, as ClassRegistry does."""
    if isinstance(path, str) and path.count(":") == 1:
        module_name, attribute_path = path.split(":")
        if all(part.isidentifier() for part in module_name.split(".")) and all(
            part.isidentifier() for part in attribute_path.split(".")
        ):
            return module_name, attribute_path
    raise InvalidProviderSpecError(f"Invalid provider path {path!r}; expected 'module:qualified.attribute'.")
