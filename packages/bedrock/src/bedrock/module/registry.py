"""Bedrock module registry."""

from __future__ import annotations

import threading
from typing import Any

from .entities import AppConfig
from .exc import AppRegistryNotReady, DuplicateModuleError, ModuleDependencyError, ModuleLifecycleError
from .manifest import build_app_config
from .signals import module_loaded, module_ready, module_shutdown, registry_ready, registry_shutdown


class ModuleRegistry:
    """Registry that installs and manages Bedrock modules.

    Modules are installed by import path. Dependencies declared in each
    module's ``manifest.yaml`` are resolved and installed recursively before
    the dependent module itself is installed.

    Bootstrap hooks (``on_load``, ``ready``, ``on_shutdown``) are looked up in
    each module's optional ``bootstrap.py`` and called directly by the registry.
    External code may observe lifecycle events through the module signals.

    Args:
        config: Optional host application configuration passed to hook callables.
    """

    def __init__(self, config: Any | None = None) -> None:
        self.config = config
        self._app_configs: dict[str, AppConfig] = {}
        self._install_order: list[str] = []
        self._ready = False
        self._lock = threading.RLock()
        self._loading = False

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Return whether the registry has been fully readied."""
        return self._ready

    def get(self, name: str) -> AppConfig:
        """Return an installed module by import path.

        Args:
            name: Import path of the module, e.g. ``"bedrock_app"``.

        Returns:
            The installed ``AppConfig``.

        Raises:
            AppRegistryNotReady: If the registry has not been populated yet.
            KeyError: If no module with that name is installed.
        """
        self._check_ready()
        return self._app_configs[name]

    def all(self) -> list[AppConfig]:
        """Return all installed modules in install order.

        Returns:
            Installed modules in dependency-resolved order.
        """
        self._check_ready()
        return [self._app_configs[name] for name in self._install_order]

    def is_installed(self, name: str) -> bool:
        """Return whether a module is currently installed.

        Args:
            name: Import path of the module.

        Returns:
            ``True`` if the module is installed.
        """
        return name in self._app_configs

    def list_installed(self) -> list[str]:
        """Return a list of installed module import paths in install order."""
        return list(self._install_order)

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    def install(self, import_path: str) -> AppConfig:
        """Install a module and its dependencies by import path.

        Dependencies declared in ``manifest.yaml`` under ``depends_on`` are
        installed recursively before this module. Installing an already-installed
        module is a no-op.

        This method is thread-safe.

        Args:
            import_path: Python import path of the module, e.g. ``"bedrock_app"``.

        Returns:
            The installed ``AppConfig``.

        Raises:
            DuplicateModuleError: If a dependency produces a naming conflict.
            InvalidManifestError: If a manifest is missing or invalid.
            ModuleLifecycleError: If the ``on_load`` hook raises.
        """
        with self._lock:
            return self._install(import_path, _chain=[])

    def populate(self, import_paths: list[str]) -> list[AppConfig]:
        """Install a list of modules in declared order, then mark all ready.

        Args:
            import_paths: Ordered list of import paths to install.

        Returns:
            All installed modules in dependency-resolved order.

        Raises:
            RuntimeError: If ``populate`` is called while already populating.
        """
        with self._lock:
            if self._loading:
                raise RuntimeError("populate() is not reentrant.")
            self._loading = True

        for import_path in import_paths:
            self.install(import_path)

        result = self.mark_ready()

        with self._lock:
            self._loading = False

        return result

    def mark_ready(self) -> list[AppConfig]:
        """Call each module's ``ready`` hook then emit registry-level signals.

        Returns:
            All installed modules in install order.

        Raises:
            ModuleLifecycleError: If any ``ready`` hook raises.
        """
        with self._lock:
            modules = [self._app_configs[name] for name in self._install_order]

        for app in modules:
            self._call_hook(app, "ready")
            app.lifecycle_state = "ready"
            module_ready.send(app, registry=self, module=app, config=self.config)

        with self._lock:
            self._ready = True

        registry_ready.send(self, registry=self, config=self.config)
        return modules

    def shutdown(self) -> None:
        """Call each module's ``on_shutdown`` hook in reverse install order."""
        with self._lock:
            modules = [self._app_configs[name] for name in reversed(self._install_order)]

        for app in modules:
            self._call_hook(app, "on_shutdown")
            app.lifecycle_state = "shutdown"
            module_shutdown.send(app, registry=self, module=app, config=self.config)

        registry_shutdown.send(self, registry=self, config=self.config)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _install(self, import_path: str, _chain: list[str]) -> AppConfig:
        """Recursive install implementation (must be called under lock).

        Args:
            import_path: Import path to install.
            _chain: Current dependency chain for cycle detection.

        Returns:
            The installed ``AppConfig``.

        Raises:
            ModuleDependencyError: If a dependency cycle is detected.
            DuplicateModuleError: If two different paths resolve the same name.
        """
        if import_path in self._app_configs:
            return self._app_configs[import_path]

        if import_path in _chain:
            cycle = " -> ".join(_chain + [import_path])
            raise ModuleDependencyError(f"Circular dependency detected: {cycle}")

        app = build_app_config(import_path)

        chain = _chain + [import_path]
        for dep in app.manifest.depends_on:
            self._install(dep, chain)

        if import_path in self._app_configs:
            raise DuplicateModuleError(f"Module '{import_path}' was already installed by a concurrent dependency.")

        self._app_configs[import_path] = app
        self._install_order.append(import_path)

        self._call_hook(app, "on_load")
        module_loaded.send(app, registry=self, module=app, config=self.config)

        return app

    def _call_hook(self, app: AppConfig, hook_name: str) -> None:
        """Call a bootstrap hook if present, passing registry and module.

        Args:
            app: The module whose bootstrap hook should be called.
            hook_name: One of ``"on_load"``, ``"ready"``, or ``"on_shutdown"``.

        Raises:
            ModuleLifecycleError: If the hook raises an exception.
        """
        hook = app.get_hook(hook_name)
        if hook is None:
            return
        try:
            hook(self, app)
        except Exception as exc:
            raise ModuleLifecycleError(f"Hook '{hook_name}' in module '{app.name}' raised an error: {exc}") from exc

    def _check_ready(self) -> None:
        """Raise if the registry has not been fully readied.

        Raises:
            AppRegistryNotReady: If ``mark_ready`` has not been called.
        """
        if not self._ready:
            raise AppRegistryNotReady()


apps = ModuleRegistry()

__all__ = ["ModuleRegistry", "apps"]
