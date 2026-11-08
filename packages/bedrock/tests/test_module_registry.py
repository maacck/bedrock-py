"""Unit tests for the Bedrock module registry."""

from __future__ import annotations

from typing import Any

import pytest
from bedrock.module.exc import (
    AppRegistryNotReady,
    ModuleDependencyError,
    ModuleLifecycleError,
)
from bedrock.module.registry import ModuleRegistry
from bedrock.module.signals import module_loaded, module_ready, module_shutdown, registry_ready, registry_shutdown
from bedrock.testing.modules import ModuleFactory


class TestDependencyResolution:
    """Installation order and dependency graph validation."""

    def test_installs_dependencies_in_order(self, module_factory: ModuleFactory) -> None:
        module_factory("core", manifest={"title": "Core", "version": "1"})
        module_factory(
            "api",
            manifest={"title": "API", "version": "1", "depends_on": ["core"]},
        )

        registry = ModuleRegistry()
        registry.install("api")

        assert registry.list_installed() == ["core", "api"]

    def test_installs_deep_dependency_chain(self, module_factory: ModuleFactory) -> None:
        module_factory("db", manifest={"title": "DB", "version": "1"})
        module_factory(
            "models",
            manifest={"title": "Models", "version": "1", "depends_on": ["db"]},
        )
        module_factory(
            "api",
            manifest={"title": "API", "version": "1", "depends_on": ["models"]},
        )

        registry = ModuleRegistry()
        registry.install("api")

        assert registry.list_installed() == ["db", "models", "api"]

    def test_repeated_install_is_noop(self, module_factory: ModuleFactory) -> None:
        module_factory("once", manifest={"title": "Once", "version": "1"})

        registry = ModuleRegistry()
        first = registry.install("once")
        second = registry.install("once")

        assert registry.list_installed() == ["once"]
        assert first is second

    def test_detects_circular_dependency(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "loop_a",
            manifest={"title": "Loop A", "version": "1", "depends_on": ["loop_b"]},
        )
        module_factory(
            "loop_b",
            manifest={"title": "Loop B", "version": "1", "depends_on": ["loop_a"]},
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleDependencyError, match="Circular dependency detected"):
            registry.install("loop_a")

    def test_detects_self_dependency(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "self_loop",
            manifest={"title": "Self", "version": "1", "depends_on": ["self_loop"]},
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleDependencyError, match="Circular dependency detected"):
            registry.install("self_loop")


