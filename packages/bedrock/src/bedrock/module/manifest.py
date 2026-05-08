"""Manifest loading for the Bedrock module system."""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType

import yaml
from pydantic import ValidationError

from .entities import AppConfig, ModuleManifest
from .exc import InvalidManifestError


def load_manifest(import_path: str) -> ModuleManifest:
    """Load and validate a module's ``manifest.yaml`` from its package directory.

    The manifest file is resolved relative to the package's ``__file__`` location,
    so it must live at the root of the package directory.

    Args:
        import_path: Python import path of the module, e.g. ``"bedrock_app"``.

    Returns:
        Parsed and validated manifest model.

    Raises:
        InvalidManifestError: If the manifest is missing, unreadable, or invalid.
    """
    spec = find_spec(import_path)
    if spec is None or spec.origin is None:
        raise InvalidManifestError(f"Cannot locate package for import path '{import_path}'.")

    package_dir = Path(spec.origin).parent
    manifest_path = package_dir / "manifest.yaml"

    if not manifest_path.exists():
        raise InvalidManifestError(f"Missing manifest.yaml in package '{import_path}' at '{package_dir}'.")

    try:
        raw_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InvalidManifestError(f"Failed to read manifest for '{import_path}': {exc}") from exc
    except yaml.YAMLError as exc:
        raise InvalidManifestError(f"Failed to parse manifest for '{import_path}': {exc}") from exc

    if not isinstance(raw_data, dict):
        raise InvalidManifestError(f"Manifest for '{import_path}' must contain a YAML mapping.")

    try:
        return ModuleManifest.model_validate(raw_data)
    except ValidationError as exc:
        raise InvalidManifestError(f"Manifest for '{import_path}' is invalid: {exc}") from exc


def load_bootstrap(import_path: str) -> ModuleType | None:
    """Import the ``bootstrap`` submodule of a package if it exists.

    Args:
        import_path: Python import path of the parent module, e.g. ``"bedrock_app"``.

    Returns:
        The imported bootstrap module, or ``None`` if the submodule does not exist.
    """
    bootstrap_path = f"{import_path}.bootstrap"
    if find_spec(bootstrap_path) is None:
        return None
    return import_module(bootstrap_path)


def load_models(import_path: str) -> ModuleType | None:
    """Import the ``models`` submodule of a package if it exists.

    SQLAlchemy requires model classes to be imported before the mapper can
    resolve relationships. This function ensures that happens as part of
    module installation.

    Args:
        import_path: Python import path of the parent module, e.g. ``"bedrock_app"``.

    Returns:
        The imported models module, or ``None`` if the submodule does not exist.
    """
    models_path = f"{import_path}.models"
    if find_spec(models_path) is None:
        return None
    return import_module(models_path)


def build_app_config(import_path: str) -> AppConfig:
    """Import a module by its import path and build its ``AppConfig``.

    Args:
        import_path: Python import path of the module, e.g. ``"bedrock_app"``.

    Returns:
        Fully constructed app config ready for registry installation.

    Raises:
        InvalidManifestError: If the manifest is missing or invalid.
    """
    spec = find_spec(import_path)
    if spec is None or spec.origin is None:
        raise InvalidManifestError(f"Cannot locate package for import path '{import_path}'.")

    package_module = import_module(import_path)
    manifest = load_manifest(import_path)
    bootstrap_module = load_bootstrap(import_path)
    models_module = load_models(import_path)

    return AppConfig(
        name=import_path,
        manifest=manifest,
        package_dir=Path(spec.origin).parent,
        package_module=package_module,
        bootstrap_module=bootstrap_module,
        models_module=models_module,
    )


__all__ = [
    "build_app_config",
    "load_bootstrap",
    "load_manifest",
    "load_models",
]
