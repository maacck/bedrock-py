"""``bedrock-cli gen`` — generic template-based code generation from SQLAlchemy models."""

from __future__ import annotations

import importlib
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import typer
from jinja2 import TemplateNotFound, TemplateSyntaxError

from bedrock_cli import console
from bedrock_cli.scaffolding import (
    RenderedFile,
    ScaffoldOverwriteError,
    normalize_module_identifier,
    render_files,
    validate_template_name,
)
from bedrock_cli.template_env import build_template_environment

_COLUMN_TYPE_MAP: dict[str, str] = {
    "INTEGER": "int",
    "INT": "int",
    "BIGINT": "int",
    "SMALLINT": "int",
    "FLOAT": "float",
    "DOUBLE": "float",
    "REAL": "float",
    "NUMERIC": "float",
    "DECIMAL": "Decimal",
    "VARCHAR": "str",
    "STRING": "str",
    "TEXT": "str",
    "CHAR": "str",
    "CLOB": "str",
    "BOOLEAN": "bool",
    "BOOL": "bool",
    "DATETIME": "datetime",
    "DATE": "datetime",
    "TIMESTAMP": "datetime",
    "JSON": "dict",
    "JSONB": "dict",
}

# Template name -> built-in template path (relative to loader root)
_BUILTIN_TEMPLATES: dict[str, str] = {
    "entity": "crud/entities.py.j2",
    "service": "crud/service.py.j2",
}


def _slugify(name: str) -> str:
    return normalize_module_identifier(name)


def _sqlalchemy_type_to_python(column: Any) -> str:
    type_name = type(column.type).__name__.upper()
    return _COLUMN_TYPE_MAP.get(type_name, "Any")


def _resolve_and_load(module_path: str, class_name: str) -> type:
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        console.error(f"Cannot import module '{module_path}': {exc}")
        raise typer.Exit(code=1) from exc

    try:
        cls = getattr(mod, class_name)
    except AttributeError as exc:
        console.error(f"Class '{class_name}' not found in '{module_path}'")
        raise typer.Exit(code=1) from exc

    return cls


def _introspect_columns(model_cls: type) -> list[dict[str, Any]]:
    if not hasattr(model_cls, "__table__"):
        console.error(f"'{model_cls.__name__}' does not appear to be a SQLAlchemy ORM model (missing __table__).")
        raise typer.Exit(code=1)

    columns = []
    for col in model_cls.__table__.columns:
        python_type = _sqlalchemy_type_to_python(col)
        columns.append(
            {
                "name": col.name,
                "python_type": python_type,
                "nullable": col.nullable,
                "primary_key": col.primary_key,
                "needs_datetime_import": python_type == "datetime",
                "needs_decimal_import": python_type == "Decimal",
            }
        )
    return columns


def _parse_model_ref(model_ref: str) -> tuple[str, str]:
    if ":" not in model_ref:
        console.error(f"model_ref must be in '<module.path>:<ClassName>' format, got: {model_ref!r}")
        raise typer.Exit(code=1)

    module_path, class_name = model_ref.rsplit(":", 1)
    return module_path, class_name


def _ensure_importable() -> None:
    cwd = str(Path(".").resolve())
    src_dir = str((Path(".") / "src").resolve())
    for path_entry in (src_dir, cwd):
        if path_entry not in sys.path:
            sys.path.insert(0, path_entry)


def _build_context(model_ref: str, module_slug: str) -> dict[str, Any]:
    _ensure_importable()

    module_path, class_name = _parse_model_ref(model_ref)

    spec = find_spec(module_path.rsplit(".", 1)[0] if "." in module_path else module_path)
    if spec is None:
        console.error(
            f"Cannot find package for '{module_path}'. "
            "Make sure the package is installed or available in the current directory."
        )
        raise typer.Exit(code=1)

    model_cls = _resolve_and_load(module_path, class_name)
    columns = _introspect_columns(model_cls)

    needs_datetime = any(c["needs_datetime_import"] for c in columns)
    needs_decimal = any(c["needs_decimal_import"] for c in columns)
    needs_any = any(c["python_type"] == "Any" for c in columns)

    data_columns = [c for c in columns if not c["primary_key"]]
    pk_columns = [c for c in columns if c["primary_key"]]

    return {
        "model_name": model_cls.__name__,
        "model_slug": module_slug,
        "module_name": module_slug,
        "columns": columns,
        "data_columns": data_columns,
        "pk_columns": pk_columns,
        "needs_datetime": needs_datetime,
        "needs_any": needs_any,
        "needs_decimal": needs_decimal,
    }


def _resolve_template_name(template: str) -> str:
    """Resolve a user-facing template name to a Jinja2 template path.

    Resolution order:
        1. ``<template>.py.j2`` (works for both _bedrock_gen/ overrides and custom templates)
        2. Built-in mapping (e.g. "entity" -> "crud/entities.py.j2")
    """
    env = build_template_environment()

    # Try direct filename first (enables user-defined templates like "router.py.j2")
    direct_name = f"{template}.py.j2"
    try:
        env.get_template(direct_name)
        return direct_name
    except TemplateNotFound:
        pass
    except TemplateSyntaxError as exc:
        console.error(f"Template '{direct_name}' contains invalid Jinja syntax: {exc}")
        raise typer.Exit(code=1) from exc

    # Fall back to built-in mapping
    if template in _BUILTIN_TEMPLATES:
        return _BUILTIN_TEMPLATES[template]

    console.error(
        f"Template '{template}' not found. "
        f"Available built-in templates: {', '.join(sorted(_BUILTIN_TEMPLATES.keys()))}. "
        f"Or place a custom '{direct_name}' in your _bedrock_gen/ directory."
    )
    raise typer.Exit(code=1)


def gen(
    template: str = typer.Argument(..., help="Template name (e.g. entity, service, or a custom template)."),
    model_ref: str = typer.Argument(
        ..., help="Model reference in '<module.path>:<ClassName>' format (e.g. myapp.models:User)."
    ),
    path: Path = typer.Argument(..., help="Output directory where the generated file will be written."),
    name: str | None = typer.Option(None, "-n", help="Override module slug (default: derived from class name)."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files."),
) -> None:
    """Generate a file from a template using SQLAlchemy model introspection.

    Built-in templates: entity, service.
    Custom templates: place <name>.py.j2 in _bedrock_gen/ at your project root.
    """
    try:
        template = validate_template_name(template)
        _, class_name = _parse_model_ref(model_ref)
        module_slug = _slugify(name) if name else _slugify(class_name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    template_path = _resolve_template_name(template)

    console.info(f"Generating [bold]{template}[/bold] for {class_name} in {path}")

    context = _build_context(model_ref, module_slug)

    output_filename = f"{template}.py"
    files: list[RenderedFile] = [
        RenderedFile(output_filename, template_path, context),
    ]

    try:
        written = render_files(files, path, overwrite=overwrite)
    except ScaffoldOverwriteError as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    for written_path in written:
        console.success(str(written_path))
