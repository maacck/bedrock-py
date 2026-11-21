"""Bedrock database migration CLI commands.

Exposes Bedrock's module-scoped migration operations as a :class:`typer.Typer`
sub-app that can be mounted onto the main Bedrock CLI.  Each command takes an
app import path that serves as both the host app to load into the registry and
the target of the migration operation.

Usage::

    bedrock db revision myerp.wms -m "add order table"
    bedrock db upgrade myerp.wms
    bedrock db upgrade myerp.wms +1
    bedrock db downgrade myerp.wms -- -1
    bedrock db downgrade myerp.wms base
    bedrock db downgrade myerp.auth base --load myerp.orders --cascade
    bedrock db heads myerp.wms
    bedrock db current myerp.wms
    bedrock db check myerp.wms
    bedrock db history myerp.wms
    bedrock db adopt myerp.wms myerp.auth
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from bedrock.database import MigrationError, MigrationsManager

if TYPE_CHECKING:
    from bedrock.module.registry import ModuleRegistry

db_app = typer.Typer(
    name="db",
    rich_markup_mode="rich",
    help="Database migration commands for Bedrock modules.",
)

_console = Console()

_LOAD_HELP = (
    "Also load this module (and its dependencies) so its applied migrations are known. "
    "Repeat for several modules. Dependents are never loaded automatically."
)
_CASCADE_HELP = "Also roll back applied migrations of loaded modules that depend on the ones being removed."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def build_migrations_manager(registry: ModuleRegistry) -> MigrationsManager:
    """Build the one :class:`MigrationsManager` every migration-running CLI command uses.

    An initialized ``db`` supplies the URL together with its own settings (lock timeout and
    version table), so they cannot disagree; otherwise both come from the environment.
    """
    from bedrock.database import db
    from bedrock.database.config import DbSettings

    if db._database_url is not None:
        database_url, settings = db._database_url, db.settings
    else:
        settings = DbSettings()
        database_url = settings.SQLALCHEMY_DATABASE_URI
    return MigrationsManager(
        registry=registry,
        database_url=database_url,
        lock_timeout=settings.MIGRATION_LOCK_TIMEOUT,
        version_table=settings.MIGRATION_VERSION_TABLE,
    )


def _bootstrap_manager(*roots: str) -> MigrationsManager:
    """Load *roots* into the registry and return a :class:`MigrationsManager`.

    Args:
        *roots: Import paths to load; the first is the command's target app.
            A registry that is already ready is never extended, so every root
            must already be loaded in that case.

    Returns:
        A :class:`MigrationsManager` bound to the configured database URL.

    Raises:
        :class:`typer.Exit`: If a root is not loaded in an already-ready registry.
    """
    from bedrock.module.registry import apps

    unique = list(dict.fromkeys(roots))
    if not apps.ready:
        apps.populate(unique)
    else:
        missing = [root for root in unique if not apps.is_installed(root)]
        if missing:
            _console.print(
                f"[bold red]✗[/bold red] Not loaded in the running registry: {escape(', '.join(missing))}. "
                "Start the command in a fresh process."
            )
            raise typer.Exit(1)

    return build_migrations_manager(apps)


def _fail(exc: MigrationError) -> NoReturn:
    """Print *exc* and exit with status 1."""
    _console.print(f"[bold red]✗[/bold red] {escape(str(exc))}")
    raise typer.Exit(1) from exc


def _print_steps(steps: list[tuple[str, str]]) -> None:
    """Print executed migrations as ``app:revision`` lines, or ``Nothing to do.``."""
    if not steps:
        _console.print("Nothing to do.")
        return
    for app, revision_name in steps:
        _console.print(f"{app}:{revision_name}", markup=False, highlight=False)


def _print_revisions(revisions: tuple[str, ...]) -> None:
    """Print one revision per line, or ``-`` when there is none."""
    for name in revisions or ("-",):
        _console.print(name, markup=False, highlight=False)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@db_app.command(rich_help_panel="Write migration scripts")
def revision(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
    message: Annotated[str, typer.Option("-m", "--message", help="Short description for this revision.")],
    autogenerate: Annotated[bool, typer.Option(help="Autogenerate migration from model diff.")] = True,
) -> None:
    """Write a draft migration script for *APP_IMPORT_PATH* without updating the database.

    The revision is named ``NNNN_<slug>`` and chained after the app's current head.
    Dependencies on other modules' migrations are recorded automatically.

    Examples::

        bedrock db revision myerp.wms -m "initial schema"
        bedrock db revision myerp.wms -m "add shipment status" --no-autogenerate
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        manager.revision(app_import_path, message=message, autogenerate=autogenerate)
    except MigrationError as exc:
        _fail(exc)
    _console.print(f"[bold green]✓[/bold green] Revision created for [bold]{escape(app_import_path)}[/bold].")


