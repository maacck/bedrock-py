"""Write one new migration script for a module.

``write_revision`` numbers the script per module (``0001_initial`` style), links it to the
module's single head, records cross-module dependencies, and renders the body either from
an autogenerate comparison (a connection is given) or as an empty ``pass`` skeleton.

Every check and the full source rendering finish before the file is created, and the file is
opened exclusively, so a failure never leaves a partial script or overwrites an existing one.

This file must not use ``from __future__ import annotations``.
"""

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy
from alembic.autogenerate import produce_migrations, render_python_code
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Table
from sqlalchemy.engine import Connection
from sqlalchemy.types import TypeEngine

from .graph import _MAX_REVISION_LENGTH, MigrationError, MigrationGraph, MigrationKey
from .ledger import LEGACY_VERSION_TABLE, STAGING_TABLE_NAME, VERSION_TABLE_NAME

if TYPE_CHECKING:
    from ...module.entities import AppConfig

_NUMBER = re.compile(r"^([0-9]{4,})_")
_DIALECT = re.compile(r"sqlalchemy\.dialects\.(\w+)")

_SCRIPT_TEMPLATE = '''"""{message}

Revision ID: {revision}
Revises: {revises}
Create Date: {create_date}

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
{imports}

revision: str = {revision!r}
down_revision: str | None = {down_revision!r}
depends_on: Sequence[str] = {depends_on!r}


def upgrade() -> None:
    """Upgrade schema."""
    {upgrades}


def downgrade() -> None:
    """Downgrade schema."""
    {downgrades}
'''


def _normalize_table_owners(owners: dict[str, str], default_schema: str | None) -> dict[tuple[str | None, str], str]:
    """Resolve metadata keys to unique physical table identities for a connection.

    Args:
        owners: Validated metadata table keys and their owning app paths.
        default_schema: The connection's default schema, or None offline.

    Returns:
        Owners keyed by normalized schema and table name.

    Raises:
        MigrationError: If metadata changed or two declarations name one physical table.
    """
    from ..base import BedrockModel

    normalized: dict[tuple[str | None, str], str] = {}
    for key, owner in owners.items():
        table = BedrockModel.metadata.tables.get(key)
        if table is None:
            raise MigrationError(f"Table '{key}' disappeared after resolving ownership; retry with stable models.")
        identity = (None if table.schema == default_schema else table.schema, table.name)
        if identity in normalized:
            raise MigrationError(
                f"Table '{key}' has duplicate declarations for the default schema; "
                "use one Table declaration per physical table.",
                owner,
            )
        normalized[identity] = owner
    return normalized


def _ownership_options(owners: dict[str, str], app: str, connection: Connection, version_table: str) -> dict[str, Any]:
    """Alembic options limiting reflection and metadata comparison to *app*'s tables."""
    default_schema = connection.dialect.default_schema_name
    normalized = _normalize_table_owners(owners, default_schema)
    reserved = {VERSION_TABLE_NAME, STAGING_TABLE_NAME, LEGACY_VERSION_TABLE, version_table, version_table + "_adopt"}
    for schema, name in normalized:
        if schema is None and name.lower() in reserved:
            raise MigrationError(f"Table '{name}' is reserved for migration state.", app)
    tables = {identity for identity, owner in normalized.items() if owner == app}
    schemas = {schema for schema, _ in tables}

    def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
        """Limit database reflection to the target's schemas and tables."""
        if type_ == "schema":
            return (None if name == default_schema else name) in schemas
        if type_ == "table":
            schema = parent_names.get("schema_name")
            return (None if schema == default_schema else schema, name) in tables
        return True

    def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
        """Filter objects by their owning table, not a foreign key's target."""
        table = obj if isinstance(obj, Table) else obj.table
        schema = None if table.schema == default_schema else table.schema
        return (schema, table.name) in tables

    return {"include_name": include_name, "include_object": include_object, "include_schemas": True}


def _type_prefixes(type_: TypeEngine[Any], imports: set[str]) -> dict[str, str]:
    """Map the class name of *type_* and its ARRAY item types to the module prefix that imports it."""
    prefixes: dict[str, str] = {}
    current: Any = type_
    while isinstance(current, TypeEngine):
        if getattr(current, "_variant_mapping", None):
            raise MigrationError("Cannot render a type with dialect variants; write this migration by hand.")
        module = type(current).__module__
        match = _DIALECT.match(module)
        if match:
            imports.add(f"from sqlalchemy.dialects import {match.group(1)}")
            prefixes[type(current).__name__] = match.group(1)
        elif module.startswith("sqlalchemy."):
            prefixes[type(current).__name__] = "sa"
        else:
            raise MigrationError(
                f"Cannot render column type {type(current).__module__}.{type(current).__name__} "
                "in a generated migration; write this migration by hand."
            )
        current = getattr(current, "item_type", None)
    return prefixes


def _render_type(type_: TypeEngine[Any], imports: set[str]) -> str:
    """Render *type_* with every class qualified by a module that *imports* (or the template) provides.

    Alembic's own rendering emits bare nested names such as ``JSONB(astext_type=Text())``, which fail when
    the script runs in a fresh namespace.
    """
    prefixes = _type_prefixes(type_, imports)

    def qualify(node: ast.expr) -> ast.expr:
        if isinstance(node, ast.Constant):
            return node
        if isinstance(node, (ast.Tuple, ast.List)):
            node.elts = [qualify(elt) for elt in node.elts]
            return node
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            prefix = prefixes.get(name)
            if prefix is None:
                if not isinstance(getattr(sqlalchemy, name, None), type):
                    raise MigrationError(
                        f"Cannot render type argument '{name}' of {type_!r} in a generated migration; "
                        "write this migration by hand."
                    )
                prefix = "sa"
            node.func = ast.Attribute(ast.Name(prefix, ast.Load()), name, ast.Load())
            node.args = [qualify(arg) for arg in node.args]
            for keyword in node.keywords:
                keyword.value = qualify(keyword.value)
            return node
        raise MigrationError(f"Cannot render {type_!r} in a generated migration; write this migration by hand.")

    return ast.unparse(qualify(ast.parse(repr(type_), mode="eval").body))


