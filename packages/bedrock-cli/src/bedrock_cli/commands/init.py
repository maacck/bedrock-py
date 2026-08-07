"""``bedrock-cli init`` command for scaffolding new projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from bedrock_cli import console
from bedrock_cli.scaffolding import RenderedFile, ScaffoldExistsError, normalize_module_identifier, render_files


def _slugify(name: str) -> str:
    """Convert a project name to a valid Python package identifier.

    Args:
        name: Raw project/workspace name.

    Returns:
        Lowercased, hyphen/space-replaced-with-underscore identifier.
    """
    return normalize_module_identifier(name)


def init(
    name: str = typer.Argument(..., help="Project name (used as directory and package name)."),
    output_dir: Path = typer.Option(
        Path("."),
        "--output-dir",
        "-o",
        help="Parent directory where the project will be created.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files."),
) -> None:
    """Scaffold a new Bedrock project.

    Creates a new project directory at OUTPUT_DIR/NAME with a standard
    Bedrock layout: pyproject.toml, .python-version, README.md, and a
    source package with manifest, models, bootstrap, installation, and exc.
    """
    try:
        package_base = _slugify(name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    destination = output_dir / name

    console.info(f"Creating project [bold]{name}[/bold] at {destination}")

    project_context: dict[str, Any] = {
        "module_name": package_base,
        "package": package_base,
        "version": "0.1.0",
    }
    files: list[RenderedFile] = [
        RenderedFile(
            relative_path="pyproject.toml",
            template_name="workspace/pyproject.toml.j2",
            context={"project_name": name, "package_base": package_base},
        ),
        RenderedFile(
            relative_path=".python-version",
            template_name="workspace/python-version.j2",
            context={},
        ),
        RenderedFile(
            relative_path="README.md",
            template_name="workspace/README.md.j2",
            context={"project_name": name, "package_base": package_base},
        ),
        RenderedFile(
            relative_path=f"src/{package_base}/__init__.py",
            template_name="workspace/app_init.py.j2",
            context={"project_name": name},
        ),
        RenderedFile(f"src/{package_base}/manifest.yaml", "module/manifest.yaml.j2", project_context),
        RenderedFile(f"src/{package_base}/models.py", "module/models.py.j2", project_context),
        RenderedFile(f"src/{package_base}/bootstrap.py", "module/bootstrap.py.j2", project_context),
        RenderedFile(f"src/{package_base}/installation.py", "module/installation.py.j2", project_context),
        RenderedFile(f"src/{package_base}/exc.py", "module/exceptions.py.j2", project_context),
    ]

    try:
        written = render_files(files, destination, overwrite=overwrite)
    except ScaffoldExistsError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    for path in written:
        console.success(str(path.relative_to(output_dir)))

    console.info("")
    console.info("Next steps:")
    console.info(f"  cd {name}")
    console.info("  uv sync")
