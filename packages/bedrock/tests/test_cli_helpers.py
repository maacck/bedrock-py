"""Unit tests for lightweight CLI helper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import bedrock.cli.apps as cli_apps
import bedrock.cli.run as cli_run
import pytest
import typer
from bedrock.module.exc import InvalidModuleCallableError


class TestRunHelperParsing:
    """Tests for argument parsing helpers in ``bedrock.cli.run``."""

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            (["--app", "demo.app"], "demo.app"),
            (["-a", "demo.app"], "demo.app"),
            (["--app=demo.app"], "demo.app"),
            (["-a=demo.app"], "demo.app"),
            (["serve", "--app", "demo.app", "--app", "other.app"], "demo.app"),
            (["--app"], None),
            (["serve"], None),
        ],
    )
    def test_parse_args_for_app(self, args: list[str], expected: str | None) -> None:
        assert cli_run._parse_args_for_app(args) == expected

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["run", "--app", "demo.app"], "demo.app"),
            (["run", "-a", "demo.app"], "demo.app"),
            (["run", "--app=demo.app"], "demo.app"),
            (["run", "serve", "--app", "demo.app"], "demo.app"),
            (["serve", "--app", "demo.app", "run"], None),
            (["run", "serve"], "serve"),
            (["run", "my_app", "my_command"], "my_app"),
        ],
    )
    def test_parse_run_args_for_app(self, argv: list[str], expected: str | None) -> None:
        assert cli_run._parse_run_args_for_app(argv) == expected


class TestAppsHelpers:
    """Tests for focused helper behavior in ``bedrock.cli.apps``."""

    @pytest.mark.parametrize(
        ("passed", "optional", "expected"),
        [
            (True, False, "[bold green]✓[/bold green]"),
            (False, False, "[bold red]✗[/bold red]"),
            (False, True, "[dim]-[/dim]"),
        ],
    )
    def test_status_icon(self, passed: bool, optional: bool, expected: str) -> None:
        assert cli_apps._status_icon(passed, optional=optional) == expected

    def test_check_installation_hooks_accepts_kwargs(self) -> None:
        def install(**kwargs: object) -> None:
            del kwargs

        cli_apps._check_installation_hooks(install)

    def test_check_installation_hooks_rejects_missing_kwargs(self) -> None:
        def install() -> None:
            return None

        with pytest.raises(InvalidModuleCallableError, match=r"must accept \*\*kwargs"):
            cli_apps._check_installation_hooks(install)

    def test_inspect_dependency_executes_installation_hooks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_calls: list[dict[str, object]] = []
        validated_hooks: list[str] = []

        def install(**kwargs: object) -> None:
            install_calls.append(kwargs)

        def pre_install(**kwargs: object) -> None:
            del kwargs

        def post_install(**kwargs: object) -> None:
            del kwargs

        monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
        monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=object(), models_module=None),
        )
        monkeypatch.setattr(
            cli_apps,
            "load_optional_callable",
            lambda path: {
                "demo.app.installation:install": install,
                "demo.app.installation:pre_install": pre_install,
                "demo.app.installation:post_install": post_install,
            }.get(path),
        )
        monkeypatch.setattr(cli_apps, "_check_installation_hooks", lambda func: validated_hooks.append(func.__name__))

        result = cli_apps._inspect_dependency("demo.app")

        assert result.import_path == "demo.app"
        assert result.manifest_valid is True
        assert result.module_loads is True
        assert result.has_bootstrap is True
        assert result.has_models is False
        assert result.installation_valid is True
        assert result.errors == []
        assert validated_hooks == ["install", "pre_install", "post_install"]
        assert install_calls == [{}]

    def test_inspect_dependency_reports_missing_install_function(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
        monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=None, models_module=None),
        )
        monkeypatch.setattr(cli_apps, "load_optional_callable", lambda path: None)

        result = cli_apps._inspect_dependency("demo.app")

        assert result.manifest_valid is True
        assert result.module_loads is True
        assert result.installation_valid is False
        assert result.errors == ["No 'install' function in installation.py for demo.app."]


class TestInstallCommand:
    """Tests for the ``bedrock app install`` command."""

    def _make_app_config(self, name: str = "demo.app", version: str = "0.1.0") -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            manifest=SimpleNamespace(version=version),
            models_module=None,
            package_dir=Path(f"/tmp/{name}"),
        )

    def _patch_apps_get(self, monkeypatch: pytest.MonkeyPatch, app_config: SimpleNamespace) -> None:
        from bedrock.module import apps

        monkeypatch.setattr(apps, "get", lambda name: app_config)

    def test_install_runs_all_hooks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_order: list[str] = []

        def pre_install() -> None:
            call_order.append("pre_install")

        def install() -> None:
            call_order.append("install")

        def post_install() -> None:
            call_order.append("post_install")

        app_config = self._make_app_config()
        self._patch_apps_get(monkeypatch, app_config)
        monkeypatch.setattr(cli_apps, "_bootstrap_migrations_manager", lambda: None)
        monkeypatch.setattr(cli_apps.os.path, "exists", lambda path: True)
        monkeypatch.setattr(
            cli_apps,
            "load_optional_callable",
            lambda path: {
                "demo.app.installation:install": install,
                "demo.app.installation:pre_install": pre_install,
                "demo.app.installation:post_install": post_install,
            }.get(path),
        )

        cli_apps.install("demo.app", skip_migrations=True)

        assert call_order == ["pre_install", "install", "post_install"]

    def test_install_with_skip_migrations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_order: list[str] = []

        def install() -> None:
            call_order.append("install")

        app_config = self._make_app_config()
        self._patch_apps_get(monkeypatch, app_config)
        monkeypatch.setattr(cli_apps.os.path, "exists", lambda path: True)
        monkeypatch.setattr(
            cli_apps,
            "load_optional_callable",
            lambda path: {"demo.app.installation:install": install}.get(path),
        )

        cli_apps.install("demo.app", skip_migrations=True)

        assert call_order == ["install"]

    def test_install_runs_migrations_when_not_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = MagicMock()
        manager.ensure_schema.return_value = "created"

        app_config = self._make_app_config()
        app_config.models_module = object()

        self._patch_apps_get(monkeypatch, app_config)
        monkeypatch.setattr(cli_apps, "_bootstrap_migrations_manager", lambda: manager)
        monkeypatch.setattr(cli_apps.os.path, "exists", lambda path: True)
        monkeypatch.setattr(
            cli_apps,
            "load_optional_callable",
            lambda path: {"demo.app.installation:install": lambda: None}.get(path),
        )

        cli_apps.install("demo.app", skip_migrations=False)

        manager.ensure_schema.assert_called_once_with("demo.app")

    def test_install_skips_hooks_when_no_installation_py(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app_config = self._make_app_config()
        self._patch_apps_get(monkeypatch, app_config)
        monkeypatch.setattr(cli_apps, "_bootstrap_migrations_manager", lambda: None)
        monkeypatch.setattr(cli_apps.os.path, "exists", lambda path: False)

        cli_apps.install("demo.app", skip_migrations=True)

    def test_install_skips_hooks_when_no_install_function(self, monkeypatch: pytest.MonkeyPatch) -> None:
        app_config = self._make_app_config()
        self._patch_apps_get(monkeypatch, app_config)
        monkeypatch.setattr(cli_apps, "_bootstrap_migrations_manager", lambda: None)
        monkeypatch.setattr(cli_apps.os.path, "exists", lambda path: True)
        monkeypatch.setattr(cli_apps, "load_optional_callable", lambda path: None)

        cli_apps.install("demo.app", skip_migrations=True)

    def test_install_with_only_install_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_order: list[str] = []

        def install() -> None:
            call_order.append("install")

        app_config = self._make_app_config()
        self._patch_apps_get(monkeypatch, app_config)
        monkeypatch.setattr(cli_apps, "_bootstrap_migrations_manager", lambda: None)
        monkeypatch.setattr(cli_apps.os.path, "exists", lambda path: True)
        monkeypatch.setattr(
            cli_apps,
            "load_optional_callable",
            lambda path: {"demo.app.installation:install": install}.get(path),
        )

        cli_apps.install("demo.app", skip_migrations=True)

        assert call_order == ["install"]

    def test_install_raises_on_unknown_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bedrock.module import apps

        monkeypatch.setattr(apps, "get", lambda name: (_ for _ in ()).throw(KeyError(name)))

        with pytest.raises(typer.Exit):
            cli_apps.install("nonexistent.app", skip_migrations=True)

    def test_install_raises_on_migration_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bedrock.database.migrations_manager import MigrationError

        manager = MagicMock()
        manager.ensure_schema.side_effect = MigrationError("schema failed")

        app_config = self._make_app_config()
        app_config.models_module = object()

        self._patch_apps_get(monkeypatch, app_config)
        monkeypatch.setattr(cli_apps, "_bootstrap_migrations_manager", lambda: manager)

        with pytest.raises(typer.Exit):
            cli_apps.install("demo.app", skip_migrations=False)
