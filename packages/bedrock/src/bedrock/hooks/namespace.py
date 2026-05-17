"""Hook namespace — module-facing scoped API for hook specs and implementations."""

from collections.abc import Callable
from typing import Any

from .registry import HookRegistry


class HookNamespace:
    """Scoped interface for a single hook namespace.

    Modules create a ``HookNamespace("auth")`` and use it to declare
    specifications and register implementations.  All names are automatically
    prefixed with the namespace (e.g., ``"auth.authenticate"``).

    Args:
        name: The namespace name.
        registry: The backing :class:`HookRegistry` instance.
    """

    def __init__(self, name: str, registry: HookRegistry | None = None) -> None:
        self._name = name
        if registry is None:
            from . import hooks as _global_hooks

            registry = _global_hooks
        self._registry = registry

    @property
    def name(self) -> str:
        """Return the namespace name."""
        return self._name

    def spec(self, fn: Callable[..., Any] | None = None, *, firstresult: bool = False) -> Any:
        """Register a function as a hook specification.

        Can be used as a bare decorator or with arguments::

            @ns.spec
            def authenticate(self, request):
                pass


            @ns.spec(firstresult=True)
            def authenticate(self, request):
                pass

        Args:
            fn: The function to decorate. When ``None``, returns a decorator factory.
            firstresult: If ``True``, dispatch stops after the first non-None result.

        Returns:
            The decorated function, or a decorator factory when *fn* is ``None``.
        """
        if fn is not None:
            fqn = f"{self._name}.{fn.__name__}"
            self._registry.register_spec(fqn, fn, firstresult, self._name)
            return fn

        def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
            fqn_inner = f"{self._name}.{f.__name__}"
            self._registry.register_spec(fqn_inner, f, firstresult, self._name)
            return f

        return decorator

    def impl(self, fn: Callable[..., Any] | None = None, *, priority: int = 0) -> Any:
        """Register a function as a hook implementation.

        Can be used as a bare decorator or with arguments::

            @ns.impl
            def authenticate(self, request):
                pass


            @ns.impl(priority=10)
            def authenticate(self, request):
                pass

        Args:
            fn: The function to decorate. When ``None``, returns a decorator factory.
            priority: Execution priority. Lower values run first.

        Returns:
            The decorated function, or a decorator factory when *fn* is ``None``.
        """
        if fn is not None:
            fqn = f"{self._name}.{fn.__name__}"
            self._registry.register_impl(fqn, fn, priority, None)
            return fn

        def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
            fqn_inner = f"{self._name}.{f.__name__}"
            self._registry.register_impl(fqn_inner, f, priority, None)
            return f

        return decorator

    def add_specs_from(self, obj: Any) -> None:
        """Scan a class or module for ``@hookspec``-decorated methods and register each.

        Args:
            obj: A class, instance, or module whose attributes are inspected.
        """
        for attr_name in dir(obj):
            if attr_name.startswith("_"):
                continue
            method = getattr(obj, attr_name, None)
            if method is None or not callable(method):
                continue
            meta = getattr(method, "_hookspec", None)
            if meta is not None:
                fqn = f"{self._name}.{attr_name}"
                self._registry.register_spec(fqn, method, meta["firstresult"], self._name)

    def add_impls_from(self, obj: Any, *, module: str | None = None) -> None:
        """Scan a class or instance for ``@hookimpl``-decorated methods and register each.

        Args:
            obj: A class or instance whose attributes are inspected.
            module: Optional bedrock module name for ordering metadata.
        """
        for attr_name in dir(obj):
            if attr_name.startswith("_"):
                continue
            method = getattr(obj, attr_name, None)
            if method is None or not callable(method):
                continue
            meta = getattr(method, "_hookimpl", None)
            if meta is not None:
                fqn = f"{self._name}.{attr_name}"
                self._registry.register_impl(fqn, method, meta["priority"], module)

    def call(self, name: str, /, **kwargs: Any) -> list[Any]:
        """Dispatch a hook call synchronously.

        Args:
            name: Hook name within this namespace (without the namespace prefix).
            **kwargs: Keyword arguments passed to each implementation.

        Returns:
            List of return values from implementations.
        """
        fqn = f"{self._name}.{name}"
        return self._registry.call(fqn, **kwargs)

    async def acall(self, name: str, /, **kwargs: Any) -> list[Any]:
        """Dispatch a hook call asynchronously.

        Args:
            name: Hook name within this namespace (without the namespace prefix).
            **kwargs: Keyword arguments passed to each implementation.

        Returns:
            List of return values from implementations.
        """
        fqn = f"{self._name}.{name}"
        return await self._registry.acall(fqn, **kwargs)

    def call_robust(
        self,
        name: str,
        /,
        **kwargs: Any,
    ) -> list[tuple[Callable[..., Any], Any | Exception]]:
        """Dispatch a hook call, catching exceptions per-implementation.

        Args:
            name: Hook name within this namespace (without the namespace prefix).
            **kwargs: Keyword arguments passed to each implementation.

        Returns:
            List of ``(callable, result_or_exception)`` tuples.
        """
        fqn = f"{self._name}.{name}"
        return self._registry.call_robust(fqn, **kwargs)

    def has_spec(self, name: str) -> bool:
        """Check whether a spec is registered for *name* in this namespace.

        Args:
            name: Hook name without the namespace prefix.

        Returns:
            ``True`` if a spec exists, ``False`` otherwise.
        """
        fqn = f"{self._name}.{name}"
        return self._registry.has_spec(fqn)

    def get_impls(self, name: str) -> list[Callable[..., Any]]:
        """Return the list of implementation callables for a hook.

        Args:
            name: Hook name without the namespace prefix.

        Returns:
            List of callables, ordered by priority.
        """
        fqn = f"{self._name}.{name}"
        return [impl.fn for impl in self._registry._impls.get(fqn, [])]

    def specs(self) -> list[str]:
        """List spec names registered in this namespace (without the prefix).

        Returns:
            List of spec name strings.
        """
        prefix = f"{self._name}."
        return sorted(fqn[len(prefix) :] for fqn in self._registry._specs if fqn.startswith(prefix))

    def validate(self) -> list[str]:
        """Validate only the specs and implementations in this namespace."""
        return self._registry.validate(namespace=self._name)

    def reset(self) -> None:
        """Clear all specs and impls belonging to this namespace only."""
        self._registry.reset(namespace=self._name)
