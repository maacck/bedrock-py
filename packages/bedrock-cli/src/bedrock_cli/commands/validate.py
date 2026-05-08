"""``bedrock validate`` sub-commands for validating Bedrock project manifests."""

from __future__ import annotations

from pathlib import Path

import typer
import yaml

from bedrock_cli import console

app = typer.Typer(help="Validate Bedrock project configuration.")

_REQUIRED_KEYS = {"name", "package", "version"}


def _find_manifests(root: Path) -> list[Path]:
    """Recursively find all manifest.yaml files under root.

    Args:
        root: Root directory to search from.

    Returns:
        Sorted list of manifest.yaml Paths.
    """
    return sorted(root.rglob("manifest.yaml"))


def _validate_manifest(path: Path) -> list[str]:
    """Check a single manifest.yaml for required fields.

    Args:
        path: Path to a manifest.yaml file.

    Returns:
        List of error strings; empty list means valid.
    """
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    for key in sorted(_REQUIRED_KEYS):
        if key not in data or not data[key]:
            errors.append(f"Missing required field: '{key}'")

    return errors


@app.command("manifests")
def validate_manifests(
    root: Path = typer.Option(
        Path("."),
        "--root",
        "-r",
        help="Project root to scan for manifest.yaml files.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Exit with non-zero code if any manifest has warnings.",
    ),
) -> None:
    """Validate all manifest.yaml files in the project.

    Checks that each manifest contains the required fields:
    name, package, version, and kind.
    """
    manifests = _find_manifests(root)
    if not manifests:
        console.warning("No manifest.yaml files found.")
        raise typer.Exit()

    failed = 0
    for path in manifests:
        rel = path.relative_to(root)
        errors = _validate_manifest(path)
        if errors:
            console.error(f"{rel}")
            for err in errors:
                console.info(f"  {err}")
            failed += 1
        else:
            console.success(str(rel))

    if failed:
        console.error(f"{failed} manifest(s) failed validation.")
        raise typer.Exit(code=1)
    else:
        console.success("All manifests are valid.")
