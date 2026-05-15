"""Integration tests for ModuleRegistry._call_hook with DI container and hooks."""

from pathlib import Path

import pytest
from bedrock.module.exc import ModuleLifecycleError
from bedrock.module.registry import ModuleRegistry

from tests.helpers import make_fake_module


class TestCallHookLegacyPositional:
    """Legacy bootstrap hooks called via positional (registry, app)."""

    def test_legacy_positional_hook_receives_registry_and_app(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "legacy_pos",
            manifest={"title": "LegacyPos", "version": "1"},
            bootstrap=(
                "def on_load(r, a):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('legacy_pos', type(r).__name__, a.name))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("legacy_pos")

        assert ("legacy_pos", "ModuleRegistry", "legacy_pos") in builtins._hook_log

    def test_legacy_with_registry_app_named_params(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "legacy_named",
            manifest={"title": "LegacyNamed", "version": "1"},
            bootstrap=(
                "def on_load(registry, app):\n    import builtins\n    builtins._hook_log.append(('named', app.name))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("legacy_named")

        assert ("named", "legacy_named") in builtins._hook_log


class TestCallHookKeywordInjection:
    """New-style hooks with keyword-injected container and hooks."""

    def test_hook_receives_container_kwarg(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_container",
            manifest={"title": "KWContainer", "version": "1"},
            bootstrap=(
                "def on_load(registry, app, container):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('container', type(container).__name__))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_container")

        assert ("container", "Container") in builtins._hook_log

    def test_hook_receives_hooks_kwarg(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_hooks",
            manifest={"title": "KWHooks", "version": "1"},
            bootstrap=(
                "def on_load(registry, app, hooks):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('hooks', type(hooks).__name__))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_hooks")

        assert ("hooks", "HookRegistry") in builtins._hook_log

    def test_hook_receives_all_keyword_injections(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_all",
            manifest={"title": "KWAll", "version": "1"},
            bootstrap=(
                "def on_load(registry, app, container, hooks):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('all', app.name, type(container).__name__, type(hooks).__name__))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_all")

        assert ("all", "kw_all", "Container", "HookRegistry") in builtins._hook_log

    def test_hook_with_kwargs_falls_back_to_positional(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_splat",
            manifest={"title": "KWSplat", "version": "1"},
            bootstrap=(
                "def on_load(registry, app, **kwargs):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('splat', app.name))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_splat")

        assert ("splat", "kw_splat") in builtins._hook_log


class TestCallHookNoHook:
    """Modules without bootstrap hooks are handled gracefully."""

    def test_no_bootstrap_module(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "no_bootstrap",
            manifest={"title": "NoBootstrap", "version": "1"},
        )

        registry = ModuleRegistry()
        registry.install("no_bootstrap")

        assert registry.is_installed("no_bootstrap")

    def test_bootstrap_without_hook_function(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "empty_bootstrap",
            manifest={"title": "EmptyBootstrap", "version": "1"},
            bootstrap="pass\n",
        )

        registry = ModuleRegistry()
        registry.install("empty_bootstrap")

        assert registry.is_installed("empty_bootstrap")


class TestCallHookErrorWrapping:
    """Hook exceptions are wrapped in ModuleLifecycleError."""

    def test_positional_hook_error_wrapped(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "err_pos",
            manifest={"title": "ErrPos", "version": "1"},
            bootstrap="def on_load(r, a): raise RuntimeError('positional boom')\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="on_load.*raised an error"):
            registry.install("err_pos")

    def test_keyword_hook_error_wrapped(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "err_kw",
            manifest={"title": "ErrKW", "version": "1"},
            bootstrap="def on_load(registry, app, container): raise ValueError('keyword boom')\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="on_load.*raised an error"):
            registry.install("err_kw")

    def test_ready_hook_error_wrapped(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "err_ready",
            manifest={"title": "ErrReady", "version": "1"},
            bootstrap="def ready(registry, app): raise RuntimeError('ready boom')\n",
        )

        registry = ModuleRegistry()
        registry.install("err_ready")

        with pytest.raises(ModuleLifecycleError, match="ready.*raised an error"):
            registry.mark_ready()


class TestCallHookLifecycleIntegration:
    """Full lifecycle: on_load → ready → on_shutdown with keyword injection."""

    def test_full_lifecycle_with_hooks(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "lifecycle",
            manifest={"title": "Lifecycle", "version": "1"},
            bootstrap=(
                "def on_load(registry, app, container):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('on_load', app.name))\n"
                "\n"
                "def ready(registry, app, hooks):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('ready', app.name))\n"
                "\n"
                "def on_shutdown(registry, app):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('on_shutdown', app.name))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.populate(["lifecycle"])
        registry.shutdown()

        log = builtins._hook_log
        on_load_entry = next(e for e in log if e[0] == "on_load")
        ready_entry = next(e for e in log if e[0] == "ready")
        shutdown_entry = next(e for e in log if e[0] == "on_shutdown")

        assert on_load_entry == ("on_load", "lifecycle")
        assert ready_entry == ("ready", "lifecycle")
        assert shutdown_entry == ("on_shutdown", "lifecycle")

    def test_dependency_order_with_hooks(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "base_mod",
            manifest={"title": "Base", "version": "1"},
            bootstrap=(
                "def on_load(registry, app):\n    import builtins\n    builtins._hook_log.append(('load', app.name))\n"
            ),
        )
        make_fake_module(
            fake_package,
            "dep_mod",
            manifest={"title": "Dep", "version": "1", "depends_on": ["base_mod"]},
            bootstrap=(
                "def on_load(registry, app):\n    import builtins\n    builtins._hook_log.append(('load', app.name))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.populate(["dep_mod"])

        load_entries = [e for e in builtins._hook_log if e[0] == "load"]
        assert [e[1] for e in load_entries] == ["base_mod", "dep_mod"]

    def test_multiple_modules_mixed_hook_styles(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "mod_legacy",
            manifest={"title": "Legacy", "version": "1"},
            bootstrap=("def on_load(r, a):\n    import builtins\n    builtins._hook_log.append(('legacy', a.name))\n"),
        )
        make_fake_module(
            fake_package,
            "mod_new",
            manifest={"title": "New", "version": "1", "depends_on": ["mod_legacy"]},
            bootstrap=(
                "def on_load(registry, app, container):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('new', app.name, type(container).__name__))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.populate(["mod_new"])

        assert ("legacy", "mod_legacy") in builtins._hook_log
        assert ("new", "mod_new", "Container") in builtins._hook_log