@db_app.command(rich_help_panel="Apply or roll back migrations")
def upgrade(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
    target: Annotated[
        str, typer.Argument(help="Revision target: 'head', '+N', or the full revision name in this app.")
    ] = "head",
) -> None:
    """Upgrade *APP_IMPORT_PATH* to *TARGET* (default: head).

    Migrations of the app's dependencies that are not applied yet run first.
    Other modules are left untouched. Prints the executed ``app:revision`` steps.

    Examples::

        bedrock db upgrade myerp.wms
        bedrock db upgrade myerp.wms +1
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        steps = manager.upgrade(app_import_path, target)
    except MigrationError as exc:
        _fail(exc)
    _print_steps(steps)


@db_app.command(rich_help_panel="Apply or roll back migrations")
def downgrade(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
    target: Annotated[
        str, typer.Argument(help="Revision target: 'base', '-N', or the full revision name in this app.")
    ],
    cascade: Annotated[bool, typer.Option("--cascade", help=_CASCADE_HELP)] = False,
    load: Annotated[list[str] | None, typer.Option("--load", help=_LOAD_HELP)] = None,
) -> None:
    """Downgrade *APP_IMPORT_PATH* to *TARGET*.

    Rolling back is refused while applied migrations of other modules depend on the
    ones being removed. Load those modules with ``--load`` and add ``--cascade`` to
    roll them back first. Dependents that are not loaded are always an error.
    Prints the executed ``app:revision`` steps in execution order.

    Examples::

        bedrock db downgrade myerp.wms -- -1
        bedrock db downgrade myerp.wms base
        bedrock db downgrade myerp.auth base --load myerp.orders --cascade
    """
    manager = _bootstrap_manager(app_import_path, *(load or ()))
    try:
        steps = manager.downgrade(app_import_path, target, cascade=cascade)
    except MigrationError as exc:
        _fail(exc)
    _print_steps(steps)


@db_app.command(rich_help_panel="Inspect migration state")
def heads(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
) -> None:
    """Show the head revisions for *APP_IMPORT_PATH*, one per line.

    Examples::

        bedrock db heads myerp.wms
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        revisions = manager.heads(app_import_path)
    except MigrationError as exc:
        _fail(exc)
    _print_revisions(revisions)


@db_app.command(rich_help_panel="Inspect migration state")
def current(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
) -> None:
    """Show the applied head revisions in the database for *APP_IMPORT_PATH*, one per line.

    Prints ``-`` when nothing is applied.

    Examples::

        bedrock db current myerp.wms
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        revisions = manager.current(app_import_path)
    except MigrationError as exc:
        _fail(exc)
    _print_revisions(revisions)


@db_app.command(rich_help_panel="Inspect migration state")
def check(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
) -> None:
    """Report whether each module's migrations are at head without changing the database.

    Prints the current and head revisions for the app and for every other
    installed app that already has migration scripts. Exits with status 1 when
    any reported module is behind head or its revision is unknown.

    Examples::

        bedrock db check myerp.wms
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        statuses = manager.check(app_import_path)
    except MigrationError as exc:
        _fail(exc)

    table = Table(show_header=True, header_style="bold cyan", box=None, show_edge=False)
    table.add_column("Branch", style="blue", no_wrap=True)
    table.add_column("Current", style="yellow", no_wrap=True)
    table.add_column("Head", style="yellow", no_wrap=True)
    table.add_column("State", no_wrap=True)

    failed = False
    for status in statuses:
        if status.state != "at-head":
            failed = True
        state_style = {"at-head": "bold green", "behind": "bold yellow"}.get(status.state, "bold red")
        table.add_row(
            status.branch,
            ", ".join(status.current) if status.current else "-",
            ", ".join(status.head) if status.head else "-",
            Text(status.state, style=state_style),
        )

    _console.print(table)
    if failed:
        _console.print("[bold red]✗[/bold red] One or more branches are behind head or have an unknown revision.")
        raise typer.Exit(1)
    _console.print("[bold green]✓[/bold green] All migration branches are at head.")


@db_app.command(rich_help_panel="Inspect migration state")
def history(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
) -> None:
    """Show the migration history for *APP_IMPORT_PATH*.

    Examples::

        bedrock db history myerp.wms
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        rows = manager.get_history(app_import_path)
    except MigrationError as exc:
        _fail(exc)

    if not rows:
        _console.print(f"[dim]No revisions found for [bold]{escape(app_import_path)}[/bold].[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None, show_edge=False)
    table.add_column("Revision", style="yellow", no_wrap=True)
    table.add_column("Down Revision", style="dim", no_wrap=True)
    table.add_column("Depends On", style="blue", no_wrap=True)
    table.add_column("Message", style="white")

    for row in rows:
        rev_cell = Text(row["revision"])
        if row["is_head"]:
            rev_cell.append(" (head)", style="bold green")
        table.add_row(
            rev_cell,
            Text(row["down_revision"]),
            Text(row["depends_on"]),
            Text(row["message"]),
        )

    _console.print(table)


@db_app.command(rich_help_panel="Legacy adoption and ledger setup")
def adopt(
    app_import_paths: Annotated[
        list[str], typer.Argument(help="Import paths of every module that has migrations, e.g. 'myerp.wms'.")
    ],
) -> None:
    """Import an Alembic-managed database into ``bedrock_migrations`` as a protected baseline.

    Run once per existing database, with every module loaded, before any other
    migration command. ``alembic_version`` is left untouched. No migration script
    is executed, and the imported rows cannot be rolled back.

    Examples::

        bedrock db adopt myerp.auth myerp.wms
    """
    manager = _bootstrap_manager(*app_import_paths)
    try:
        count = manager.adopt()
    except MigrationError as exc:
        _fail(exc)
    _console.print(f"[bold green]✓[/bold green] Imported {count} baseline migration(s). No scripts were executed.")


@db_app.command("init-ledger", rich_help_panel="Legacy adoption and ledger setup")
def init_ledger(
    app_import_paths: Annotated[list[str], typer.Argument(help="Root modules of a new, independent registry.")],
    adopted_ledger: Annotated[
        str, typer.Option("--adopted-ledger", help="Existing ledger that adopted this database.")
    ],
) -> None:
    """Initialize the configured empty ledger without importing or executing migrations."""
    manager = _bootstrap_manager(*app_import_paths)
    try:
        manager.init_ledger(adopted_ledger=adopted_ledger)
    except MigrationError as exc:
        _fail(exc)
    _console.print(f"Created empty migration ledger '{escape(manager.version_table)}'.")


__all__ = ["db_app"]
