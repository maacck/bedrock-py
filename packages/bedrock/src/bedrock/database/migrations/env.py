"""Alembic environment script for Bedrock-managed migrations.

When invoked by the Bedrock :class:`~bedrock.database.migrations_manager.MigrationsManager`,
this script receives per-app context via ``context.config.attributes``:

``current_app``
    Import path of the app being migrated (e.g. ``"myerp.wms"``), or
    ``None`` when a global operation (heads, current) is requested.

``registry``
    A fully populated :class:`~bedrock.module.registry.ModuleRegistry`
    instance.  Used to resolve the set of SQLAlchemy table names that belong
    to the target app, so that ``autogenerate`` never touches foreign tables.

``bedrock_managed``
    Sentinel flag (always ``True``) indicating this call came from the
    Bedrock CLI rather than a raw ``alembic`` invocation.

When called directly via the ``alembic`` CLI (without the sentinel), the
script falls back to the full shared metadata so that manual inspections still
work.
"""

import types
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import engine_from_config, pool

if TYPE_CHECKING:
    from bedrock.module.registry import ModuleRegistry

# ---------------------------------------------------------------------------
# Standard Alembic logging setup
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Resolve target metadata and optional per-app table filter
# ---------------------------------------------------------------------------

# Import BedrockModel lazily to avoid circular imports at module load time.
from bedrock.database.base import BedrockModel  # noqa: E402

target_metadata = BedrockModel.metadata


def _collect_app_tables(models_module: types.ModuleType) -> frozenset[str]:
    """Collect all ``__tablename__`` values declared in *models_module*.

    Args:
        models_module: The imported ``models`` submodule of a Bedrock app.

    Returns:
        A frozen set of table name strings owned by that app.
    """
    table_names: set[str] = set()
    for obj in vars(models_module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, BedrockModel)
            and obj is not BedrockModel
            and hasattr(obj, "__tablename__")
        ):
            table_names.add(obj.__tablename__)
    return frozenset(table_names)


def _build_include_name(app_tables: frozenset[str]):
    """Return an ``include_name`` callback restricted to *app_tables*.

    The callback is passed to :func:`alembic.context.configure` and ensures
    that ``autogenerate`` only considers tables belonging to the current app.

    Args:
        app_tables: Set of table names owned by the current app.

    Returns:
        A callable compatible with the Alembic ``include_name`` hook.
    """

    def include_name(name: str, type_: str, parent_names: dict) -> bool:
        """Filter schema objects to only those owned by the current app.

        Args:
            name: Object name (table name, schema name, etc.).
            type_: Kind of object (``"table"``, ``"schema"``, etc.).
            parent_names: Contextual parent name mapping from Alembic.

        Returns:
            ``True`` if the object should be included in autogenerate.
        """
        if type_ == "table":
            return name in app_tables
        return True

    return include_name


# Determine per-app context injected by MigrationsManager, if present.
_attrs = config.attributes
_is_bedrock = _attrs.get("bedrock_managed", False)
_current_app: str | None = _attrs.get("current_app")
_registry: ModuleRegistry | None = _attrs.get("registry")

_include_name = None

if _is_bedrock and _current_app is not None and _registry is not None:
    try:
        app_config = _registry.get(_current_app)
        if app_config.models_module is not None:
            _app_tables = _collect_app_tables(app_config.models_module)
            _include_name = _build_include_name(_app_tables)
    except KeyError:
        # App not found — fall back to unfiltered (should not normally happen).
        pass


# ---------------------------------------------------------------------------
# Migration runners
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in offline mode (no live DB connection required).

    Configures the Alembic context with only a URL.  The generated SQL is
    written to stdout rather than executed against the database.
    """
    url = config.get_main_option("sqlalchemy.url")

    context_kwargs: dict = {
        "url": url,
        "target_metadata": target_metadata,
        "literal_binds": True,
        "dialect_opts": {"paramstyle": "named"},
    }
    if _include_name is not None:
        context_kwargs["include_name"] = _include_name

    with context.begin_transaction():
        context.configure(**context_kwargs)
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in online mode (live DB connection).

    Creates an :class:`~sqlalchemy.engine.Engine` from the config URL and
    associates a connection with the Alembic migration context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    context_kwargs = {
        "connection": connectable.connect(),
        "target_metadata": target_metadata,
    }
    if _include_name is not None:
        context_kwargs["include_name"] = _include_name

    with connectable.connect() as connection:
        context_kwargs["connection"] = connection
        context.configure(**context_kwargs)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
