"""``bedrock inspect`` sub-commands for introspecting Bedrock project state."""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.table import Table
from rich.tree import Tree

from bedrock_cli import console

app = typer.Typer(help="Inspect a Bedrock project's module registry.")


def _find_manifests(root: Path) -> list[Path]:
    """Recursively find all manifest.yaml files under root.

    Args:
        root: Root directory to search from.

    Returns:
        Sorted list of manifest.yaml Paths.
    """
    return sorted(root.rglob("manifest.yaml"))


def _load_manifest(path: Path) -> dict:
    """Load a YAML manifest file.

    Args:
        path: Absolute or relative path to manifest.yaml.

    Returns:
        Parsed YAML content as a dict.
    """
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@app.command("modules")
def inspect_modules(
    root: Path = typer.Option(
        Path("."),
        "--root",
        "-r",
        help="Project root to scan for manifest.yaml files.",
    ),
) -> None:
    """List all discovered Bedrock modules with their metadata."""
    manifests = _find_manifests(root)
    if not manifests:
        console.warning("No manifest.yaml files found.")
        raise typer.Exit()

    table = Table(title="Bedrock Modules", show_lines=True)
    table.add_column("Name", style="bold cyan")
    table.add_column("Package")
    table.add_column("Version")
    table.add_column("Kind")
    table.add_column("Path", style="dim")

    for path in manifests:
        data = _load_manifest(path)
        table.add_row(
            data.get("name", "—"),
            data.get("package", "—"),
            data.get("version", "—"),
            data.get("kind", "—"),
            str(path.relative_to(root)),
        )

    console.console.print(table)


@app.command("tree")
def inspect_tree(
    root: Path = typer.Option(
        Path("."),
        "--root",
        "-r",
        help="Project root to scan for manifest.yaml files.",
    ),
) -> None:
    """Display the module hierarchy as a tree."""
    manifests = _find_manifests(root)
    if not manifests:
        console.warning("No manifest.yaml files found.")
        raise typer.Exit()

    tree = Tree(f"[bold]{root.resolve().name}[/bold]")
    for path in manifests:
        data = _load_manifest(path)
        name = data.get("name", path.parent.name)
        pkg = data.get("package", "")
        label = f"[cyan]{name}[/cyan]  [dim]{pkg}[/dim]"
        tree.add(label)

    console.console.print(tree)


@app.command("deps")
def inspect_deps(
    root: Path = typer.Option(
        Path("."),
        "--root",
        "-r",
        help="Project root to scan for manifest.yaml files.",
    ),
) -> None:
    """Show inter-module dependency relationships."""
    manifests = _find_manifests(root)
    if not manifests:
        console.warning("No manifest.yaml files found.")
        raise typer.Exit()

    data_by_name: dict[str, dict] = {}
    for path in manifests:
        data = _load_manifest(path)
        name = data.get("name")
        if name:
            data_by_name[name] = data

    table = Table(title="Module Dependencies", show_lines=True)
    table.add_column("Module", style="bold cyan")
    table.add_column("Depends On")

    for name, data in sorted(data_by_name.items()):
        deps = data.get("depends_on") or []
        dep_str = ", ".join(deps) if deps else "[dim]none[/dim]"
        table.add_row(name, dep_str)

    console.console.print(table)
