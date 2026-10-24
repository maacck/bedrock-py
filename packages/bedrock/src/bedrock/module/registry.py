"""Bedrock module registry."""

from __future__ import annotations

import inspect
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .entities import AppConfig, ModuleLifecycleState
from .exc import (
    AppRegistryNotReady,
    DuplicateModuleError,
    InvalidModuleCallableError,
    ModuleDependencyError,
    ModuleFailure,
    ModuleLifecycleError,
    ModuleStartupRollbackError,
)
from .manifest import build_app_config
from .signals import module_loaded, module_ready, module_shutdown, registry_ready, registry_shutdown


class _InstallAttempt:
    """Records the first failure of one installation operation and the module step it came from."""

    def __init__(self) -> None:
        self.failure: ModuleFailure | None = None

    @contextmanager
    def guard(self, module: str, phase: str) -> Iterator[None]:
        """Attribute an exception raised inside the block to ``module`` and ``phase`` if none was recorded yet."""
        try:
            yield
        except Exception as exc:
            if self.failure is None:
                self.failure = ModuleFailure(module=module, phase=phase, error=exc)
            raise


class ModuleRegistry:
    """Registry that installs and manages Bedrock modules.

    Modules are installed by import path. Dependencies declared in each
    module's ``manifest.yaml`` are resolved and installed recursively before
    the dependent module itself is installed.

    Bootstrap hooks (``on_load``, ``ready``, ``on_shutdown``) are looked up in
    each module's optional ``bootstrap.py`` and called directly by the registry.
    External code may observe lifecycle events through the module signals.

    Lifecycle guarantees:

    * Installation is failure-atomic. If installing a module (or any dependency
      it pulls in) fails, every module introduced by that operation is removed
      from the registry. Modules that were installed before the operation are
      never touched. Introduced modules that had already completed ``on_load``
      are unwound best-effort by calling their ``on_shutdown`` hook in reverse
      install order.
    * Each module moves forward through ``loading -> loaded -> ready -> shutdown``
      (see ``ModuleLifecycleState``). ``mark_ready`` skips modules that are
      already ready, so it can be retried after a failed ``ready`` hook.
    * ``shutdown`` attempts every module that is not yet shut down in reverse
      install order even when some hooks fail, then raises a single
      ``ModuleLifecycleError`` listing all failures. A module whose shutdown
      completed is never shut down again.
    * ``mark_ready`` and ``shutdown`` are not re-entrant. While one of them is
      running, a call to either from a hook or a signal receiver raises
      ``ModuleLifecycleError`` instead of re-running hooks. The guard is plain
      registry state rather than a thread lock, so it also protects against
      interleaving on a single thread.

    Args:
        config: Optional host application configuration passed to hook callables.
    """

    def __init__(self, config: Any | None = None) -> None:
        self.config = config
        self._app_configs: dict[str, AppConfig] = {}
        self._install_order: list[str] = []
        self._ready = False
        self._shut_down = False
        self._operation: str | None = None
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

        The operation is failure-atomic: if it fails, every module it introduced
        is removed again and modules installed earlier are left untouched.

        This method is thread-safe.

        Args:
            import_path: Python import path of the module, e.g. ``"bedrock_app"``.

        Returns:
            The installed ``AppConfig``.

        Raises:
            DuplicateModuleError: If a dependency produces a naming conflict.
            InvalidManifestError: If a manifest is missing or invalid.
            ModuleLifecycleError: If the ``on_load`` hook raises.
            ModuleStartupRollbackError: If installation failed and unwinding modules
                that had already loaded also failed. Its ``failures`` hold the original
                failure first, followed by the cleanup failures.
        """
        with self._lock:
            return self._install_atomically([import_path])[0]

    def populate(self, import_paths: list[str]) -> list[AppConfig]:
        """Install a list of modules in declared order, then mark all ready.

        The installation phase is failure-atomic as a whole: if any import path
        fails to install, modules introduced by this call are rolled back,
        including those of earlier import paths in the list. If a ``ready`` hook
        fails afterwards, installed modules are kept so that ``mark_ready`` (or
        ``populate`` again with the same paths) can resume without re-running
        ready hooks that already succeeded.

        The registry lock is held for the whole call, so other threads wait
        until it finishes.

        Args:
            import_paths: Ordered list of import paths to install.

        Returns:
            All installed modules in dependency-resolved order.

        Raises:
            ModuleLifecycleError: If ``populate`` is called while already populating,
                or if a hook fails.
            ModuleStartupRollbackError: If installation failed and cleanup also failed.
        """
        with self._lock:
            if self._loading:
                raise ModuleLifecycleError("populate() is not reentrant.")
            self._loading = True

            try:
                self._install_atomically(import_paths)
                return self.mark_ready()
            finally:
                self._loading = False

    def mark_ready(self) -> list[AppConfig]:
        """Call each loaded module's ``ready`` hook then emit registry-level signals.

        Readiness is tracked per module. Modules that are already ready are
        skipped, so calling this again after a failure only runs the hooks that
        have not succeeded yet. Processing stops at the first failing hook;
        ``registry_ready`` is only emitted once every module is ready.

        Returns:
            All installed modules in install order.

        Raises:
            ModuleLifecycleError: If any ``ready`` hook raises (``failures`` names the
                module and holds the hook's exception), if a module is still loading or
                has already been shut down, or if called from a hook or signal receiver
                while ``mark_ready`` or ``shutdown`` is already running.
        """
        with self._lock, self._lifecycle_operation("mark_ready"):
            modules = [self._app_configs[name] for name in self._install_order]
            blocked = [
                app.name
                for app in modules
                if app.lifecycle_state in (ModuleLifecycleState.LOADING, ModuleLifecycleState.SHUTDOWN)
            ]
            if blocked:
                raise ModuleLifecycleError(
                    f"Cannot mark the registry ready: modules still loading or already shut down: {', '.join(blocked)}."
                )

            newly_ready = 0
            for app in modules:
                if app.lifecycle_state != ModuleLifecycleState.LOADED:
                    continue
                try:
                    self._call_hook(app, "ready")
                except ModuleLifecycleError as exc:
                    error = self._aggregate_error(
                        "Readiness failed", [ModuleFailure(module=app.name, phase="ready", error=exc)]
                    )
                    raise error from error.__cause__
                app.transition_to(ModuleLifecycleState.READY)
                newly_ready += 1
                module_ready.send(app, registry=self, module=app, config=self.config)

            already_announced = self._ready
            self._ready = True
            if already_announced and not newly_ready:
                return modules

            self._validate_hooks()
            registry_ready.send(self, registry=self, config=self.config)
            return modules

    def shutdown(self) -> None:
        """Call each module's ``on_shutdown`` hook in reverse install order.

        Every module that has not completed shutdown is attempted, even after
        earlier failures. A module whose shutdown completed is never shut down
        again, so repeated calls are harmless; modules whose hook failed are
        attempted again on the next call. ``registry_shutdown`` is emitted once,
        after every module has shut down.

        Raises:
            ModuleLifecycleError: After all attempts, if any hook or ``module_shutdown``
                receiver failed. ``failures`` lists every failure with its original cause.
                Also raised if called from a hook or signal receiver while ``mark_ready``
                or ``shutdown`` is already running.
        """
        with self._lock, self._lifecycle_operation("shutdown"):
            modules = [self._app_configs[name] for name in reversed(self._install_order)]
            failures: list[ModuleFailure] = []

            for app in modules:
                if app.lifecycle_state == ModuleLifecycleState.SHUTDOWN:
                    continue
                failures.extend(self._shutdown_module(app))

            if failures:
                raise self._aggregate_error(f"Shutdown failed for {len(failures)} step(s)", failures)

            if self._shut_down:
                return
            self._shut_down = True
            registry_shutdown.send(self, registry=self, config=self.config)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _install(self, import_path: str, _chain: list[str], attempt: _InstallAttempt) -> AppConfig:
        """Recursive install implementation (must be called under lock).

        Args:
            import_path: Import path to install.
            _chain: Current dependency chain for cycle detection.
            attempt: Collects where the first failure of the surrounding operation happened.

        Returns:
            The installed ``AppConfig``.

        Raises:
            ModuleDependencyError: If a dependency cycle is detected.
            DuplicateModuleError: If two different paths resolve the same name.
        """
        if import_path in self._app_configs:
            return self._app_configs[import_path]

        with attempt.guard(import_path, "dependency"):
            if import_path in _chain:
                cycle = " -> ".join(_chain + [import_path])
                raise ModuleDependencyError(f"Circular dependency detected: {cycle}")

        with attempt.guard(import_path, "manifest"):
            app = build_app_config(import_path)

        chain = _chain + [import_path]
        for dep in app.manifest.depends_on:
            self._install(dep, chain, attempt)

        with attempt.guard(import_path, "install"):
            if import_path in self._app_configs:
                raise DuplicateModuleError(f"Module '{import_path}' was already installed by a concurrent dependency.")

        self._app_configs[import_path] = app
        self._install_order.append(import_path)

        with attempt.guard(import_path, "on_load"):
            self._call_hook(app, "on_load")
        app.transition_to(ModuleLifecycleState.LOADED)
        with attempt.guard(import_path, "module_loaded"):
            module_loaded.send(app, registry=self, module=app, config=self.config)

        return app

    def _install_atomically(self, import_paths: list[str]) -> list[AppConfig]:
        """Install import paths as one failure-atomic operation (must be called under lock).

        Args:
            import_paths: Import paths to install in order.

        Returns:
            The installed ``AppConfig`` for each import path.

        Raises:
            ModuleLifecycleError: The original failure when rollback was clean.
            ModuleStartupRollbackError: If rollback itself failed for some modules.
        """
        preexisting = set(self._app_configs)
        attempt = _InstallAttempt()
        try:
            return [self._install(import_path, [], attempt) for import_path in import_paths]
        except BaseException as exc:
            introduced = [name for name in self._install_order if name not in preexisting]
            unwind_failures = self._rollback(introduced)
            if not unwind_failures or not isinstance(exc, Exception):
                raise

            original = attempt.failure or ModuleFailure(module=import_paths[0], phase="install", error=exc)
            error = self._aggregate_error(
                f"Installation of {', '.join(import_paths)} failed ({exc}) and cleanup was incomplete",
                [original, *unwind_failures],
                error_type=ModuleStartupRollbackError,
            )
            raise error from error.__cause__

    def _rollback(self, introduced: list[str]) -> list[ModuleFailure]:
        """Remove modules introduced by a failed installation (must be called under lock).

        Modules that completed ``on_load`` are unwound best-effort with their
        ``on_shutdown`` hook in reverse install order. A module that never
        finished loading, or that was already shut down, has nothing to release. Every introduced module is
        removed from membership and install order, whatever the unwind outcome.

        Args:
            introduced: Names of modules added by the failed operation, in install order.

        Returns:
            Failures that occurred while unwinding; empty when cleanup was clean.
        """
        failures: list[ModuleFailure] = []
        try:
            for name in reversed(introduced):
                app = self._app_configs[name]
                if app.lifecycle_state in (ModuleLifecycleState.LOADING, ModuleLifecycleState.SHUTDOWN):
                    continue
                failures.extend(self._shutdown_module(app))
        finally:
            removed = set(introduced)
            for name in introduced:
                self._app_configs.pop(name, None)
            self._install_order[:] = [name for name in self._install_order if name not in removed]
        return failures

    def _shutdown_module(self, app: AppConfig) -> list[ModuleFailure]:
        """Shut down one module and report failures instead of raising.

        The module is marked ``SHUTDOWN`` only after its ``on_shutdown`` hook
        succeeded, so a failed hook is retried by a later ``shutdown`` call.
        ``module_shutdown`` is emitted once the hook completed; a failing
        receiver is reported but does not undo the shutdown.

        Args:
            app: Module to shut down.

        Returns:
            Failures recorded for this module; empty on success.
        """
        try:
            self._call_hook(app, "on_shutdown")
        except Exception as exc:
            return [ModuleFailure(module=app.name, phase="on_shutdown", error=exc)]

        app.transition_to(ModuleLifecycleState.SHUTDOWN)
        try:
            module_shutdown.send(app, registry=self, module=app, config=self.config)
        except Exception as exc:
            wrapped = ModuleLifecycleError(
                f"Receiver of 'module_shutdown' for module '{app.name}' raised an error: {exc}"
            )
            wrapped.__cause__ = exc
            return [ModuleFailure(module=app.name, phase="module_shutdown", error=wrapped)]
        return []

    @staticmethod
    def _aggregate_error(
        summary: str,
        failures: list[ModuleFailure],
        error_type: type[ModuleLifecycleError] = ModuleLifecycleError,
    ) -> ModuleLifecycleError:
        """Build one lifecycle error exposing every failure and its original cause.

        Args:
            summary: Human-readable lead sentence.
            failures: Failures to aggregate, in the order they occurred.
            error_type: Concrete error class to build.

        Returns:
            The aggregate error. It always has ``failures`` set, and its ``__cause__``
            is always an ``ExceptionGroup`` holding the error of every failure, in order.
            Each hook failure error chains to the exception the module's own code raised.
        """
        details = "; ".join(f"{failure.error}" for failure in failures)
        error = error_type(f"{summary}: {details}", failures=tuple(failures))
        error.__cause__ = ExceptionGroup(
            summary, [failure.error for failure in failures if isinstance(failure.error, Exception)]
        )
        return error

    @contextmanager
    def _lifecycle_operation(self, name: str) -> Iterator[None]:
        """Mark ``mark_ready`` or ``shutdown`` as running and reject nested calls (call under lock).

        The guard is plain registry state: it is not a lock, so hooks and signal
        receivers that call back into the registry fail fast instead of re-running
        hooks, and an async implementation can reuse it between coroutines.

        Args:
            name: Public method starting the operation.

        Yields:
            Nothing; the guard is released on exit, including on failure.

        Raises:
            ModuleLifecycleError: If ``mark_ready`` or ``shutdown`` is already running.
        """
        if self._operation is not None:
            raise ModuleLifecycleError(
                f"Cannot call {name}() while {self._operation}() is running; "
                "lifecycle hooks and signal receivers must not call back into it."
            )
        self._operation = name
        try:
            yield
        finally:
            self._operation = None

    def _call_hook(self, app: AppConfig, hook_name: str) -> None:
        """Call a bootstrap hook if present using keyword injection only.

        Hooks must accept keyword invocation using the injected names
        ``registry``, ``app``, ``container``, and ``hooks``. Hooks may declare
        any supported subset or ``**kwargs`` to receive all injected values.
        Legacy positional-only contracts are rejected with a clear error.

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
            hook(**self._get_hook_kwargs(app=app, hook_name=hook_name, hook=hook))
        except Exception as exc:
            raise ModuleLifecycleError(f"Hook '{hook_name}' in module '{app.name}' raised an error: {exc}") from exc

    def _get_hook_kwargs(self, app: AppConfig, hook_name: str, hook: Any) -> dict[str, Any]:
        """Return injected kwargs for a bootstrap hook or raise on invalid signatures.

        Args:
            app: Module app config associated with the hook.
            hook_name: Lifecycle hook name.
            hook: Hook callable.

        Returns:
            Keyword arguments that should be passed to the hook.

        Raises:
            TypeError: If the hook cannot be invoked via the supported keyword contract.
        """
        from ..di import container
        from ..hooks import hooks

        provided_kwargs = {
            "registry": self,
            "app": app,
            "container": container,
            "hooks": hooks,
        }

        parameters = tuple(inspect.signature(hook).parameters.values())
        accepts_var_keyword = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)

        kwargs: dict[str, Any] = {}
        missing_required: list[str] = []
        positional_only: list[str] = []

        for parameter in parameters:
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                continue

            if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                continue

            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                positional_only.append(parameter.name)
                if parameter.default is inspect.Parameter.empty:
                    missing_required.append(parameter.name)
                continue

            if parameter.name in provided_kwargs:
                kwargs[parameter.name] = provided_kwargs[parameter.name]
                continue

            if parameter.default is inspect.Parameter.empty:
                missing_required.append(parameter.name)

        if missing_required:
            raise InvalidModuleCallableError(
                self._format_invalid_hook_signature_error(
                    app=app, hook_name=hook_name, missing_required=missing_required, positional_only=positional_only
                )
            )

        if accepts_var_keyword:
            return provided_kwargs

        if kwargs:
            return kwargs

        raise InvalidModuleCallableError(
            self._format_invalid_hook_signature_error(
                app=app, hook_name=hook_name, missing_required=[], positional_only=positional_only
            )
        )

    @staticmethod
    def _format_invalid_hook_signature_error(
        app: AppConfig,
        hook_name: str,
        missing_required: list[str],
        positional_only: list[str],
    ) -> str:
        """Return a human-readable bootstrap hook signature error message."""
        details = [
            f"Bootstrap hook '{hook_name}' in module '{app.name}' must accept keyword invocation using the injected names "
            "'registry', 'app', 'container', and 'hooks'."
        ]

        if missing_required:
            details.append(f"Unsupported required parameters: {', '.join(missing_required)}.")

        if positional_only:
            details.append(f"Positional-only parameters cannot be injected by keyword: {', '.join(positional_only)}.")

        if not missing_required and not positional_only:
            details.append("The hook signature does not declare any supported injected keyword parameters.")

        return " ".join(details)

    def _check_ready(self) -> None:
        """Raise if the registry has not been fully readied.

        Raises:
            AppRegistryNotReady: If ``mark_ready`` has not been called.
        """
        if not self._ready:
            raise AppRegistryNotReady()

    @staticmethod
    def _validate_hooks() -> None:
        """Run hook validation and log warnings for orphaned impls or empty specs."""
        try:
            from ..hooks import hooks as _hooks

            warnings = _hooks.validate()
            if warnings:
                from ..logging import get_logger

                logger = get_logger(__name__)
                for warning in warnings:
                    logger.warning(f"Hook validation: {warning}")
        except ImportError:
            pass


apps = ModuleRegistry()

__all__ = ["ModuleRegistry", "apps"]
