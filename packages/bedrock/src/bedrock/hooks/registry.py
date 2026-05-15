"""Hook registry — global backing store for hook specs and implementations."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .caller import dispatch_acall, dispatch_call, dispatch_call_robust
from .exc import HookSpecNotFoundError


@dataclass
class HookSpec:
    """Metadata for a registered hook specification."""

    name: str
    fn: Callable[..., Any]
    firstresult: bool
    namespace: str


@dataclass
class HookImpl:
    """Metadata for a registered hook implementation."""

    fn: Callable[..., Any]
    priority: int
    module: str | None = None
    _order: int = field(default=0, repr=False)


class HookRegistry:
    """Global store for hook specifications and implementations.

    Specs and implementations are keyed by fully-qualified name
    (e.g., ``"auth.authenticate"``). Implementations are maintained
    in priority-sorted order for each spec.
    """

    def __init__(self) -> None:
        self._specs: dict[str, HookSpec] = {}
        self._impls: dict[str, list[HookImpl]] = {}
        self._namespaces: dict[str, Any] = {}
        self._counter: int = 0

    def namespace(self, name: str) -> Any:
        """Get or create a :class:`HookNamespace` for the given name.

        Args:
            name: The namespace name (e.g., ``"auth"``).

        Returns:
            A :class:`HookNamespace` scoped to *name*.
        """
        if name not in self._namespaces:
            from .namespace import HookNamespace

            self._namespaces[name] = HookNamespace(name, self)
        return self._namespaces[name]

    def register_spec(
        self,
        fqn: str,
        fn: Callable[..., Any],
        firstresult: bool,
        namespace: str,
    ) -> None:
        """Register a hook specification.

        Args:
            fqn: Fully-qualified name (e.g., ``"auth.authenticate"``).
            fn: The spec function (used for signature reference).
            firstresult: If ``True``, dispatch stops after first non-None result.
            namespace: The namespace this spec belongs to.
        """
        self._specs[fqn] = HookSpec(
            name=fqn,
            fn=fn,
            firstresult=firstresult,
            namespace=namespace,
        )

    def register_impl(
        self,
        fqn: str,
        fn: Callable[..., Any],
        priority: int,
        module: str | None,
    ) -> None:
        """Register a hook implementation in priority-sorted position.

        Lower priority values run first. Ties are broken by registration order.

        Args:
            fqn: Fully-qualified name (e.g., ``"auth.authenticate"``).
            fn: The implementation callable.
            priority: Execution priority (lower runs first).
            module: Optional bedrock module name for ordering info.
        """
        self._counter += 1
        impl = HookImpl(fn=fn, priority=priority, module=module, _order=self._counter)
        impls = self._impls.setdefault(fqn, [])
        impls.append(impl)
        impls.sort(key=lambda i: (i.priority, i._order))

    def call(self, fqn: str, /, **kwargs: Any) -> list[Any]:
        """Dispatch a hook call synchronously.

        Args:
            fqn: Fully-qualified hook name.
            **kwargs: Keyword arguments passed to each implementation.

        Returns:
            List of return values from implementations.

        Raises:
            HookSpecNotFoundError: If no spec exists for *fqn*.
        """
        spec = self._get_spec_or_raise(fqn)
        impls = self._impls.get(fqn, [])
        return dispatch_call(impls, kwargs, spec.firstresult)

    async def acall(self, fqn: str, /, **kwargs: Any) -> list[Any]:
        """Dispatch a hook call asynchronously.

        Sync implementations are wrapped via :func:`asyncio.to_thread`.

        Args:
            fqn: Fully-qualified hook name.
            **kwargs: Keyword arguments passed to each implementation.

        Returns:
            List of return values from implementations.

        Raises:
            HookSpecNotFoundError: If no spec exists for *fqn*.
        """
        spec = self._get_spec_or_raise(fqn)
        impls = self._impls.get(fqn, [])
        return await dispatch_acall(impls, kwargs, spec.firstresult)

    def call_robust(
        self,
        fqn: str,
        /,
        **kwargs: Any,
    ) -> list[tuple[Callable[..., Any], Any | Exception]]:
        """Dispatch a hook call, catching exceptions per-implementation.

        Args:
            fqn: Fully-qualified hook name.
            **kwargs: Keyword arguments passed to each implementation.

        Returns:
            List of ``(callable, result_or_exception)`` tuples.

        Raises:
            HookSpecNotFoundError: If no spec exists for *fqn*.
        """
        spec = self._get_spec_or_raise(fqn)
        impls = self._impls.get(fqn, [])
        return dispatch_call_robust(impls, kwargs, spec.firstresult)

    def has_spec(self, fqn: str) -> bool:
        """Check whether a spec is registered for *fqn*.

        Args:
            fqn: Fully-qualified hook name.

        Returns:
            ``True`` if a spec exists, ``False`` otherwise.
        """
        return fqn in self._specs

    def validate(self) -> list[str]:
        """Validate the registry and return warning messages.

        Checks:
        - Implementations registered for non-existent specs.
        - Specs with zero implementations.

        Returns:
            List of human-readable warning strings.
        """
        warnings: list[str] = []

        for fqn in self._impls:
            if fqn not in self._specs:
                warnings.append(f"Hook impl registered for non-existent spec: {fqn}")

        for fqn in self._specs:
            if fqn not in self._impls or not self._impls[fqn]:
                warnings.append(f"Hook spec has no implementations: {fqn}")

        return warnings

    def namespaces(self) -> list[str]:
        """List all namespace names that have at least one registered spec.

        Returns:
            Sorted list of namespace name strings.
        """
        return sorted({spec.namespace for spec in self._specs.values()})

    def reset(self, namespace: str | None = None) -> None:
        """Clear all specs and impls, or only those in a specific namespace.

        Args:
            namespace: If provided, only clear specs/impls in this namespace.
                If ``None``, clear everything.
        """
        if namespace is None:
            self._specs.clear()
            self._impls.clear()
            self._namespaces.clear()
            self._counter = 0
        else:
            keys_to_remove = [k for k, v in self._specs.items() if v.namespace == namespace]
            for key in keys_to_remove:
                del self._specs[key]
                self._impls.pop(key, None)
            self._namespaces.pop(namespace, None)

    def _get_spec_or_raise(self, fqn: str) -> HookSpec:
        """Return the spec for *fqn* or raise :class:`HookSpecNotFoundError`.

        Args:
            fqn: Fully-qualified hook name.

        Returns:
            The :class:`HookSpec` instance.

        Raises:
            HookSpecNotFoundError: If the spec is not registered.
        """
        spec = self._specs.get(fqn)
        if spec is None:
            raise HookSpecNotFoundError(msg=f"No hook spec registered for '{fqn}'.")
        return spec
