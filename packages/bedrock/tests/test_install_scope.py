"""Semantic tests for which modules ``bedrock app install`` loads and installs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import bedrock.cli.apps as cli_apps
import bedrock.cli.db as cli_db
import pytest
import typer
from bedrock.module.registry import ModuleRegistry

# name -> depends_on. ``scope_t`` sits in a diamond (t -> b, c -> d); ``scope_root`` also loads unrelated ``scope_u``.
_PACKAGES = {
    "scope_d": [],
    "scope_b": ["scope_d"],
    "scope_c": ["scope_d"],
    "scope_t": ["scope_b", "scope_c"],
    "scope_u": [],
    "scope_root": ["scope_t", "scope_u"],
}


@pytest.fixture(scope="module")
def package_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("scope_packages")
    for name, depends_on in _PACKAGES.items():
        package = root / name
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "models.py").write_text("")
        (package / "installation.py").write_text("")
        deps = "".join(f"  - {dep}\n" for dep in depends_on)
        (package / "manifest.yaml").write_text(f"title: {name}\ndepends_on:\n{deps}" if deps else f"title: {name}\n")
    return root


@pytest.fixture
def installed(package_root: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fresh registry with real packages; returns the modules whose ``install`` hook ran, in order."""
    monkeypatch.syspath_prepend(str(package_root))
    monkeypatch.setattr("bedrock.module.apps", ModuleRegistry())
    calls: list[str] = []

    def load_hook(path: str):
        module, hook = path.split(".installation:")
        return (lambda: calls.append(module)) if hook == "install" else None

    monkeypatch.setattr(cli_apps, "load_optional_callable", load_hook)
    return calls


@pytest.fixture
def schema_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the migrations manager; returns modules passed to ``ensure_schema`` in order."""
    calls: list[str] = []
    manager = MagicMock()
    manager.ensure_schema.side_effect = lambda name: calls.append(name) or "up-to-date"
    monkeypatch.setattr(cli_db, "build_migrations_manager", lambda registry: manager)
    return calls


def test_default_installs_only_target(installed: list[str], schema_calls: list[str]) -> None:
    cli_apps.install("scope_t")

    assert installed == ["scope_t"]
    assert schema_calls == ["scope_t"]


def test_root_loads_unrelated_dependency_without_installing_it(installed: list[str], schema_calls: list[str]) -> None:
    from bedrock.module import apps

    cli_apps.install("scope_t", root="scope_root")

    assert apps.is_installed("scope_u")
    assert installed == ["scope_t"]
    assert schema_calls == ["scope_t"]


def test_with_dependencies_installs_diamond_once_in_dependency_order(
    installed: list[str], schema_calls: list[str]
) -> None:
    cli_apps.install("scope_t", with_dependencies=True)

    assert installed == ["scope_d", "scope_b", "scope_c", "scope_t"]
    assert schema_calls == installed


def test_with_dependencies_excludes_modules_outside_target_closure(
    installed: list[str], schema_calls: list[str]
) -> None:
    cli_apps.install("scope_t", root="scope_root", with_dependencies=True)

    assert installed == ["scope_d", "scope_b", "scope_c", "scope_t"]


def test_skip_migrations_skips_schema_only(installed: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(registry: object) -> None:
        raise AssertionError("migrations manager must not be built")

    monkeypatch.setattr(cli_db, "build_migrations_manager", fail)

    cli_apps.install("scope_t", with_dependencies=True, skip_migrations=True)

    assert installed == ["scope_d", "scope_b", "scope_c", "scope_t"]


def test_target_outside_root_is_rejected_before_any_install(installed: list[str], schema_calls: list[str]) -> None:
    from bedrock.module import apps

    apps.populate(["scope_u"])  # Already loaded, but not reachable from the requested root.
    with pytest.raises(typer.Exit) as exc_info:
        cli_apps.install("scope_u", root="scope_t")

    assert exc_info.value.exit_code == 1
    assert installed == []
    assert schema_calls == []


def test_unknown_target_exits_without_traceback(installed: list[str]) -> None:
    with pytest.raises(typer.Exit) as exc_info:
        cli_apps.install("scope_missing")

    assert exc_info.value.exit_code == 1
    assert installed == []
