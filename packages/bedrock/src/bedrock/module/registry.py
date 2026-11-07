"""Bedrock module registry."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from ._runtime import (
    CallEffect,
    Effect,
    HookEffect,
    LoopAdapter,
    OperationGate,
    SignalEffect,
    Steps,
    discard_awaitable,
    loop_is_running,
)
from .entities import AppConfig, ModuleLifecycleState
from .exc import (
    AppRegistryNotReady,
    DuplicateModuleError,
    InvalidModuleCallableError,
    ModuleAsyncRequiredError,
    ModuleDependencyError,
    ModuleFailure,
    ModuleLifecycleError,
    ModuleStartupRollbackError,
)
from .manifest import build_app_config
from .signals import module_loaded, module_ready, module_shutdown, registry_ready, registry_shutdown

Finalizer = tuple[str, str, Effect]
"""A cleanup step as ``(owner, phase, effect)``, run after every module hook; see ``ModuleRegistry._shutdown_with``."""

_ASYNC_ENTRYPOINT_HINT = {
    "install": "await registry.ainstall(...)",
    "populate": "await registry.apopulate(...) (or await bedrock.asetup())",
    "mark_ready": "await registry.amark_ready()",
    "shutdown": "await registry.ashutdown() (or await bedrock.ashutdown())",
}


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

    Every lifecycle operation has a synchronous entrypoint (``install``,
    ``populate``, ``mark_ready``, ``shutdown``) and an asynchronous one with the
    ``a`` prefix (``ainstall``, ``apopulate``, ``amark_ready``, ``ashutdown``).
    Both run the same implementation, so the guarantees below hold for either.

    Hook and receiver adaptation:

    * A hook may be a plain function or a coroutine function. The registry calls
      it and awaits whatever it returns when that is awaitable, so a plain function
      may also return an awaitable only when it has async work to do.
    * Async entrypoints await async hooks and receivers on the running loop and
      run synchronous hooks and receivers in a worker thread (``asyncio.to_thread``),
      like ``hooks.acall`` and ``Signal.asend``.
    * Sync entrypoints call synchronous hooks directly and adapt async work on a
      persistent private event loop, but only when the calling thread has no running
      loop. The loop stays open until every module has shut down, so an async client
      opened by one hook is closed by another on the same loop. Inside a running loop
      they raise ``ModuleAsyncRequiredError`` naming the async entrypoint, without
      nesting loops or leaving a coroutine unawaited. Registries whose hooks are all
      synchronous keep working from sync entrypoints inside a running loop.
    * Start up and shut down a registry through the same entrypoint family. Async
      clients are bound to the loop that opened them.

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
      ``ready`` reports that readiness was announced; a module installed later
      stays ``loaded`` until ``mark_ready`` runs again, so read each module's
      ``lifecycle_state`` rather than treating ``ready`` as "every module is ready".
    * ``shutdown`` attempts every module that is not yet shut down in reverse
      install order even when some hooks fail, then raises a single
      ``ModuleLifecycleError`` listing all failures. A module whose shutdown
      completed is never shut down again.
    * ``install``, ``populate``, ``mark_ready`` and ``shutdown`` each run as one
      registry operation, and the only nesting allowed is ``install`` from an
      ``on_load`` hook or a ``module_loaded`` receiver (including during its
      rollback, which rejects new modules). Any other call from a hook or a
      signal receiver raises ``ModuleLifecycleError`` before it changes anything:
      ``shutdown`` or ``mark_ready`` while a module is installing, ``mark_ready``
      or ``shutdown`` inside each other, and ``install`` of a new module while
      ``mark_ready`` or ``shutdown`` runs. A hook that must abort an installation
      raises an error, which rolls the installation back.
    * Operations are serialized without holding a thread lock across an ``await``.
      A call from inside the running operation is detected through the context it
      runs in, which includes child tasks and ``to_thread`` workers started from a
      hook, and is rejected as above. An unrelated thread or task waits for the
      running operation to finish. A synchronous call from another task on the
      thread that owns the running operation raises instead of deadlocking.
    * Cancelling an async operation leaves every module in a valid state and
      releases the operation. An interrupted installation still rolls back; an
      interrupted ``amark_ready`` or ``ashutdown`` resumes from the modules that
      have not finished when called again.

    Args:
        config: Optional host application configuration passed to hook callables.
    """

    def __init__(self, config: Any | None = None) -> None:
        self.config = config
        self._app_configs: dict[str, AppConfig] = {}
        self._install_order: list[str] = []
        self._ready = False
        self._shut_down = False
        self._gate = OperationGate()
        self._adapter = LoopAdapter()
        self._loading = False
        self._rolling_back = False

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Return whether readiness has been announced for the registry.

        This stays ``True`` when a module is installed after readiness; that module remains
        ``LOADED`` until ``mark_ready()`` runs again. It does not mean every installed module
        is ready, so check each module's ``lifecycle_state`` when that matters.
        """
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
        Rollback callbacks cannot introduce new modules through ``install()`` or
        ``populate()``; attempts raise ``ModuleLifecycleError`` before loading them.
        The same applies to hooks and signal receivers running inside ``mark_ready()``
        or ``shutdown()``: install modules before readiness starts, or call
        ``install()`` and then ``mark_ready()`` again afterwards. Installing a module
        that is already installed is always a no-op. ``on_load`` hooks and
        ``module_loaded`` receivers may install further modules, but cannot call
        ``mark_ready()`` or ``shutdown()``; raise an error to abort the installation.

        This method is thread-safe. Asynchronous hooks are adapted as described on the
        class; use :meth:`ainstall` inside a running event loop.

        Args:
            import_path: Python import path of the module, e.g. ``"bedrock_app"``.

        Returns:
            The installed ``AppConfig``.

        Raises:
            DuplicateModuleError: If a dependency produces a naming conflict.
            InvalidManifestError: If a manifest is missing or invalid.
            ModuleLifecycleError: If the ``on_load`` hook raises, or a new module is
                requested while installation rollback, ``mark_ready()`` or ``shutdown()``
                is running.
            ModuleAsyncRequiredError: If a hook or receiver needs an event loop but one is
                already running in this thread.
            ModuleStartupRollbackError: If installation failed and unwinding modules
                that had already loaded also failed. Its ``failures`` hold the original
                failure first, followed by the cleanup failures.
        """
        paths = [import_path]
        return self._run_sync("install", lambda: self._install_atomically(paths), self._introduces(paths))[0]

    async def ainstall(self, import_path: str) -> AppConfig:
        """Install a module and its dependencies by import path, awaiting asynchronous hooks.

        Same contract as :meth:`install`. Asynchronous ``on_load`` hooks and ``module_loaded``
        receivers are awaited in dependency order; synchronous ones run in a worker thread.

        Args:
            import_path: Python import path of the module, e.g. ``"bedrock_app"``.

        Returns:
            The installed ``AppConfig``.

        Raises:
            DuplicateModuleError: If a dependency produces a naming conflict.
            InvalidManifestError: If a manifest is missing or invalid.
            ModuleLifecycleError: As for :meth:`install`.
            ModuleStartupRollbackError: As for :meth:`install`.
        """
        paths = [import_path]
        return (await self._run_async("install", lambda: self._install_atomically(paths), self._introduces(paths)))[0]

    def populate(self, import_paths: list[str]) -> list[AppConfig]:
        """Install a list of modules in declared order, then mark all ready.

        The installation phase is failure-atomic as a whole: if any import path
        fails to install, modules introduced by this call are rolled back,
        including those of earlier import paths in the list. If a ``ready`` hook
        fails afterwards, installed modules are kept so that ``mark_ready`` (or
        ``populate`` again with the same paths) can resume without re-running
        ready hooks that already succeeded.

        The whole call is one registry operation, so other threads and tasks wait
        until it finishes.

        Args:
            import_paths: Ordered list of import paths to install.

        Returns:
            All installed modules in dependency-resolved order.

        Raises:
            ModuleLifecycleError: If ``populate`` is called while already populating,
                or if a hook fails.
            ModuleAsyncRequiredError: If a hook or receiver needs an event loop but one is
                already running in this thread.
            ModuleStartupRollbackError: If installation failed and cleanup also failed.
        """
        return self._run_sync("populate", lambda: self._populate(import_paths))

    async def apopulate(self, import_paths: list[str]) -> list[AppConfig]:
        """Install a list of modules, then mark all ready, awaiting asynchronous hooks.

        Same contract as :meth:`populate`.

        Args:
            import_paths: Ordered list of import paths to install.

        Returns:
            All installed modules in dependency-resolved order.

        Raises:
            ModuleLifecycleError: As for :meth:`populate`.
            ModuleStartupRollbackError: As for :meth:`populate`.
        """
        return await self._run_async("populate", lambda: self._populate(import_paths))

    def mark_ready(self) -> list[AppConfig]:
        """Call each loaded module's ``ready`` hook then emit registry-level signals.

        Readiness is tracked per module. Modules that are already ready are
        skipped, so calling this again after a failure only runs the hooks that
        have not succeeded yet. Processing stops at the first failing hook;
        ``registry_ready`` is only emitted once every module is ready.
        If its notification fails, registry readiness is reset so startup can be
        retried without repeating completed module hooks. Receivers may query the
        ready registry during notification and must tolerate notification retries.

        Returns:
            All installed modules in install order.

        Raises:
            ModuleLifecycleError: If any ``ready`` hook raises (``failures`` names the
                module and holds the hook's exception), if a module is still loading or
                has already been shut down, or if called from a hook or signal receiver
                while ``install``, ``mark_ready`` or ``shutdown`` is already running.
            ModuleAsyncRequiredError: If a ``ready`` hook is asynchronous and an event loop is
                already running in this thread. Raised before any hook runs.
        """
        return self._run_sync("mark_ready", self._mark_ready)

    async def amark_ready(self) -> list[AppConfig]:
        """Call each loaded module's ``ready`` hook, awaiting asynchronous hooks.

        Same contract as :meth:`mark_ready`.

        Returns:
            All installed modules in install order.

        Raises:
            ModuleLifecycleError: As for :meth:`mark_ready`.
        """
        return await self._run_async("mark_ready", self._mark_ready)

    def shutdown(self) -> None:
        """Call each module's ``on_shutdown`` hook in reverse install order.

        Every module that has not completed shutdown is attempted, even after
        earlier failures. A module whose shutdown completed is never shut down
        again, so repeated calls are harmless; modules whose hook failed are
        attempted again on the next call. ``registry_shutdown`` is emitted after
        every module has shut down. Failed notifications are retried on the next
        call without repeating completed hooks; receivers must be retry-safe.

        Raises:
            ModuleLifecycleError: After all attempts, if any hook or ``module_shutdown``
                receiver failed. ``failures`` lists every failure with its original cause.
                Also raised if called from a hook or signal receiver while ``install``,
                ``mark_ready`` or ``shutdown`` is already running.
            ModuleAsyncRequiredError: If an ``on_shutdown`` hook is asynchronous and an event
                loop is already running in this thread. Raised before any hook runs.
        """
        self._run_sync("shutdown", lambda: self._shutdown_with(None))

    async def ashutdown(self) -> None:
        """Call each module's ``on_shutdown`` hook in reverse install order, awaiting asynchronous hooks.

        Same contract as :meth:`shutdown`. The coroutine completes only after every awaited
        hook has finished; nothing started by the registry keeps running afterwards.

        Raises:
            ModuleLifecycleError: As for :meth:`shutdown`.
        """
        await self._run_async("shutdown", lambda: self._shutdown_with(None))

    def _shutdown_with_cleanups(self, finalizers: Callable[[], list[Finalizer]]) -> None:
        """Sync shutdown followed by process-level cleanup steps; used by ``bedrock.lifecycle.shutdown``."""
        self._run_sync("shutdown", lambda: self._shutdown_with(finalizers))

    async def _ashutdown_with_cleanups(self, finalizers: Callable[[], list[Finalizer]]) -> None:
        """Async shutdown followed by process-level cleanup steps; used by ``bedrock.lifecycle.ashutdown``."""
        await self._run_async("shutdown", lambda: self._shutdown_with(finalizers))

    # ------------------------------------------------------------------
    # Operation plumbing (shared by the sync and async entrypoints)
    # ------------------------------------------------------------------

    def _introduces(self, import_paths: Sequence[str]) -> Callable[[], bool]:
        """Return a check telling whether installing ``import_paths`` would add a module."""
        return lambda: any(path not in self._app_configs for path in import_paths)

    def _run_sync(
        self,
        name: str,
        steps: Callable[[], Steps[Any]],
        introduces: Callable[[], bool] | None = None,
    ) -> Any:
        """Run one operation from a synchronous entrypoint."""
        lease = self._gate.enter(name, introduces)
        try:
            if lease.outermost:
                self._preflight_async_hooks(name)
            return self._drive_sync(steps())
        finally:
            try:
                if lease.outermost:
                    self._close_adapter_if_idle()
            finally:
                self._gate.leave(lease)

    async def _run_async(
        self,
        name: str,
        steps: Callable[[], Steps[Any]],
        introduces: Callable[[], bool] | None = None,
    ) -> Any:
        """Run one operation from an asynchronous entrypoint."""
        lease = await self._gate.aenter(name, introduces)
        try:
            return await self._drive_async(steps())
        finally:
            try:
                if lease.outermost and self._adapter.is_open and self._is_idle():
                    await asyncio.to_thread(self._adapter.close)
            finally:
                self._gate.leave(lease)

    def _drive_sync(self, steps: Steps[Any]) -> Any:
        """Execute the effects of ``steps`` synchronously and return its result."""
        try:
            effect = next(steps)
            while True:
                try:
                    value = self._perform_sync(effect)
                except BaseException as exc:
                    effect = steps.throw(exc)
                else:
                    effect = steps.send(value)
        except StopIteration as stop:
            return stop.value

    async def _drive_async(self, steps: Steps[Any]) -> Any:
        """Execute the effects of ``steps`` on the running loop and return its result."""
        try:
            effect = next(steps)
            while True:
                try:
                    value = await self._perform_async(effect)
                except BaseException as exc:
                    effect = steps.throw(exc)
                else:
                    effect = steps.send(value)
        except StopIteration as stop:
            return stop.value

    def _perform_sync(self, effect: Effect) -> Any:
        if isinstance(effect, HookEffect):
            return self._call_hook_sync(effect.app, effect.hook_name)
        if isinstance(effect, SignalEffect):
            return effect.signal.send(effect.sender, _async_wrapper=self._adapt_receiver_sync, **effect.kwargs)
        if isinstance(effect, CallEffect):
            return effect.fn()
        if loop_is_running():
            raise self._async_required("An asynchronous cleanup step")
        return self._adapter.run(effect.factory())

    async def _perform_async(self, effect: Effect) -> Any:
        if isinstance(effect, HookEffect):
            return await self._call_hook_async(effect.app, effect.hook_name)
        if isinstance(effect, SignalEffect):
            return await effect.signal.asend(effect.sender, **effect.kwargs)
        if isinstance(effect, CallEffect):
            return effect.fn()
        return await effect.factory()

    def _preflight_async_hooks(self, name: str) -> None:
        """Refuse to start ``mark_ready`` or ``shutdown`` in a running loop when a coroutine hook would be needed.

        Raising before the first hook keeps the registry untouched. Installation cannot be checked ahead because
        dependencies are discovered while installing; there the first awaitable hook raises, and the usual
        rollback applies.
        """
        if not loop_is_running():
            return
        if name == "mark_ready":
            hook_name, targets = (
                "ready",
                [app for app in self._modules() if app.lifecycle_state is ModuleLifecycleState.LOADED],
            )
        elif name == "shutdown":
            hook_name = "on_shutdown"
            targets = [
                app for app in reversed(self._modules()) if app.lifecycle_state is not ModuleLifecycleState.SHUTDOWN
            ]
        else:
            return
        offenders = [app.name for app in targets if inspect.iscoroutinefunction(app.get_hook(hook_name))]
        if offenders:
            raise self._async_required(f"The '{hook_name}' hook of module(s) {', '.join(offenders)}", name)

    def _async_required(self, subject: str, entrypoint: str | None = None) -> ModuleAsyncRequiredError:
        """Build the error for async work requested from a sync entrypoint inside a running loop."""
        entrypoint = entrypoint or self._gate.entrypoint() or "install"
        hint = _ASYNC_ENTRYPOINT_HINT.get(entrypoint, "the matching async entrypoint")
        return ModuleAsyncRequiredError(
            f"{subject} is asynchronous and cannot run from {entrypoint}() inside a running event loop. "
            f"Use {hint} instead."
        )

    def _modules(self) -> list[AppConfig]:
        return [self._app_configs[name] for name in self._install_order]

    def _is_idle(self) -> bool:
        return all(app.lifecycle_state is ModuleLifecycleState.SHUTDOWN for app in self._app_configs.values())

    def _close_adapter_if_idle(self) -> None:
        """Close the sync adapter loop once no module that could own a client on it is still alive."""
        if self._adapter.is_open and self._is_idle():
            self._adapter.close()

    # ------------------------------------------------------------------
    # Operations (written once; executed by the sync or async driver)
    # ------------------------------------------------------------------

    def _populate(self, import_paths: list[str]) -> Steps[list[AppConfig]]:
        if self._loading:
            raise ModuleLifecycleError("populate() is not reentrant.")
        self._loading = True
        try:
            with self._gate.phase("install"):
                yield from self._install_atomically(import_paths)
            with self._gate.phase("mark_ready"):
                return (yield from self._mark_ready())
        finally:
            self._loading = False

    def _mark_ready(self) -> Steps[list[AppConfig]]:
        modules = self._modules()
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
                yield HookEffect(app, "ready")
            except ModuleLifecycleError as exc:
                error = self._aggregate_error(
                    "Readiness failed", [ModuleFailure(module=app.name, phase="ready", error=exc)]
                )
                raise error from error.__cause__
            app.transition_to(ModuleLifecycleState.READY)
            newly_ready += 1
            yield SignalEffect(module_ready, app, {"registry": self, "module": app, "config": self.config})

        already_announced = self._ready
        self._ready = True
        if already_announced and not newly_ready:
            return modules

        try:
            self._validate_hooks()
            yield SignalEffect(registry_ready, self, {"registry": self, "config": self.config})
        except BaseException:
            self._ready = False
            raise
        return modules

    def _shutdown_with(self, finalizers: Callable[[], list[Finalizer]] | None) -> Steps[None]:
        """Shut down every module, then run ``finalizers`` whether or not module cleanup succeeded.

        ``finalizers`` is how process-level infrastructure, such as the database, is released after every
        module hook, ``module_shutdown`` receiver and ``registry_shutdown`` receiver has run. Each finalizer
        step is attempted even when an earlier step or a module failed. A cancellation skips them: the
        interrupted shutdown is resumed by calling it again.
        """
        error: Exception | None = None
        try:
            yield from self._shutdown_modules()
        except Exception as exc:
            error = exc

        step_failures: list[ModuleFailure] = []
        if finalizers is not None:
            for owner, phase, effect in finalizers():
                try:
                    yield effect
                except Exception as exc:
                    step_failures.append(ModuleFailure(module=owner, phase=phase, error=exc))

        if step_failures:
            if isinstance(error, ModuleLifecycleError) and error.failures:
                earlier = list(error.failures)
            elif error is not None:
                earlier = [ModuleFailure(module="bedrock", phase="registry_shutdown", error=error)]
            else:
                earlier = []
            failures = [*earlier, *step_failures]
            raise self._aggregate_error(f"Shutdown failed for {len(failures)} step(s)", failures)
        if error is not None:
            raise error

    def _shutdown_modules(self) -> Steps[None]:
        modules = [self._app_configs[name] for name in reversed(self._install_order)]
        failures: list[ModuleFailure] = []

        for app in modules:
            if app.lifecycle_state == ModuleLifecycleState.SHUTDOWN:
                continue
            failures.extend((yield from self._shutdown_module(app)))

        if failures:
            raise self._aggregate_error(f"Shutdown failed for {len(failures)} step(s)", failures)

        if self._shut_down:
            return
        yield SignalEffect(registry_shutdown, self, {"registry": self, "config": self.config})
        self._shut_down = True

    def _install(self, import_path: str, _chain: list[str], attempt: _InstallAttempt) -> Steps[AppConfig]:
        """Recursive install implementation (must be called inside an install operation).

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
            yield from self._install(dep, chain, attempt)

        with attempt.guard(import_path, "install"):
            if import_path in self._app_configs:
                raise DuplicateModuleError(f"Module '{import_path}' was already installed by a concurrent dependency.")

        self._app_configs[import_path] = app
        self._install_order.append(import_path)

        with attempt.guard(import_path, "on_load"):
            yield HookEffect(app, "on_load")
        app.transition_to(ModuleLifecycleState.LOADED)
        with attempt.guard(import_path, "module_loaded"):
            yield SignalEffect(module_loaded, app, {"registry": self, "module": app, "config": self.config})

        return app

    def _install_atomically(self, import_paths: list[str]) -> Steps[list[AppConfig]]:
        """Install import paths as one failure-atomic operation (must be called inside an install operation).

        Args:
            import_paths: Import paths to install in order.

        Returns:
            The installed ``AppConfig`` for each import path.

        Raises:
            ModuleLifecycleError: The original failure when rollback was clean.
            ModuleStartupRollbackError: If rollback itself failed for some modules.
        """
        introduces_modules = any(path not in self._app_configs for path in import_paths)
        if self._rolling_back and introduces_modules:
            raise ModuleLifecycleError("Cannot install new modules while installation rollback is running.")
        if not introduces_modules:
            return [self._app_configs[import_path] for import_path in import_paths]

        preexisting = set(self._app_configs)
        attempt = _InstallAttempt()
        try:
            installed: list[AppConfig] = []
            for import_path in import_paths:
                installed.append((yield from self._install(import_path, [], attempt)))
            return installed
        except BaseException as exc:
            introduced = [name for name in self._install_order if name not in preexisting]
            unwind_failures = yield from self._rollback(introduced)
            if not unwind_failures or not isinstance(exc, Exception):
                raise

            original = attempt.failure or ModuleFailure(module=import_paths[0], phase="install", error=exc)
            error = self._aggregate_error(
                f"Installation of {', '.join(import_paths)} failed ({exc}) and cleanup was incomplete",
                [original, *unwind_failures],
                error_type=ModuleStartupRollbackError,
            )
            raise error from error.__cause__

    def _rollback(self, introduced: list[str]) -> Steps[list[ModuleFailure]]:
        """Remove modules introduced by a failed installation (must be called inside an install operation).

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
        self._rolling_back = True
        try:
            for name in reversed(introduced):
                app = self._app_configs[name]
                if app.lifecycle_state in (ModuleLifecycleState.LOADING, ModuleLifecycleState.SHUTDOWN):
                    continue
                failures.extend((yield from self._shutdown_module(app)))
        finally:
            removed = set(introduced)
            for name in introduced:
                self._app_configs.pop(name, None)
            self._install_order[:] = [name for name in self._install_order if name not in removed]
            self._rolling_back = False
        return failures

    def _shutdown_module(self, app: AppConfig) -> Steps[list[ModuleFailure]]:
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
            yield HookEffect(app, "on_shutdown")
        except Exception as exc:
            return [ModuleFailure(module=app.name, phase="on_shutdown", error=exc)]

        app.transition_to(ModuleLifecycleState.SHUTDOWN)
        try:
            yield SignalEffect(module_shutdown, app, {"registry": self, "module": app, "config": self.config})
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

    # ------------------------------------------------------------------
    # Hook and receiver invocation
    # ------------------------------------------------------------------

    def _call_hook_sync(self, app: AppConfig, hook_name: str) -> None:
        """Call a bootstrap hook if present using keyword injection only, adapting an awaitable result.

        Hooks must accept keyword invocation using the injected names
        ``registry``, ``app``, ``container``, and ``hooks``. Hooks may declare
        any supported subset or ``**kwargs`` to receive all injected values.
        Legacy positional-only contracts are rejected with a clear error.

        Args:
            app: The module whose bootstrap hook should be called.
            hook_name: One of ``"on_load"``, ``"ready"``, or ``"on_shutdown"``.

        Raises:
            ModuleLifecycleError: If the hook raises an exception.
            ModuleAsyncRequiredError: If the hook is asynchronous and a loop is already running.
        """
        hook = app.get_hook(hook_name)
        if hook is None:
            return
        try:
            kwargs = self._get_hook_kwargs(app=app, hook_name=hook_name, hook=hook)
            subject = f"Hook '{hook_name}' in module '{app.name}'"
            if loop_is_running() and inspect.iscoroutinefunction(hook):
                raise self._async_required(subject)
            result = hook(**kwargs)
            if inspect.isawaitable(result):
                if loop_is_running():
                    discard_awaitable(result)
                    raise self._async_required(subject)
                self._adapter.run(result)
        except ModuleAsyncRequiredError:
            raise
        except Exception as exc:
            raise ModuleLifecycleError(f"Hook '{hook_name}' in module '{app.name}' raised an error: {exc}") from exc

    async def _call_hook_async(self, app: AppConfig, hook_name: str) -> None:
        """Async twin of :meth:`_call_hook_sync`: await coroutine hooks, thread-offload plain ones.

        A plain function that returns an awaitable has it awaited here, on the running loop.

        Raises:
            ModuleLifecycleError: If the hook raises an exception.
        """
        hook = app.get_hook(hook_name)
        if hook is None:
            return
        try:
            kwargs = self._get_hook_kwargs(app=app, hook_name=hook_name, hook=hook)
            if inspect.iscoroutinefunction(hook):
                result = hook(**kwargs)
            else:
                result = await asyncio.to_thread(hook, **kwargs)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            raise ModuleLifecycleError(f"Hook '{hook_name}' in module '{app.name}' raised an error: {exc}") from exc

    def _adapt_receiver_sync(self, receiver: Callable[..., Any]) -> Callable[..., Any]:
        """Adapt an async signal receiver for ``Signal.send`` on the persistent adapter loop.

        This is the ``_async_wrapper`` the signal API already provides; the default wrapper would run each
        receiver on a throwaway loop. In a running loop the receiver is not called at all.
        """

        def adapted(*args: Any, **kwargs: Any) -> Any:
            if loop_is_running():
                name = getattr(receiver, "__qualname__", repr(receiver))
                raise self._async_required(f"Signal receiver '{name}'")
            return self._adapter.run(receiver(*args, **kwargs))

        return adapted

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
