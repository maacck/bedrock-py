import os

import typer
from rich.console import Console

from bedrock import get_logger
from bedrock.settings import settings
from bedrock.utils.lazyload import load_optional_callable

logger = get_logger()


def manage_callback(app: str | None = typer.Option(None, "-A", help="Specify the module to load")):
    from bedrock.module import apps

    app = app or settings.APP
    if not app:
        typer.echo("No app specified. Use --app or set the APP environment variable.")
        raise typer.Exit(code=1)
    apps.populate([app])
    for app in apps.all():
        commands_app = app.commands()
        if commands_app:
            manage_app.add_typer(commands_app, name=app.name)


manage_app = typer.Typer(help="Manage Bedrock applications and modules", callback=manage_callback)


def _bootstrap_migrations_manager():
    """Build a :class:`~bedrock.database.MigrationsManager` from the current settings.

    Returns:
        A :class:`~bedrock.database.MigrationsManager` bound to the configured
        database URL and the populated app registry.

    Raises:
        :class:`typer.Exit`: If the database is not configured.
    """
    from bedrock.database.config import DbSettings
    from bedrock.database.migrations_manager import MigrationsManager
    from bedrock.module import apps

    database_url = DbSettings().SQLALCHEMY_DATABASE_URI
    return MigrationsManager(registry=apps, database_url=database_url)


@manage_app.command()
def install(
    app: str | None = typer.Option(
        None, help="Specify the module to install. Leave blank to install all loaded modules."
    ),
    skip_migrations: bool = typer.Option(
        False, "--skip-migrations", help="Skip running database migrations during installation."
    ),
):
    """Install the current Bedrock application.

    For each module this command:

    1. Ensures the database schema is up to date (create tables on first install,
       upgrade if behind head).
    2. Runs the optional ``installation.py`` lifecycle hooks
       (``pre_install`` → ``install`` → ``post_install``).
    """
    from bedrock.module import apps

    console = Console()
    console.print("[bold green]\u2713[/bold green] Installing application...")
    if app:
        try:
            apps.get(app)
        except KeyError as ex:
            console.print(f"[bold red]\u2717[/bold red] App '{app}' not found")
            raise typer.Exit(1) from ex

    modules_to_install = [apps.get(app)] if app else apps.all()

    manager = None if skip_migrations else _bootstrap_migrations_manager()

    for module in modules_to_install:
        console.print(f"[bold blue]Installing app:[/bold blue] {module.name} {module.manifest.version}")

        # ------------------------------------------------------------------
        # Step 1: Database schema
        # ------------------------------------------------------------------
        if manager is not None and module.models_module is not None:
            from bedrock.database.migrations_manager import MigrationError

            try:
                status = manager.ensure_schema(module.name)
                _status_labels = {
                    "created": "[bold green]✓[/bold green] Schema created (tables initialised and stamped at head).",
                    "upgraded": "[bold green]✓[/bold green] Schema upgraded to head.",
                    "up-to-date": "[dim]Schema already at head — no migration needed.[/dim]",
                }
                console.print(_status_labels.get(status, f"[dim]Schema {status}.[/dim]"))
            except MigrationError as exc:
                console.print(f"[bold red]✗ Migration failed for {module.name}:[/bold red] {exc}")
                raise typer.Exit(1) from exc
        elif manager is not None and module.models_module is None:
            console.print(f"[dim]No models module for {module.name} — skipping schema step.[/dim]")

        # ------------------------------------------------------------------
        # Step 2: Installation lifecycle hooks
        # ------------------------------------------------------------------
        if not os.path.exists(module.package_dir / "installation.py"):
            console.print(
                f"[bold yellow]![/bold yellow] No installation.py found for {module.name}, skipping installation hooks."
            )
            continue

        installation = load_optional_callable(f"{module.name}.installation:install")
        if not installation:
            console.print(
                f"[bold yellow]![/bold yellow] No 'install' function found in installation.py for {module.name}, skipping."
            )
            continue

        pre_install = load_optional_callable(f"{module.name}.installation:pre_install")
        if pre_install:
            console.print(f"[bold blue]Running pre-install hook for {module.name}...[/bold blue]")
            pre_install()

        installation()

        post_install = load_optional_callable(f"{module.name}.installation:post_install")
        if post_install:
            console.print(f"[bold blue]Running post-install hook for {module.name}...[/bold blue]")
            post_install()

    console.print("[bold green]\u2713[/bold green] Application installed successfully.")
