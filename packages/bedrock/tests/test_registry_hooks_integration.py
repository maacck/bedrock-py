"""Integration tests for ModuleRegistry bootstrap hook keyword invocation."""

from pathlib import Path

import pytest
from bedrock.module.exc import ModuleLifecycleError
from bedrock.module.registry import ModuleRegistry

from tests.helpers import make_fake_module


class TestCallHookKeywordInjection:
    """Bootstrap hooks are invoked through the keyword-only contract."""

    def test_hook_receives_registry_and_app_keywords(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_registry_app",
            manifest={"title": "KWRegistryApp", "version": "1"},
            bootstrap=(
                "def on_load(*, registry, app):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('registry_app', type(registry).__name__, app.name))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_registry_app")

        assert ("registry_app", "ModuleRegistry", "kw_registry_app") in builtins._hook_log

    def test_hook_receives_container_kwarg(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_container",
            manifest={"title": "KWContainer", "version": "1"},
            bootstrap=(
                "def on_load(*, container):\n"
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
                "def on_load(*, hooks):\n"
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
                "def on_load(*, registry, app, container, hooks):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('all', app.name, type(container).__name__, type(hooks).__name__, type(registry).__name__))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_all")

        assert ("all", "kw_all", "Container", "HookRegistry", "ModuleRegistry") in builtins._hook_log

    def test_hook_with_kwargs_receives_all_named_injections(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_splat",
            manifest={"title": "KWSplat", "version": "1"},
            bootstrap=(
                "def on_load(**kwargs):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('splat', sorted(kwargs)))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_splat")

        assert ("splat", ["app", "container", "hooks", "registry"]) in builtins._hook_log

    def test_hook_with_named_param_and_kwargs_receives_all_named_injections(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "kw_mixed_kwargs",
            manifest={"title": "KWMixedKwargs", "version": "1"},
            bootstrap=(
                "def on_load(*, app, **kwargs):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('mixed', app.name, sorted(kwargs)))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.install("kw_mixed_kwargs")

        assert ("mixed", "kw_mixed_kwargs", ["container", "hooks", "registry"]) in builtins._hook_log


class TestCallHookUnsupportedSignatures:
    """Unsupported legacy signatures fail with clear errors."""

    def test_legacy_positional_names_are_rejected(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "legacy_pos",
            manifest={"title": "LegacyPos", "version": "1"},
            bootstrap="def on_load(r, a): pass\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="must accept keyword invocation") as exc_info:
            registry.install("legacy_pos")

        message = str(exc_info.value)
        assert "Unsupported required parameters: r, a" in message

    def test_positional_only_parameters_are_rejected(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "pos_only",
            manifest={"title": "PosOnly", "version": "1"},
            bootstrap="def on_load(registry, /, app): pass\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(
            ModuleLifecycleError, match="Positional-only parameters cannot be injected by keyword"
        ) as exc_info:
            registry.install("pos_only")

        assert "Unsupported required parameters: registry" in str(exc_info.value)

    def test_hook_with_no_supported_injected_parameters_is_rejected(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "no_supported_params",
            manifest={"title": "NoSupportedParams", "version": "1"},
            bootstrap="def on_load(flag=False): pass\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="does not declare any supported injected keyword parameters"):
            registry.install("no_supported_params")

    def test_unknown_required_parameter_is_rejected(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "unknown_required",
            manifest={"title": "UnknownRequired", "version": "1"},
            bootstrap="def on_load(*, service): pass\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="Unsupported required parameters: service"):
            registry.install("unknown_required")


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

    def test_keyword_hook_error_wrapped(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "err_kw",
            manifest={"title": "ErrKW", "version": "1"},
            bootstrap="def on_load(*, registry, app, container): raise ValueError('keyword boom')\n",
        )

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="on_load.*raised an error"):
            registry.install("err_kw")

    def test_ready_hook_error_wrapped(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "err_ready",
            manifest={"title": "ErrReady", "version": "1"},
            bootstrap="def ready(*, registry, app): raise RuntimeError('ready boom')\n",
        )

        registry = ModuleRegistry()
        registry.install("err_ready")

        with pytest.raises(ModuleLifecycleError, match="ready.*raised an error"):
            registry.mark_ready()


class TestCallHookLifecycleIntegration:
    """Full lifecycle uses keyword-based hook injection."""

    def test_full_lifecycle_with_hooks(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "lifecycle",
            manifest={"title": "Lifecycle", "version": "1"},
            bootstrap=(
                "def on_load(*, app, container):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('on_load', app.name, type(container).__name__))\n"
                "\n"
                "def ready(*, registry, app, hooks):\n"
                "    import builtins\n"
                "    builtins._hook_log.append(('ready', app.name, type(registry).__name__, type(hooks).__name__))\n"
                "\n"
                "def on_shutdown(*, app):\n"
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
        on_load_entry = next(entry for entry in log if entry[0] == "on_load")
        ready_entry = next(entry for entry in log if entry[0] == "ready")
        shutdown_entry = next(entry for entry in log if entry[0] == "on_shutdown")

        assert on_load_entry == ("on_load", "lifecycle", "Container")
        assert ready_entry == ("ready", "lifecycle", "ModuleRegistry", "HookRegistry")
        assert shutdown_entry == ("on_shutdown", "lifecycle")

    def test_dependency_order_with_hooks(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "base_mod",
            manifest={"title": "Base", "version": "1"},
            bootstrap=(
                "def on_load(*, app):\n    import builtins\n    builtins._hook_log.append(('load', app.name))\n"
            ),
        )
        make_fake_module(
            fake_package,
            "dep_mod",
            manifest={"title": "Dep", "version": "1", "depends_on": ["base_mod"]},
            bootstrap=(
                "def on_load(*, app):\n    import builtins\n    builtins._hook_log.append(('load', app.name))\n"
            ),
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()
        registry.populate(["dep_mod"])

        load_entries = [entry for entry in builtins._hook_log if entry[0] == "load"]
        assert [entry[1] for entry in load_entries] == ["base_mod", "dep_mod"]

    def test_multiple_modules_require_keyword_contract(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "mod_keyword_only",
            manifest={"title": "KeywordOnly", "version": "1"},
            bootstrap=(
                "def on_load(*, app):\n    import builtins\n    builtins._hook_log.append(('keyword_only', app.name))\n"
            ),
        )
        make_fake_module(
            fake_package,
            "mod_bad_legacy",
            manifest={"title": "BadLegacy", "version": "1", "depends_on": ["mod_keyword_only"]},
            bootstrap="def on_load(r, a): pass\n",
        )

        import builtins

        builtins._hook_log = []

        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="Unsupported required parameters: r, a"):
            registry.populate(["mod_bad_legacy"])

        assert ("keyword_only", "mod_keyword_only") in builtins._hook_log
