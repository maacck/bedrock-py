"""``bedrock make`` sub-commands for generating modules and CRUD code."""

from __future__ import annotations

import importlib
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import typer

from bedrock_cli import console
from bedrock_cli.scaffolding import RenderedFile, ScaffoldExistsError, ScaffoldOverwriteError, render_files

app = typer.Typer(help="Generate modules and domain code.")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

_DATETIME_TYPES = {"DATETIME", "DATE", "TIMESTAMP"}


def _slugify(name: str) -> str:
    """Convert a name to a snake_case package identifier.

    Args:
        name: Raw name string.

    Returns:
        Lowercased underscore-separated identifier.
    """
    return name.lower().replace("-", "_").replace(" ", "_")


def _to_camel(value: str) -> str:
    """Convert snake_case or kebab-case to PascalCase.

    Args:
        value: Input string.

    Returns:
        PascalCase representation.
    """
    return "".join(p.capitalize() for p in value.replace("-", "_").split("_") if p)


def _sqlalchemy_type_to_python(column: Any) -> str:
    """Derive a Python type annotation string from a SQLAlchemy column.

    Args:
        column: A SQLAlchemy ``Column`` object.

    Returns:
        Python type name as a string (e.g. ``"int"``, ``"str"``).
    """
    type_name = type(column.type).__name__.upper()
    return _COLUMN_TYPE_MAP.get(type_name, "Any")


def _parse_model_ref(package: str, model_ref: str) -> tuple[str, str, str]:
    """Parse and validate a model reference string.

    Args:
        package: Top-level package name acting as the import root.
        model_ref: ``"<file>:<ClassName>"`` string, e.g. ``"models:User"``.

    Returns:
        Tuple of ``(package, module_file, class_name)``.

    Raises:
        typer.Exit: If model_ref is not in the expected format.
    """
    if ":" not in model_ref:
        console.error(f"model_ref must be in '<module>:<ClassName>' format, got: {model_ref!r}")
        raise typer.Exit(code=1)

    module_file, class_name = model_ref.split(":", 1)
    return package, module_file, class_name


def _resolve_and_load(module_path: str, module_file: str, class_name: str) -> type:
    """Import and return a class from a fully qualified dotted path.

    Args:
        package: Root package name.
        module_path: Dotted sub-path relative to package (e.g. ``modules.inventory``).
        module_file: File/module name within module_path (e.g. ``models``).
        class_name: Class name to retrieve.

    Returns:
        The resolved class.

    Raises:
        typer.Exit: On import or attribute resolution failure.
    """
    full_module = f"{module_path}.{module_file}"
    try:
        mod = importlib.import_module(full_module)
    except ModuleNotFoundError as exc:
        console.error(f"Cannot import module '{full_module}': {exc}")
        raise typer.Exit(code=1) from exc

    try:
        cls = getattr(mod, class_name)
    except AttributeError as exc:
        console.error(f"Class '{class_name}' not found in '{full_module}'")
        raise typer.Exit(code=1) from exc

    return cls


def _introspect_columns(model_cls: type) -> list[dict[str, Any]]:
    """Extract column metadata from a SQLAlchemy ORM model class.

    Args:
        model_cls: A SQLAlchemy declarative model class.

    Returns:
        List of dicts with keys ``name``, ``python_type``, ``nullable``,
        ``primary_key``, and ``needs_datetime_import``.

    Raises:
        typer.Exit: If the class has no ``__table__`` attribute.
    """
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


# ---------------------------------------------------------------------------
# ``make module`` command
# ---------------------------------------------------------------------------


