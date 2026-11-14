"""Alembic environment for Bedrock's shared metadata and per-app migrations.

``MigrationsManager.revision`` supplies ``bedrock_table_owners`` and
``current_app`` in Config.attributes. Both reflected objects and metadata
objects are filtered by that snapshot. Historical scripts still execute their
own operations; these filters are not a migration execution sandbox.

Raw Alembic calls without ``bedrock_managed`` retain the full metadata view.
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from bedrock.database.base import BedrockModel
from bedrock.database.migrations_manager import MigrationError, _normalize_table_owners
from sqlalchemy import Table, engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = BedrockModel.metadata


def _configure(**kwargs: Any) -> None:
    """Configure online or offline migrations with the same ownership rules.

    Args:
        **kwargs: Alembic connection or offline URL configuration.

    Raises:
        MigrationError: If Bedrock autogeneration has no valid ownership scope.
    """
    attrs = config.attributes
    managed = attrs.get("bedrock_managed", False)
    owners = attrs.get("bedrock_table_owners") if managed else None
    app = attrs.get("current_app")
    options: dict[str, Any] = {"target_metadata": target_metadata}
    if owners is not None:
        registry = attrs.get("registry")
        if not app or registry is None:
            raise MigrationError("Bedrock autogeneration requires a target app and its populated registry.")
        try:
            registry.get(app)
        except KeyError as exc:
            raise MigrationError(f"Migration target '{app}' is not loaded.", app) from exc
        connection = kwargs.get("connection")
        default_schema = connection.dialect.default_schema_name if connection is not None else None
        normalized = _normalize_table_owners(owners, default_schema)
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

        options.update(include_name=include_name, include_object=include_object, include_schemas=True)

    context.configure(**kwargs, **options)
    revision = context.get_context().opts.get("revision_context")
    if managed and revision is not None and revision.command_args.get("autogenerate") and owners is None:
        raise MigrationError("Bedrock autogeneration requires a resolved table ownership snapshot.", app)


def run_migrations_offline() -> None:
    """Render historical migration SQL without opening a database connection."""
    _configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using one connection, releasing the engine on every exit."""
    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with engine.connect() as connection:
            _configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
