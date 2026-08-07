"""Template rendering utilities for scaffolding project files.

Provides data structures and functions to render Jinja2 templates
to target file paths with protection against accidental overwrites.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
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


class ScaffoldPathError(ValueError):
    """Raised when a requested scaffold path escapes its destination."""


_MODULE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_SAFE_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def normalize_module_identifier(name: str) -> str:
    """Normalize a user-visible name into an importable Python module name."""
    if not name or any(character in name for character in ("/", "\\", "\x00")) or ".." in name:
        raise ValueError("must not contain path separators, NUL bytes, or '..'")
    if any(character.isspace() and character != " " for character in name):
        raise ValueError("must not contain control characters or line breaks")
    if not re.fullmatch(r"[A-Za-z0-9 _-]+", name):
        raise ValueError("may only contain letters, digits, spaces, hyphens, and underscores")

    identifier = name.lower().replace("-", "_").replace(" ", "_")
    if not _MODULE_IDENTIFIER.fullmatch(identifier) or keyword.iskeyword(identifier):
        raise ValueError("must normalize to a non-keyword Python identifier")
    return identifier


def validate_template_name(name: str) -> str:
    """Validate a template stem used for both lookup and generated file names."""
    if not _SAFE_TEMPLATE_NAME.fullmatch(name):
        raise ValueError("must contain only letters, digits, hyphens, and underscores")
    return name


def _safe_target_path(destination: Path, relative_path: str) -> Path:
    """Return a target confined to ``destination`` after resolving symlinks."""
    windows_path = PureWindowsPath(relative_path)
    path = Path(relative_path)
    if (
        not relative_path
        or path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or "\\" in relative_path
        or ".." in path.parts
    ):
        raise ScaffoldPathError(f"Refusing unsafe output path: {relative_path!r}")

    root = destination.resolve(strict=False)
    target = (root / path).resolve(strict=False)
    if not target.is_relative_to(root):
        raise ScaffoldPathError(f"Refusing output path outside destination: {relative_path!r}")
    return target


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
        ScaffoldPathError: If a rendered path would escape ``destination``.
    """
    environment = build_template_environment()
    written_paths: list[Path] = []
    targets = [(rendered_file, _safe_target_path(destination, rendered_file.relative_path)) for rendered_file in files]

    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise ScaffoldExistsError(f"Destination already exists and is not empty: {destination}")

    for rendered_file, target_path in targets:
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
