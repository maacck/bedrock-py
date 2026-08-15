"""Unit tests for lightweight CLI helper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import bedrock.cli.apps as cli_apps
import bedrock.cli.run as cli_run
import pytest
import typer
from bedrock.module.exc import InvalidManifestError, InvalidModuleCallableError


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

    def test_inspect_dependency_validates_hooks_without_execution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook_calls: list[str] = []
        validated_hooks: list[str] = []

        def install(**kwargs: object) -> None:
            del kwargs
            hook_calls.append("install")

        def pre_install(**kwargs: object) -> None:
            del kwargs
            hook_calls.append("pre_install")

        def post_install(**kwargs: object) -> None:
            del kwargs
            hook_calls.append("post_install")

        monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
        monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=object(), models_module=None),
        )
        monkeypatch.setattr(
            cli_apps,
            "_load_optional_installation_hook",
            lambda app_name, hook_name: {
                "pre_install": pre_install,
                "install": install,
                "post_install": post_install,
            }.get(hook_name),
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
        assert hook_calls == []

    def test_basic_inspect_validates_hooks_without_execution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hook_calls: list[str] = []

        def install(**kwargs: object) -> None:
            del kwargs
            hook_calls.append("install")

        monkeypatch.setattr(cli_apps, "load_manifest", lambda import_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda import_path: SimpleNamespace(name=import_path, bootstrap_module=None, models_module=None),
        )
        monkeypatch.setattr(
            cli_apps,
            "_load_optional_installation_hook",
            lambda app_name, hook_name: install if hook_name == "install" else None,
        )

        result = cli_apps._run_basic_inspect("demo.app", MagicMock())

        assert result.installation_valid is True
        assert result.errors == []
        assert hook_calls == []

    def test_basic_inspect_allows_missing_installation_hooks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        console = MagicMock()
        monkeypatch.setattr(cli_apps, "load_manifest", lambda import_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda import_path: SimpleNamespace(name=import_path, bootstrap_module=None, models_module=None),
        )
        monkeypatch.setattr(cli_apps, "_load_optional_installation_hook", lambda app_name, hook_name: None)

        result = cli_apps._run_basic_inspect("demo.app", console)

        assert result.installation_valid is False
        assert result.errors == []
        console.print.assert_any_call("[bold yellow]![/bold yellow] No installation hooks found; validation skipped.")

    def test_inspect_dependency_allows_missing_installation_hooks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
        monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=None, models_module=None),
        )
        monkeypatch.setattr(cli_apps, "_load_optional_installation_hook", lambda app_name, hook_name: None)

        result = cli_apps._inspect_dependency("demo.app")

        assert result.installation_valid is False
        assert result.errors == []

    def test_inspect_dependency_reports_invalid_present_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def install() -> None:
            return None

        monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
        monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=None, models_module=None),
        )
        monkeypatch.setattr(
            cli_apps,
            "_load_optional_installation_hook",
            lambda app_name, hook_name: install if hook_name == "install" else None,
        )

        result = cli_apps._inspect_dependency("demo.app")

        assert result.installation_valid is False
        assert len(result.errors) == 1
        assert "must accept **kwargs" in result.errors[0]

    def test_validate_installation_hooks_allows_missing_installation_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def import_missing(module_name: str) -> object:
            raise ModuleNotFoundError(f"No module named '{module_name}'", name=module_name)

        monkeypatch.setattr(cli_apps, "import_module", import_missing)

        assert cli_apps._validate_installation_hooks("demo.app") is False

    def test_basic_inspect_reports_broken_installation_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_apps, "load_manifest", lambda import_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda import_path: SimpleNamespace(name=import_path, bootstrap_module=None, models_module=None),
        )

        def import_broken(module_name: str) -> object:
            raise ModuleNotFoundError("No module named 'required_dependency'", name="required_dependency")

        monkeypatch.setattr(cli_apps, "import_module", import_broken)

        result = cli_apps._run_basic_inspect("demo.app", MagicMock())

        assert result.installation_valid is False
        assert result.errors == [
            "Installation hook check failed: Cannot import installation module "
            "'demo.app.installation': No module named 'required_dependency'"
        ]

    def test_inspect_dependency_reports_broken_installation_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_apps, "find_spec", lambda dep_path: object())
        monkeypatch.setattr(cli_apps, "load_manifest", lambda dep_path: object())
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda dep_path: SimpleNamespace(name=dep_path, bootstrap_module=None, models_module=None),
        )

        def import_broken(module_name: str) -> object:
            raise ModuleNotFoundError("No module named 'required_dependency'", name="required_dependency")

        monkeypatch.setattr(cli_apps, "import_module", import_broken)

        result = cli_apps._inspect_dependency("demo.app")

        assert result.installation_valid is False
        assert result.errors == [
            "Installation: Cannot import installation module 'demo.app.installation': "
            "No module named 'required_dependency'"
        ]

    def test_validate_installation_hooks_rejects_non_callable_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_apps, "import_module", lambda module_name: SimpleNamespace(install=42))

        with pytest.raises(InvalidModuleCallableError, match="is not callable"):
            cli_apps._validate_installation_hooks("demo.app")


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

        monkeypatch.setattr(apps, "populate", lambda import_paths: [app_config])
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

        monkeypatch.setattr(apps, "populate", lambda import_paths: None)
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


class TestPlaybookCommand:
    """Tests for the ``bedrock app playbook`` command."""

    class _ConsoleDouble:
        """Lightweight console spy for capturing printed output."""

        instances: list[TestPlaybookCommand._ConsoleDouble] = []

        def __init__(self) -> None:
            self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []
            self.__class__.instances.append(self)

        def print(self, *args: object, **kwargs: object) -> None:
            self.messages.append((args, kwargs))

    @staticmethod
    def _make_app_config(package_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(package_dir=package_dir)

    def _patch_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._ConsoleDouble.instances.clear()
        monkeypatch.setattr(cli_apps, "Console", self._ConsoleDouble)

    def _latest_messages(self) -> list[tuple[tuple[object, ...], dict[str, object]]]:
        return self._ConsoleDouble.instances[-1].messages

    def test_playbook_reads_default_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        package_dir = tmp_path / "demo_app"
        playbook_dir = package_dir / "playbook"
        playbook_dir.mkdir(parents=True)
        (playbook_dir / "PLAYBOOK.md").write_text("default playbook\n", encoding="utf-8")

        self._patch_console(monkeypatch)
        monkeypatch.setattr(cli_apps, "build_app_config", lambda import_path: self._make_app_config(package_dir))

        cli_apps.playbook("demo.app")

        messages = self._latest_messages()
        assert messages == [(("default playbook\n",), {"markup": False, "highlight": False, "end": ""})]

    def test_playbook_reads_referenced_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        package_dir = tmp_path / "demo_app"
        playbook_dir = package_dir / "playbook" / "references"
        playbook_dir.mkdir(parents=True)
        (playbook_dir / "some-file.md").write_text("referenced playbook\n", encoding="utf-8")

        self._patch_console(monkeypatch)
        monkeypatch.setattr(cli_apps, "build_app_config", lambda import_path: self._make_app_config(package_dir))

        cli_apps.playbook("demo.app", "references/some-file.md")

        messages = self._latest_messages()
        assert messages == [(("referenced playbook\n",), {"markup": False, "highlight": False, "end": ""})]

    @pytest.mark.parametrize(
        ("path", "expected_message"),
        [
            ("/tmp/PLAYBOOK.md", "Playbook path must be relative to the module's playbook directory."),
            ("../PLAYBOOK.md", "Playbook path must not contain parent directory traversal."),
        ],
    )
    def test_playbook_rejects_invalid_paths(
        self,
        path: str,
        expected_message: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        package_dir = tmp_path / "demo_app"
        (package_dir / "playbook").mkdir(parents=True)

        self._patch_console(monkeypatch)
        monkeypatch.setattr(cli_apps, "build_app_config", lambda import_path: self._make_app_config(package_dir))

        with pytest.raises(typer.Exit) as exc_info:
            cli_apps.playbook("demo.app", path)

        assert exc_info.value.exit_code == 1
        assert self._latest_messages() == [((f"[bold red]Error:[/bold red] {expected_message}",), {})]

    def test_playbook_raises_for_missing_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_console(monkeypatch)
        monkeypatch.setattr(
            cli_apps,
            "build_app_config",
            lambda import_path: (_ for _ in ()).throw(
                InvalidManifestError("Cannot locate package for import path 'demo.app'.")
            ),
        )

        with pytest.raises(typer.Exit) as exc_info:
            cli_apps.playbook("demo.app")

        assert exc_info.value.exit_code == 1
        assert self._latest_messages() == [
            (("[bold red]Error:[/bold red] Cannot locate package for import path 'demo.app'.",), {})
        ]

    def test_playbook_raises_when_playbook_dir_is_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        package_dir = tmp_path / "demo_app"
        package_dir.mkdir()

        self._patch_console(monkeypatch)
        monkeypatch.setattr(cli_apps, "build_app_config", lambda import_path: self._make_app_config(package_dir))

        with pytest.raises(typer.Exit) as exc_info:
            cli_apps.playbook("demo.app")

        assert exc_info.value.exit_code == 1
        assert self._latest_messages() == [
            ((f"[bold red]Error:[/bold red] Missing playbook directory at '{package_dir / 'playbook'}'.",), {})
        ]

    def test_playbook_raises_when_file_is_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        package_dir = tmp_path / "demo_app"
        playbook_dir = package_dir / "playbook"
        playbook_dir.mkdir(parents=True)

        self._patch_console(monkeypatch)
        monkeypatch.setattr(cli_apps, "build_app_config", lambda import_path: self._make_app_config(package_dir))

        with pytest.raises(typer.Exit) as exc_info:
            cli_apps.playbook("demo.app", "references/missing.md")

        assert exc_info.value.exit_code == 1
        assert self._latest_messages() == [
            (
                (
                    f"[bold red]Error:[/bold red] Missing playbook file at '{playbook_dir / 'references' / 'missing.md'}'.",
                ),
                {},
            )
        ]