@app.command("module")
def make_module(
    name: str = typer.Argument(..., help="Module name in snake_case (e.g. inventory)."),
    modules_dir: Path = typer.Option(
        None,
        "--modules-dir",
        "-d",
        help=(
            "Directory where the module will be created. "
            "Defaults to src/<package_base>/modules/ if a src/ tree is found."
        ),
    ),
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
    """Generate a new Bedrock module submodule skeleton.

    Creates the standard Bedrock module layout inside the modules directory:
    __init__.py, manifest.yaml, models.py, entities.py, service.py, and
    exceptions.py.  Optionally adds api.py and bootstrap.py.
    """
    module_slug = _slugify(name)

    cwd = Path(".").resolve()
    # Determine destination directory
    if modules_dir is None:
        modules_dir = cwd / "modules"
        if not modules_dir.exists():
            modules_dir = cwd

    destination = modules_dir / module_slug
    print("cwd", cwd)
    # Interactive prompts when flags are not explicitly set
    if with_bootstrap is None:
        with_bootstrap = typer.confirm("Include bootstrap.py (lifecycle hooks)?", default=False)
    if with_installation is None:
        with_installation = typer.confirm("Include installation.py (installation hooks)?", default=False)

    # Derive package dotted name from destination path
    # Walk from the *parent* since destination itself doesn't exist yet.
    # package = _infer_package(destination.parent) + f".{module_slug}"

    console.info(f"Generating module [bold]{module_slug}[/bold] in {destination}")

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

    # Ensure every ancestor directory between modules_dir and destination
    # has an __init__.py so the module is importable.
    # parent = destination.parent
    # while parent != modules_dir.parent and parent != Path("."):
    #     parent_init = parent / "__init__.py"
    #     if not parent_init.exists():
    #         parent_init.write_text("", encoding="utf-8")
    #     if parent == modules_dir:
    #         break
    #     parent = parent.parent

    for path in written:
        console.success(str(path))


def _infer_package(destination: Path) -> str:
    """Attempt to derive a dotted Python package name from a file-system path.

    Walks upward from *destination* until it finds a directory without an
    ``__init__.py``, then assembles the dotted path from that anchor.

    Args:
        destination: Proposed module directory path (need not exist yet).

    Returns:
        Best-effort dotted package string, or the directory name as fallback.
    """
    parts: list[str] = []
    current = destination
    while True:
        init = current / "__init__.py"
        if init.exists():
            parts.append(current.name)
            current = current.parent
        else:
            break
    parts.reverse()
    return ".".join(parts) if parts else destination.name


# ---------------------------------------------------------------------------
# ``make crud`` command
# ---------------------------------------------------------------------------


@app.command("crud")
def make_crud(
    package: str = typer.Argument(
        ...,
        help="Root package name used as the import anchor (e.g. myapp or myapp.submodule).",
    ),
    model_ref: str = typer.Argument(
        ...,
        help="Model import path in '<file>:<ClassName>' format (e.g. models:User).",
    ),
    name: str | None = typer.Option(
        None,
        "-n",
        help="Module name",
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Directory where entities.py and service.py will be written. Defaults to the resolved module directory.",
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files."),
) -> None:
    """Generate entities.py and service.py from an existing SQLAlchemy model.

    Introspects MODEL_REF at runtime and produces typed Pydantic entities
    (Base/Create/Read/Update) and a full service layer.

    Example:

    \\b
        bedrock make crud myapp models:User --module modules.inventory
    """
    package_str, module_file, class_name = _parse_model_ref(package, model_ref)

    # Ensure the package is importable from the current directory
    cwd = str(Path(".").resolve())
    src_dir = str((Path(".") / "src").resolve())
    for path_entry in (src_dir, cwd):
        if path_entry not in sys.path:
            sys.path.insert(0, path_entry)

    spec = find_spec(package)
    if spec is None:
        console.error(
            f"Cannot find package '{package}'. "
            "Make sure the package is installed or available in the current directory."
        )
        raise typer.Exit(code=1)

    if spec.origin is not None:
        module_path = Path(spec.origin).parent
    elif spec.submodule_search_locations:
        module_path = Path(spec.submodule_search_locations[0])
    else:
        console.error(f"Cannot determine location for package '{package}'.")
        raise typer.Exit(code=1)

    model_cls = _resolve_and_load(package_str, module_file, class_name)
    columns = _introspect_columns(model_cls)

    needs_datetime = any(c["needs_datetime_import"] for c in columns)
    needs_decimal = any(c["needs_decimal_import"] for c in columns)

    needs_any = any(c["python_type"] == "Any" for c in columns)

    # Filter out primary-key and timestamp columns for Base/Create/Update
    data_columns = [c for c in columns if not c["primary_key"]]
    pk_columns = [c for c in columns if c["primary_key"]]
    module_name = name or _slugify(class_name)

    context: dict[str, Any] = {
        "model_name": class_name,
        "model_slug": module_name,
        "module_name": module_name,
        "columns": columns,
        "data_columns": data_columns,
        "pk_columns": pk_columns,
        "needs_datetime": needs_datetime,
        "needs_any": needs_any,
        "needs_decimal": needs_decimal,
    }

    if output_dir is None:
        # Resolve to the module's source directory
        output_dir = module_path / module_name

    files: list[RenderedFile] = [
        RenderedFile("entities.py", "crud/entities.py.j2", context),
        RenderedFile("service.py", "crud/service.py.j2", context),
    ]

    try:
        written = render_files(files, output_dir, overwrite=overwrite)
    except (ScaffoldExistsError, ScaffoldOverwriteError) as exc:
        console.error(str(exc))
        raise typer.Exit(code=1) from exc

    for path in written:
        console.success(str(path))
