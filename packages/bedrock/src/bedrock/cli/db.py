"""Bedrock database migration CLI commands.

Exposes Alembic-backed migration operations as a :class:`typer.Typer` sub-app
that can be mounted onto the main Bedrock CLI.  Each command takes a single
app import path that serves as both the host app to load into the registry and
the target branch for the migration operation.

Usage::

    bedrock db revision myerp.wms -m "add order table"
    bedrock db upgrade myerp.wms
    bedrock db upgrade myerp.wms +1
    bedrock db downgrade myerp.wms -1
    bedrock db downgrade myerp.wms base
    bedrock db heads myerp.wms
    bedrock db current myerp.wms
    bedrock db history myerp.wms
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from bedrock.database.migrations_manager import BranchOwnershipError, MigrationError, MigrationsManager

db_app = typer.Typer(
    name="db",
    rich_markup_mode="rich",
    help="Database migration commands powered by Alembic.",
)

_console = Console()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bootstrap_manager(app_import_path: str) -> MigrationsManager:
    """Populate the registry for *app_import_path* and return a :class:`MigrationsManager`.

    Args:
        app_import_path: Import path of the target app, e.g. ``"myerp.wms"``.
            Used both to populate the registry and as the migration target.

    Returns:
        A :class:`MigrationsManager` bound to the configured database URL.

    Raises:
        :class:`typer.Exit`: If the database is not configured.
    """
    from bedrock.database.config import DbSettings
    from bedrock.module.registry import apps

    if not apps.ready:
        apps.populate([app_import_path])

    database_url = DbSettings().SQLALCHEMY_DATABASE_URI
    return MigrationsManager(registry=apps, database_url=database_url)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@db_app.command()
def revision(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
    message: Annotated[str, typer.Option("-m", "--message", help="Short description for this revision.")],
    autogenerate: Annotated[bool, typer.Option(help="Autogenerate migration from model diff.")] = True,
) -> None:
    """Create a new migration revision for *APP_IMPORT_PATH*.

    On the first call for an app this creates the branch root (``--head=base``
    with a branch label equal to the import path).  Subsequent calls append
    to ``<import_path>@head``.

    Examples::

        bedrock db revision myerp.wms -m "initial schema"
        bedrock db revision myerp.wms -m "add shipment status" --no-autogenerate
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        manager.revision(app_import_path, message=message, autogenerate=autogenerate)
        _console.print(f"[bold green]✓[/bold green] Revision created for [bold]{app_import_path}[/bold].")
    except BranchOwnershipError as exc:
        _console.print(f"[bold red]✗ Branch ownership violation:[/bold red] {exc}")
        raise typer.Exit(1) from exc
    except MigrationError as exc:
        _console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc


@db_app.command()
def upgrade(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
    target: Annotated[str, typer.Argument(help="Revision target: 'head', '+1', or a revision ID.")] = "head",
) -> None:
    """Upgrade *APP_IMPORT_PATH* to *TARGET* (default: head).

    Only this app's branch is upgraded; other branches are left untouched.

    Examples::

        bedrock db upgrade myerp.wms
        bedrock db upgrade myerp.wms +1
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        manager.upgrade(app_import_path, target)
        _console.print(f"[bold green]✓[/bold green] [bold]{app_import_path}[/bold] upgraded to [bold]{target}[/bold].")
    except BranchOwnershipError as exc:
        _console.print(f"[bold red]✗ Branch ownership violation:[/bold red] {exc}")
        raise typer.Exit(1) from exc
    except MigrationError as exc:
        _console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc


@db_app.command()
def downgrade(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
    target: Annotated[str, typer.Argument(help="Revision target: 'base', '-1', or a revision ID.")],
) -> None:
    """Downgrade *APP_IMPORT_PATH* to *TARGET*.

    Examples::

        bedrock db downgrade myerp.wms -1
        bedrock db downgrade myerp.wms base
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        manager.downgrade(app_import_path, target)
        _console.print(
            f"[bold green]✓[/bold green] [bold]{app_import_path}[/bold] downgraded to [bold]{target}[/bold]."
        )
    except BranchOwnershipError as exc:
        _console.print(f"[bold red]✗ Branch ownership violation:[/bold red] {exc}")
        raise typer.Exit(1) from exc
    except MigrationError as exc:
        _console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc


@db_app.command()
def heads(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
) -> None:
    """Show the current head revision for *APP_IMPORT_PATH*.

    Examples::

        bedrock db heads myerp.wms
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        manager.heads(app_import_path)
    except MigrationError as exc:
        _console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc


@db_app.command()
def current(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the target app, e.g. 'myerp.wms'.")],
) -> None:
    """Show the current database revision for *APP_IMPORT_PATH*.

    Examples::

        bedrock db current myerp.wms
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        manager.current(app_import_path)
    except MigrationError as exc:
        _console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc


@db_app.command()
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
    except BranchOwnershipError as exc:
        _console.print(f"[bold red]✗ Branch ownership violation:[/bold red] {exc}")
        raise typer.Exit(1) from exc
    except MigrationError as exc:
        _console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc

    if not rows:
        _console.print(f"[dim]No revisions found for [bold]{app_import_path}[/bold].[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan", box=None, show_edge=False)
    table.add_column("Revision", style="yellow", no_wrap=True)
    table.add_column("Down Revision", style="dim", no_wrap=True)
    table.add_column("Branch Labels", style="blue", no_wrap=True)
    table.add_column("Message", style="white")

    for row in rows:
        rev_cell = Text(row["revision"])
        if row["is_head"]:
            rev_cell.append(" (head)", style="bold green")
        table.add_row(
            rev_cell,
            row["down_revision"],
            row["branch_labels"],
            row["message"],
        )

    _console.print(table)


@db_app.command()
def uninstall(
    app_import_path: Annotated[str, typer.Argument(help="Import path of the app to uninstall, e.g. 'myerp.wms'.")],
) -> None:
    """Downgrade *APP_IMPORT_PATH* to base before removing it from the project.

    Rolls back all applied migrations for the app's Alembic branch so that
    ``alembic_version`` no longer contains any entries for it.  After running
    this command the app can safely be removed from ``INSTALLED_APPS`` and its
    code deleted without leaving the version table in an inconsistent state.

    Examples::

        bedrock db uninstall myerp.wms
    """
    manager = _bootstrap_manager(app_import_path)
    try:
        manager.uninstall(app_import_path)
        _console.print(
            f"[bold green]✓[/bold green] [bold]{app_import_path}[/bold] downgraded to base. "
            "You can now remove it from INSTALLED_APPS."
        )
    except BranchOwnershipError as exc:
        _console.print(f"[bold red]✗ Branch ownership violation:[/bold red] {exc}")
        raise typer.Exit(1) from exc
    except MigrationError as exc:
        _console.print(f"[bold red]✗[/bold red] {exc}")
        raise typer.Exit(1) from exc


__all__ = ["db_app"]
