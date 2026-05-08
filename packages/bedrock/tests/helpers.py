"""Test helpers for creating fake Bedrock modules on disk."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def make_fake_module(
    root: Path,
    name: str,
    manifest: dict[str, Any] | None = None,
    bootstrap: str | None = None,
    models: str | None = None,
) -> str:
    """Create a fake importable package on disk and return its import path.

    Args:
        root: Directory already present on ``sys.path``.
        name: Package name (no dots).
        manifest: YAML-serialisable dict for ``manifest.yaml``.  When *None*
            no manifest file is written.
        bootstrap: Python source to write into ``bootstrap.py``.
        models: Python source to write into ``models.py``.

    Returns:
        The import path, e.g. ``"my_mod"``.
    """
    pkg_dir = root / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    if manifest is not None:
        (pkg_dir / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    if bootstrap is not None:
        (pkg_dir / "bootstrap.py").write_text(bootstrap, encoding="utf-8")

    if models is not None:
        (pkg_dir / "models.py").write_text(models, encoding="utf-8")

    return name


def make_fake_module_no_manifest(root: Path, name: str) -> str:
    """Create a fake package *without* a manifest file."""
    pkg_dir = root / name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    return name


def clear_import_cache(name: str) -> None:
    """Remove *name* and submodules from ``sys.modules`` so they can be re-imported."""
    to_remove = [key for key in sys.modules if key == name or key.startswith(f"{name}.")]
    for key in to_remove:
        del sys.modules[key]
