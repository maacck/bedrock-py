"""Template rendering utilities for scaffolding project files.

Provides data structures and functions to render Jinja2 templates
to target file paths with protection against accidental overwrites.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment

from bedrock_cli.template_env import build_template_environment


@dataclass(frozen=True)
class RenderedFile:
    """Represents a single file to be rendered from a Jinja2 template.

    Attributes:
        relative_path: Target file path relative to the destination root.
        template_name: Jinja2 template identifier within the templates directory.
        context: Dictionary of variables passed to the template engine.
    """

    relative_path: str
    template_name: str
    context: dict[str, Any]


class ScaffoldExistsError(Exception):
    """Raised when attempting to scaffold into a non-empty directory without overwrite."""


class ScaffoldOverwriteError(Exception):
    """Raised when an existing file would be overwritten and overwrite is disabled."""


def render_files(
    files: list[RenderedFile],
    destination: Path,
    overwrite: bool = False,
) -> list[Path]:
    """Render a batch of template files to the destination directory.

    Args:
        files: List of RenderedFile instances to process.
        destination: Target directory where files will be written.
        overwrite: If True, allow overwriting existing files.

    Returns:
        List of absolute paths to written files.

    Raises:
        ScaffoldExistsError: If destination exists and is non-empty without overwrite.
        ScaffoldOverwriteError: If a specific target file exists without overwrite.
    """
    environment = build_template_environment()
    written_paths: list[Path] = []

    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise ScaffoldExistsError(f"Destination already exists and is not empty: {destination}")

    for rendered_file in files:
        target_path = destination / rendered_file.relative_path
        if target_path.exists() and not overwrite:
            raise ScaffoldOverwriteError(
                f"Refusing to overwrite existing file: {target_path}. Use --overwrite to replace."
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(render_template(environment, rendered_file), encoding="utf-8")
        written_paths.append(target_path)

    return written_paths


def render_template(environment: Environment, rendered_file: RenderedFile) -> str:
    """Render a single Jinja2 template with the provided context.

    Args:
        environment: Configured Jinja2 Environment instance.
        rendered_file: RenderedFile containing template name and context.

    Returns:
        The rendered template string.
    """
    template = environment.get_template(rendered_file.template_name)
    return template.render(**rendered_file.context)
