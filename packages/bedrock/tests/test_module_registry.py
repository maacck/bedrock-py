"""Unit tests for the Bedrock module registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bedrock.module.entities import AppConfig
from bedrock.module.exc import (
    AppRegistryNotReady,
    ModuleDependencyError,
    ModuleLifecycleError,
)
from bedrock.module.registry import ModuleRegistry
from bedrock.module.signals import module_loaded, module_ready, module_shutdown, registry_ready, registry_shutdown

from tests.helpers import make_fake_module


class TestDependencyResolution:
    """Installation order and dependency graph validation."""

    def test_installs_dependencies_in_order(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "core", manifest={"title": "Core", "version": "1"})
        make_fake_module(
            fake_package,
            "api",
            manifest={"title": "API", "version": "1", "depends_on": ["core"]},
        )

        registry = ModuleRegistry()
        registry.install("api")

        assert registry.list_installed() == ["core", "api"]

    def test_installs_deep_dependency_chain(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "db", manifest={"title": "DB", "version": "1"})
        make_fake_module(
            fake_package,
            "models",
            manifest={"title": "Models", "version": "1", "depends_on": ["db"]},
        )
        make_fake_module(
            fake_package,
            "api",
            manifest={"title": "API", "version": "1", "depends_on": ["models"]},
        )

        registry = ModuleRegistry()
        registry.install("api")

        assert registry.list_installed() == ["db", "models", "api"]

    def test_repeated_install_is_noop(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "once", manifest={"title": "Once", "version": "1"})

        registry = ModuleRegistry()
        first = registry.install("once")
        second = registry.install("once")

        assert registry.list_installed() == ["once"]
        assert first is second

    def test_detects_circular_dependency(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "loop_a",
            manifest={"title": "Loop A", "version": "1", "depends_on": ["loop_b"]},
        )
        make_fake_module(
            fake_package,
            "loop_b",
            manifest={"title": "Loop B", "version": "1", "depends_on": ["loop_a"]},
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleDependencyError, match="Circular dependency detected"):
            registry.install("loop_a")

    def test_detects_self_dependency(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "self_loop",
            manifest={"title": "Self", "version": "1", "depends_on": ["self_loop"]},
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleDependencyError, match="Circular dependency detected"):
            registry.install("self_loop")


class TestReadinessApi:
    """``get`` / ``all`` raise before the registry is marked ready."""

    def test_get_raises_before_ready(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "mod_a", manifest={"title": "A", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_a")

        with pytest.raises(AppRegistryNotReady):
            registry.get("mod_a")

    def test_all_raises_before_ready(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "mod_b", manifest={"title": "B", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_b")

        with pytest.raises(AppRegistryNotReady):
            registry.all()

    def test_get_and_all_work_after_mark_ready(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "mod_c", manifest={"title": "C", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_c")
        registry.mark_ready()

        assert registry.get("mod_c").name == "mod_c"
        assert len(registry.all()) == 1

    def test_is_installed_works_before_ready(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "mod_d", manifest={"title": "D", "version": "1"})

        registry = ModuleRegistry()
        registry.install("mod_d")

        assert registry.is_installed("mod_d") is True
        assert registry.is_installed("missing") is False


class TestPopulate:
    """Bulk installation via ``populate``."""

    def test_populate_installs_multiple_modules(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "alpha", manifest={"title": "Alpha", "version": "1"})
        make_fake_module(fake_package, "beta", manifest={"title": "Beta", "version": "1"})

        registry = ModuleRegistry()
        result = registry.populate(["alpha", "beta"])

        assert registry.ready is True
        assert [app.name for app in result] == ["alpha", "beta"]

    def test_populate_is_not_reentrant(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "reent", manifest={"title": "Reent", "version": "1"})

        registry = ModuleRegistry()
        registry._loading = True  # simulate in-progress populate

        with pytest.raises(RuntimeError, match="not reentrant"):
            registry.populate(["reent"])


class TestMarkReady:
    """Lifecycle transition from *loaded* to *ready*."""

    def test_calls_ready_hooks_and_emits_signals(self, fake_package: Path) -> None:
        calls: list[str] = []

        def ready_hook(registry: ModuleRegistry, app: AppConfig) -> None:
            calls.append(f"ready:{app.name}")

        make_fake_module(
            fake_package,
            "ready_mod",
            manifest={"title": "Ready", "version": "1"},
            bootstrap="def ready(r, a): __import__('builtins')._test_calls.append(f'ready:{a.name}')\n",
        )

        # Patch the hook into the bootstrap module after import
        import importlib

        registry = ModuleRegistry()
        registry.install("ready_mod")

        # Manually inject the hook by reloading bootstrap
        mod = importlib.import_module("ready_mod.bootstrap")
        mod.ready = ready_hook  # type: ignore[attr-defined]

        # Rebuild app config so the hook is picked up
        from bedrock.module.manifest import build_app_config

        app = build_app_config("ready_mod")
        registry._app_configs["ready_mod"] = app

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
        assert "ready:ready_mod" in calls
        assert "module_ready" in sig_calls
        assert "registry_ready" in reg_calls

    def test_raises_module_lifecycle_error_on_bad_ready_hook(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "bad_ready",
            manifest={"title": "Bad Ready", "version": "1"},
            bootstrap="def ready(r, a): raise ValueError('boom')\n",
        )

        registry = ModuleRegistry()
        registry.install("bad_ready")

        with pytest.raises(ModuleLifecycleError, match="ready.*raised an error"):
            registry.mark_ready()


class TestShutdown:
    """Reverse-order shutdown and signal emission."""

    def test_shutdown_calls_hooks_in_reverse_order(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "first",
            manifest={"title": "First", "version": "1"},
            bootstrap="def on_shutdown(r, a): __import__('builtins')._shutdown_order.append(a.name)\n",
        )
        make_fake_module(
            fake_package,
            "second",
            manifest={"title": "Second", "version": "1", "depends_on": ["first"]},
            bootstrap="def on_shutdown(r, a): __import__('builtins')._shutdown_order.append(a.name)\n",
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

    def test_shutdown_wraps_hook_errors(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "bad_shutdown",
            manifest={"title": "Bad Shutdown", "version": "1"},
            bootstrap="def on_shutdown(r, a): raise RuntimeError('shutdown boom')\n",
        )

        registry = ModuleRegistry()
        registry.populate(["bad_shutdown"])

        with pytest.raises(ModuleLifecycleError, match="on_shutdown.*raised an error"):
            registry.shutdown()


class TestLifecycleHooks:
    """on_load hook invocation and error wrapping."""

    def test_on_load_hook_called_during_install(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "load_mod",
            manifest={"title": "Load", "version": "1"},
            bootstrap="def on_load(r, a): __import__('builtins')._load_calls.append(a.name)\n",
        )

        import builtins

        builtins._load_calls = []

        registry = ModuleRegistry()
        registry.install("load_mod")

        assert builtins._load_calls == ["load_mod"]

    def test_on_load_error_raises_module_lifecycle_error(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "bad_load",
            manifest={"title": "Bad Load", "version": "1"},
            bootstrap="def on_load(r, a): raise ZeroDivisionError('load boom')\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="on_load.*raised an error"):
            registry.install("bad_load")

    def test_module_loaded_signal_emitted(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "sig_mod", manifest={"title": "Sig", "version": "1"})

        events: list[str] = []

        def receiver(sender: Any, **kwargs: Any) -> None:
            events.append(kwargs["module"].name)

        module_loaded.connect(receiver, weak=False)

        registry = ModuleRegistry()
        registry.install("sig_mod")

        assert events == ["sig_mod"]
