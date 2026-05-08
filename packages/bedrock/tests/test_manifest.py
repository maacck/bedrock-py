"""Unit tests for manifest loading helpers."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from bedrock.module.exc import InvalidManifestError
from bedrock.module.manifest import build_app_config, load_bootstrap, load_manifest, load_models
from pydantic import ValidationError

from tests.helpers import make_fake_module, make_fake_module_no_manifest


class TestLoadManifest:
    """Success and failure paths for ``load_manifest``."""

    def test_loads_valid_manifest(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "valid_mod",
            manifest={"title": "Valid Module", "version": "1.0.0", "depends_on": ["other_mod"]},
        )

        result = load_manifest("valid_mod")

        assert result.title == "Valid Module"
        assert result.version == "1.0.0"
        assert result.depends_on == ["other_mod"]

    def test_raises_when_package_not_found(self) -> None:
        with pytest.raises(InvalidManifestError, match="Cannot locate package"):
            load_manifest("nonexistent_package_xyz")

    def test_raises_when_manifest_missing(self, fake_package: Path) -> None:
        make_fake_module_no_manifest(fake_package, "no_manifest_mod")

        with pytest.raises(InvalidManifestError, match="Missing manifest.yaml"):
            load_manifest("no_manifest_mod")

    def test_raises_on_unreadable_manifest(self, fake_package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_fake_module(fake_package, "bad_read_mod", manifest={"title": "x", "version": "1"})

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("disk failure")

        monkeypatch.setattr(Path, "read_text", boom)

        with pytest.raises(InvalidManifestError, match="Failed to read manifest"):
            load_manifest("bad_read_mod")

    def test_raises_on_invalid_yaml(self, fake_package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_fake_module(fake_package, "bad_yaml_mod", manifest={"title": "x", "version": "1"})

        def bad_load(*_args: Any, **_kwargs: Any) -> None:
            raise yaml.YAMLError("bad yaml")

        monkeypatch.setattr(yaml, "safe_load", bad_load)

        with pytest.raises(InvalidManifestError, match="Failed to parse manifest"):
            load_manifest("bad_yaml_mod")

    def test_raises_when_yaml_is_not_mapping(self, fake_package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_fake_module(fake_package, "list_yaml_mod", manifest={"title": "x", "version": "1"})

        monkeypatch.setattr(yaml, "safe_load", lambda _s: ["not", "a", "dict"])

        with pytest.raises(InvalidManifestError, match="must contain a YAML mapping"):
            load_manifest("list_yaml_mod")

    def test_raises_on_pydantic_validation_error(self, fake_package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_fake_module(fake_package, "invalid_mod", manifest={"title": "x", "version": "1"})

        def bad_validate(*_args: Any, **_kwargs: Any) -> None:
            raise ValidationError.from_exception_data(
                "ModuleManifest", [{"type": "missing", "loc": ("title",), "input": {}}]
            )

        from bedrock.module import entities

        monkeypatch.setattr(entities.ModuleManifest, "model_validate", bad_validate)

        with pytest.raises(InvalidManifestError, match="is invalid"):
            load_manifest("invalid_mod")


class TestLoadBootstrap:
    """Importing optional ``bootstrap.py`` submodules."""

    def test_returns_module_when_bootstrap_exists(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "boot_mod",
            manifest={"title": "Boot", "version": "1"},
            bootstrap="def on_load(registry, app): pass\n",
        )

        result = load_bootstrap("boot_mod")

        assert isinstance(result, ModuleType)
        assert hasattr(result, "on_load")

    def test_returns_none_when_bootstrap_missing(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "no_boot_mod", manifest={"title": "No Boot", "version": "1"})

        result = load_bootstrap("no_boot_mod")

        assert result is None


class TestLoadModels:
    """Importing optional ``models.py`` submodules."""

    def test_returns_module_when_models_exists(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "models_mod",
            manifest={"title": "Models", "version": "1"},
            models="x = 1\n",
        )

        result = load_models("models_mod")

        assert isinstance(result, ModuleType)

    def test_returns_none_when_models_missing(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "no_models_mod", manifest={"title": "No Models", "version": "1"})

        result = load_models("no_models_mod")

        assert result is None


class TestBuildAppConfig:
    """``build_app_config`` assembles a full ``AppConfig``."""

    def test_builds_full_config(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "full_mod",
            manifest={"title": "Full", "version": "2.0.0"},
            bootstrap="def ready(r, a): pass\n",
            models="y = 2\n",
        )

        result = build_app_config("full_mod")

        assert result.name == "full_mod"
        assert result.manifest.title == "Full"
        assert result.manifest.version == "2.0.0"
        assert isinstance(result.package_module, ModuleType)
        assert isinstance(result.bootstrap_module, ModuleType)
        assert isinstance(result.models_module, ModuleType)
        assert result.package_dir == fake_package / "full_mod"

    def test_builds_minimal_config_without_optional_modules(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "minimal_mod",
            manifest={"title": "Minimal", "version": "0.1.0"},
        )

        result = build_app_config("minimal_mod")

        assert result.name == "minimal_mod"
        assert result.bootstrap_module is None
        assert result.models_module is None

    def test_raises_when_package_not_found(self) -> None:
        with pytest.raises(InvalidManifestError, match="Cannot locate package"):
            build_app_config("totally_missing_package_abc")