def _render_item_hook(imports: set[str]) -> Any:
    """Build Alembic's ``render_item`` hook, recording dialect imports in *imports*.

    shortcut: only dialect types and ARRAY go through our renderer; other sqlalchemy types use
    Alembic's default, and user-defined types are rejected. Upgrade when a project needs custom types.
    """

    def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
        if type_ != "type":
            return False
        module = type(obj).__module__
        if _DIALECT.match(module) or isinstance(obj, sqlalchemy.ARRAY):
            return _render_type(obj, imports)
        if module.startswith("sqlalchemy.") and not getattr(obj, "_variant_mapping", None):
            return False
        return _render_type(obj, imports)  # raises for user-defined types and variants

    return render_item


def _slug(message: str, limit: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:limit].rstrip("_")
    return slug or "migration"


def _docstring(text: str) -> str:
    """Escape *text* so it is safe inside a triple-quoted literal."""
    out = []
    for char in text:
        if char in '\\"':
            out.append("\\" + char)
        elif char == "\n" or char.isprintable():
            out.append(char)
        else:
            out.append(f"\\x{ord(char):02x}" if ord(char) < 256 else f"\\u{ord(char):04x}")
    return "".join(out)


def _dependencies(app: "AppConfig", graph: MigrationGraph, heads: tuple[MigrationKey, ...]) -> tuple[str, ...]:
    """Heads of direct manifest dependencies that this module's history does not already cover (ADR-4)."""
    covered = graph.ancestors(heads)
    keys = {head for dep in app.manifest.depends_on for head in graph.heads(dep) if head not in covered}
    return tuple(f"{a}:{r}" for a, r in sorted(keys))


def write_revision(
    app: "AppConfig",
    graph: MigrationGraph,
    message: str,
    connection: Connection | None,
    owners: dict[str, str] | None,
    *,
    version_table: str = VERSION_TABLE_NAME,
) -> Path:
    """Create the next migration script of *app* and return its path.

    The caller has already verified the app is at head and, for autogeneration, resolved *owners*.

    Args:
        app: The module that owns the new script.
        graph: Validated graph of every loaded module's scripts.
        message: Description; becomes the docstring and the file name slug.
        connection: Open connection to compare against, or None for a manual (``pass``) script.
        owners: Metadata table key to owning app, required when *connection* is given.

    Returns:
        Path of the new ``<number>_<slug>.py`` file.

    Raises:
        MigrationError: Several heads, no ownership snapshot, an unrenderable type, or an existing file.
    """
    name = app.name
    heads = graph.heads(name)
    if len(heads) > 1:
        raise MigrationError(
            f"'{name}' has several migration heads ({', '.join(r for _, r in heads)}); "
            "merge them by hand before generating a revision.",
            name,
        )

    numbers = [int(m.group(1)) for _, rev in graph.revisions(name) if (m := _NUMBER.match(rev))]
    number = max(numbers, default=0) + 1
    prefix = f"{number:04d}_"
    revision = prefix + _slug(message, _MAX_REVISION_LENGTH - len(prefix))
    if (name, revision) in graph:
        raise MigrationError(f"Migration '{revision}' already exists for '{name}'.", name)
    down = heads[0][1] if heads else None

    imports: set[str] = set()
    upgrades = downgrades = "pass"
    if connection is not None:
        if owners is None:
            raise MigrationError("Bedrock autogeneration requires a resolved table ownership snapshot.", name)
        from ..base import BedrockModel

        options = {
            "target_metadata": BedrockModel.metadata,
            **_ownership_options(owners, name, connection, version_table),
        }
        context = MigrationContext.configure(connection, opts=options)
        script = produce_migrations(context, BedrockModel.metadata)
        hook = _render_item_hook(imports)
        assert script.upgrade_ops is not None and script.downgrade_ops is not None
        upgrades = render_python_code(script.upgrade_ops, migration_context=context, render_item=hook)
        downgrades = render_python_code(script.downgrade_ops, migration_context=context, render_item=hook)
        # Operations such as ExcludeConstraint reference a dialect even without a dialect column type.
        body = ast.parse("def generated():\n    " + upgrades + "\n    " + downgrades)
        dialects = {"postgresql", "mysql", "mssql", "oracle", "sqlite"}
        imports.update(
            f"from sqlalchemy.dialects import {n.id}"
            for n in ast.walk(body)
            if isinstance(n, ast.Name) and n.id in dialects
        )

    source = _SCRIPT_TEMPLATE.format(
        message=_docstring(message.strip()),
        revision=revision,
        revises=down or "",
        create_date=datetime.now(UTC).isoformat(),
        imports="\n".join(sorted(imports)),
        down_revision=down,
        depends_on=_dependencies(app, graph, heads),
        upgrades=upgrades,
        downgrades=downgrades,
    )
    compile(source, revision, "exec")  # never write an invalid script

    directory = app.package_dir / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{revision}.py"
    try:
        with path.open("x", encoding="utf-8") as handle:
            try:
                handle.write(source)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
    except FileExistsError as exc:
        raise MigrationError(f"Migration file {path} already exists.", name) from exc
    return path