class TestReadinessApi:
    """``get`` / ``all`` raise before the registry is marked ready."""

    def test_get_raises_before_ready(self, module_factory: ModuleFactory) -> None:
        module_factory("mod_a", manifest={"title": "A", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_a")

        with pytest.raises(AppRegistryNotReady):
            registry.get("mod_a")

    def test_all_raises_before_ready(self, module_factory: ModuleFactory) -> None:
        module_factory("mod_b", manifest={"title": "B", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_b")

        with pytest.raises(AppRegistryNotReady):
            registry.all()

    def test_get_and_all_work_after_mark_ready(self, module_factory: ModuleFactory) -> None:
        module_factory("mod_c", manifest={"title": "C", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_c")
        registry.mark_ready()

        assert registry.get("mod_c").name == "mod_c"
        assert len(registry.all()) == 1

    def test_is_installed_works_before_ready(self, module_factory: ModuleFactory) -> None:
        module_factory("mod_d", manifest={"title": "D", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_d")

        assert registry.is_installed("mod_d") is True
        assert registry.is_installed("missing") is False


class TestPopulate:
    """Bulk installation via ``populate``."""

    def test_populate_installs_multiple_modules(self, module_factory: ModuleFactory) -> None:
        module_factory("alpha", manifest={"title": "Alpha", "version": "1"})
        module_factory("beta", manifest={"title": "Beta", "version": "1"})

        registry = ModuleRegistry()
        result = registry.populate(["alpha", "beta"])

        assert registry.ready is True
        assert [app.name for app in result] == ["alpha", "beta"]

    def test_populate_is_not_reentrant(self, module_factory: ModuleFactory) -> None:
        module_factory("reent", manifest={"title": "Reent", "version": "1"})

        registry = ModuleRegistry()
        registry._loading = True  # simulate in-progress populate

        with pytest.raises(ModuleLifecycleError, match="not reentrant"):
            registry.populate(["reent"])

    def test_populate_resets_loading_flag_after_hook_failure(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "bad_load",
            manifest={"title": "BadLoad", "version": "1"},
            bootstrap="def on_load(r, a): pass\n",
        )
        module_factory("good_load", manifest={"title": "GoodLoad", "version": "1"})

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="must accept keyword invocation"):
            registry.populate(["bad_load"])

        assert registry._loading is False
        assert registry.is_installed("bad_load") is False

        result = registry.populate(["good_load"])

        assert registry.ready is True
        assert [app.name for app in result] == ["good_load"]


class TestMarkReady:
    """Lifecycle transition from *loaded* to *ready*."""

    def test_calls_ready_hooks_and_emits_signals(
        self, module_factory: ModuleFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        ready_calls: list[str] = []
        monkeypatch.setattr(builtins, "_ready_calls", ready_calls, raising=False)
        module_factory(
            "ready_mod",
            manifest={"title": "Ready", "version": "1"},
            bootstrap="def ready(*, app): __import__('builtins')._ready_calls.append(f'ready:{app.name}')\n",
        )

        registry = ModuleRegistry()
        registry.install("ready_mod")

        sig_calls: list[str] = []

        def receiver(sender: Any, **kwargs: Any) -> None:
            sig_calls.append("module_ready")

        module_ready.connect(receiver, weak=False)

        reg_calls: list[str] = []

        def reg_receiver(sender: Any, **kwargs: Any) -> None:
            reg_calls.append("registry_ready")

        registry_ready.connect(reg_receiver, weak=False)

        registry.mark_ready()

        assert registry.ready is True
        assert ready_calls == ["ready:ready_mod"]
        assert "module_ready" in sig_calls
        assert "registry_ready" in reg_calls

    def test_raises_module_lifecycle_error_on_bad_ready_hook(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "bad_ready",
            manifest={"title": "Bad Ready", "version": "1"},
            bootstrap="def ready(*, registry, app): raise ValueError('boom')\n",
        )

        registry = ModuleRegistry()
        registry.install("bad_ready")

        with pytest.raises(ModuleLifecycleError, match="ready.*raised an error"):
            registry.mark_ready()


class TestShutdown:
    """Reverse-order shutdown and signal emission."""

    def test_shutdown_calls_hooks_in_reverse_order(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "first",
            manifest={"title": "First", "version": "1"},
            bootstrap="def on_shutdown(*, app): __import__('builtins')._shutdown_order.append(app.name)\n",
        )
        module_factory(
            "second",
            manifest={"title": "Second", "version": "1", "depends_on": ["first"]},
            bootstrap="def on_shutdown(*, app): __import__('builtins')._shutdown_order.append(app.name)\n",
        )

        import builtins

        builtins._shutdown_order = []

        registry = ModuleRegistry()
        registry.populate(["second"])

        sig_calls: list[str] = []

        def receiver(sender: Any, **kwargs: Any) -> None:
            sig_calls.append("module_shutdown")

        module_shutdown.connect(receiver, weak=False)

        reg_calls: list[str] = []

        def reg_receiver(sender: Any, **kwargs: Any) -> None:
            reg_calls.append("registry_shutdown")

        registry_shutdown.connect(reg_receiver, weak=False)

        registry.shutdown()

        assert builtins._shutdown_order == ["second", "first"]
        assert "module_shutdown" in sig_calls
        assert "registry_shutdown" in reg_calls

    def test_shutdown_wraps_hook_errors(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "bad_shutdown",
            manifest={"title": "Bad Shutdown", "version": "1"},
            bootstrap="def on_shutdown(*, registry, app): raise RuntimeError('shutdown boom')\n",
        )

        registry = ModuleRegistry()
        registry.populate(["bad_shutdown"])

        with pytest.raises(ModuleLifecycleError, match="on_shutdown.*raised an error"):
            registry.shutdown()


class TestLifecycleHooks:
    """on_load hook invocation and error wrapping."""

    def test_on_load_hook_called_during_install(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "load_mod",
            manifest={"title": "Load", "version": "1"},
            bootstrap="def on_load(*, app): __import__('builtins')._load_calls.append(app.name)\n",
        )

        import builtins

        builtins._load_calls = []

        registry = ModuleRegistry()
        registry.install("load_mod")

        assert builtins._load_calls == ["load_mod"]

    def test_on_load_error_raises_module_lifecycle_error(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "bad_load",
            manifest={"title": "Bad Load", "version": "1"},
            bootstrap="def on_load(*, registry, app): raise ZeroDivisionError('load boom')\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="on_load.*raised an error"):
            registry.install("bad_load")

    def test_unsupported_legacy_positional_hook_raises_clear_error(self, module_factory: ModuleFactory) -> None:
        module_factory(
            "legacy_bad_load",
            manifest={"title": "Legacy Bad Load", "version": "1"},
            bootstrap="def on_load(r, a): pass\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="must accept keyword invocation") as exc_info:
            registry.install("legacy_bad_load")

        assert "Unsupported required parameters: r, a" in str(exc_info.value)

    def test_module_loaded_signal_emitted(self, module_factory: ModuleFactory) -> None:
        module_factory("sig_mod", manifest={"title": "Sig", "version": "1"})

        events: list[str] = []

        def receiver(sender: Any, **kwargs: Any) -> None:
            events.append(kwargs["module"].name)

        module_loaded.connect(receiver, weak=False)

        registry = ModuleRegistry()
        registry.install("sig_mod")

        assert events == ["sig_mod"]
