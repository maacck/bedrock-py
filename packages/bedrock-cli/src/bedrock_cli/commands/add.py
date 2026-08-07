"""``bedrock-cli add`` sub-commands for scaffolding submodules and domain modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from bedrock_cli import console
from bedrock_cli.scaffolding import (
    RenderedFile,
    ScaffoldExistsError,
    ScaffoldOverwriteError,
    normalize_module_identifier,
    render_files,
)

app = typer.Typer(help="Add submodules and domain modules to a Bedrock project.")


def _slugify(name: str) -> str:
    return normalize_module_identifier(name)


@app.command("submodule")
def add_submodule(
    name: str = typer.Argument(..., help="Module name in snake_case (e.g. inventory)."),
    path: Path = typer.Argument(..., help="Target directory where the submodule will be created."),
    with_bootstrap: bool = typer.Option(
        None,
        "--bootstrap/--no-bootstrap",
        help="Include bootstrap.py lifecycle hooks.",
        show_default=False,
    ),
    with_installation: bool = typer.Option(
        None,
        "--installation/--no-installation",
        help="Include installation.py installation hooks.",
        show_default=False,
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files."),
) -> None:
    """Generate a new Bedrock submodule skeleton."""
    try:
        module_slug = _slugify(name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    destination = path / module_slug

    if with_bootstrap is None:
        with_bootstrap = typer.confirm("Include bootstrap.py (lifecycle hooks)?", default=False)
    if with_installation is None:
        with_installation = typer.confirm("Include installation.py (installation hooks)?", default=False)

    console.info(f"Generating submodule [bold]{module_slug}[/bold] in {destination}")

    context: dict[str, Any] = {
        "module_name": module_slug,
        "version": "0.1.0",
    }

    files: list[RenderedFile] = [
        RenderedFile("__init__.py", "module/__init__.py.j2", context),
        RenderedFile("manifest.yaml", "module/manifest.yaml.j2", context),
        RenderedFile("models.py", "module/models.py.j2", context),
        RenderedFile("entities.py", "module/entities.py.j2", context),
        RenderedFile("exc.py", "module/exceptions.py.j2", context),
    ]

    if with_bootstrap:
        files.append(RenderedFile("bootstrap.py", "module/bootstrap.py.j2", context))

    if with_installation:
        files.append(RenderedFile("installation.py", "module/installation.py.j2", context))

    try:
        written = render_files(files, destination, overwrite=overwrite)
    except (ScaffoldExistsError, ScaffoldOverwriteError) as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    for written_path in written:
        console.success(str(written_path))


@app.command("domain")
def add_domain(
    name: str = typer.Argument(..., help="Domain module name in snake_case (e.g. user, sales_order)."),
    path: Path = typer.Argument(..., help="Target directory where the domain module will be created."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files."),
) -> None:
    """Generate a blank domain module skeleton (entities + service stubs)."""
    try:
        module_slug = _slugify(name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    destination = path / module_slug

    console.info(f"Generating domain module [bold]{module_slug}[/bold] in {destination}")

    context: dict[str, Any] = {
        "module_name": module_slug,
        "version": "0.1.0",
    }

    files: list[RenderedFile] = [
        RenderedFile("__init__.py", "module/__init__.py.j2", context),
        RenderedFile("entities.py", "module/entities.py.j2", context),
        RenderedFile("service.py", "domain/service.py.j2", context),
        RenderedFile("exc.py", "module/exceptions.py.j2", context),
    ]

    try:
        written = render_files(files, destination, overwrite=overwrite)
    except (ScaffoldExistsError, ScaffoldOverwriteError) as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    for written_path in written:
        console.success(str(written_path))
