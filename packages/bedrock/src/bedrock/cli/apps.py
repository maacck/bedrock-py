"""Application inspection and management commands for Bedrock modules."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from bedrock.utils import inspect_func

from ..module.exc import InvalidManifestError, InvalidModuleCallableError, ModuleError
from ..module.manifest import build_app_config, load_manifest
from ..utils.lazyload import load_optional_callable

apps = typer.Typer(
    rich_markup_mode="rich",
    help="Manage Bedrock applications and modules.",
)


@dataclass
class InspectResult:
    """Result of a basic module inspection."""

    import_path: str
    manifest_valid: bool = False
    module_loads: bool = False
    has_bootstrap: bool = False
    has_models: bool = False
    installation_valid: bool = False
    errors: list[str] = field(default_factory=list)


def _status_icon(passed: bool, *, optional: bool = False) -> str:
    """Return a colored status icon for Rich table display."""
    if passed:
        return "[bold green]✓[/bold green]"
    return "[dim]-[/dim]" if optional else "[bold red]✗[/bold red]"


def _check_installation_hooks(func: Callable):
    if not inspect_func.func_accepts_kwargs(func):
        raise InvalidModuleCallableError(
            f"Installation hook '{func.__module__}.{func.__name__}' must accept **kwargs for future extensibility."
        )


def _load_optional_installation_hook(app_name: str, hook_name: str) -> Callable[..., object] | None:
    """Load an optional installation hook without masking module import errors."""
    module_name = f"{app_name}.installation"
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise InvalidModuleCallableError(f"Cannot import installation module '{module_name}': {exc}") from exc
    except ImportError as exc:
        raise InvalidModuleCallableError(f"Cannot import installation module '{module_name}': {exc}") from exc

    try:
        hook = getattr(module, hook_name)
    except AttributeError:
        return None

    if not callable(hook):
        raise InvalidModuleCallableError(f"Installation hook '{module_name}:{hook_name}' is not callable.")
    return hook


def _validate_installation_hooks(app_name: str) -> bool:
    """Validate optional installation-hook signatures without executing them."""
    hooks_found = False
    for hook_name in ("install", "pre_install", "post_install"):
        hook = _load_optional_installation_hook(app_name, hook_name)
        if hook is None:
            continue
        hooks_found = True
        _check_installation_hooks(hook)
    return hooks_found


def _run_basic_inspect(import_path: str, console: Console) -> InspectResult:
    """Validate manifest, loadability, submodules, and installation hooks with console output."""
    result = InspectResult(import_path=import_path)

    try:
        load_manifest(import_path)
        result.manifest_valid = True
        console.print(f"[bold green]✓[/bold green] Manifest for ''[bold]{import_path}[/bold]'' is valid.")
    except InvalidManifestError as exc:
        result.errors.append(f"Manifest validation failed: {exc}")
        console.print(f"[bold red]✗[/bold red] Manifest validation failed: {exc}")
        return result

    try:
        app_config = build_app_config(import_path)
        result.module_loads = True
        console.print(f"[bold green]✓[/bold green] Module ''[bold]{import_path}[/bold]'' loads successfully.")

        if app_config.bootstrap_module:
            result.has_bootstrap = True
            console.print("[bold green]✓[/bold green] Bootstrap submodule is available.")
        else:
            console.print("[bold yellow]![/bold yellow] No bootstrap submodule found.")

        if app_config.models_module:
            result.has_models = True
            console.print("[bold green]✓[/bold green] Models submodule is available.")
        else:
            console.print("[bold yellow]![/bold yellow] No models submodule found.")
    except ModuleError as exc:
        result.errors.append(f"Module load failed: {exc}")
        console.print(f"[bold red]✗[/bold red] Module load failed: {exc}")
        return result

    try:
        result.installation_valid = _validate_installation_hooks(app_config.name)
        if result.installation_valid:
            console.print(
                f"[bold green]✓[/bold green] Installation hooks for '{app_config.name}' are valid (not executed)."
            )
        else:
            console.print("[bold yellow]![/bold yellow] No installation hooks found; validation skipped.")
    except (InvalidModuleCallableError, ModuleError) as exc:
        result.errors.append(f"Installation hook check failed: {exc}")
        console.print(f"[bold red]✗[/bold red] Installation hook check failed: {exc}")

    return result


def _inspect_dependency(dep_path: str) -> InspectResult:
    """Run all inspection checks on a dependency without console output."""
    result = InspectResult(import_path=dep_path)

    spec = find_spec(dep_path)
    if spec is None:
        result.errors.append(f"Cannot import: package '{dep_path}' not found in Python path.")
        return result

    try:
        load_manifest(dep_path)
        result.manifest_valid = True
    except InvalidManifestError as exc:
        result.errors.append(f"Manifest: {exc}")
        return result

    try:
        app_config = build_app_config(dep_path)
        result.module_loads = True
        result.has_bootstrap = app_config.bootstrap_module is not None
        result.has_models = app_config.models_module is not None
    except ModuleError as exc:
        result.errors.append(f"Load: {exc}")
        return result

    try:
        result.installation_valid = _validate_installation_hooks(app_config.name)
    except (InvalidModuleCallableError, ModuleError) as exc:
        result.errors.append(f"Installation: {exc}")

    return result


@apps.command()
def inspect(
    import_path: str = typer.Argument(..., help="Python import path of the module, e.g., ''bedrock.contrib.cache''."),
) -> None:
    """Inspect a module without executing installation logic.

    Validates the manifest.yaml structure, checks bootstrap and models
    submodules, validates optional installation-hook signatures, and verifies
    that all declared dependencies (depends_on) are importable and pass basic checks.

    Args:
        import_path: Python import path of the module.

    Raises:
        typer.Exit: If the manifest or module fails to load.
    """
    console = Console()

    result = _run_basic_inspect(import_path, console)
    if result.errors:
        raise typer.Exit(1)

    manifest = load_manifest(import_path)
    if not manifest.depends_on:
        console.print("\n[dim]No dependencies declared in depends_on.[/dim]")
        return

    console.print(f"\n[bold]Checking dependencies ({len(manifest.depends_on)})...[/bold]\n")

    dep_results: list[InspectResult] = []
    for dep_path in manifest.depends_on:
        dep_results.append(_inspect_dependency(dep_path))

    table = Table(title="Dependency Inspection", show_lines=True)
    table.add_column("Module", style="bold cyan", no_wrap=True)
    table.add_column("Importable", justify="center")
    table.add_column("Manifest", justify="center")
    table.add_column("Loads", justify="center")
    table.add_column("Bootstrap", justify="center")
    table.add_column("Models", justify="center")
    table.add_column("Installation", justify="center")
    table.add_column("Errors", style="red")

    has_failures = False
    for dep_result in dep_results:
        importable = find_spec(dep_result.import_path) is not None
        if not importable or dep_result.errors:
            has_failures = True

        table.add_row(
            dep_result.import_path,
            _status_icon(importable),
            _status_icon(dep_result.manifest_valid),
            _status_icon(dep_result.module_loads),
            _status_icon(dep_result.has_bootstrap, optional=True),
            _status_icon(dep_result.has_models, optional=True),
            _status_icon(
                dep_result.installation_valid, optional=not dep_result.installation_valid and not dep_result.errors
            ),
            "; ".join(dep_result.errors) if dep_result.errors else "",
        )

    console.print(table)

    if has_failures:
        console.print("\n[bold red]✗[/bold red] Some dependencies failed inspection.")
        raise typer.Exit(1)
    else:
        console.print("\n[bold green]✓[/bold green] All dependencies pass inspection.")


@apps.command()
def info(
    import_path: str = typer.Argument(..., help="Python import path of the module, e.g., ''bedrock.contrib.cache''."),
) -> None:
    """Display rich information about a Bedrock module.

    Shows the module title, description, version, dependencies, bootstrap
    and models status, and configured commands.

    Args:
        import_path: Python import path of the module.

    Raises:
        typer.Exit: If the module cannot be loaded.
    """
    console = Console()

    try:
        app_config = build_app_config(import_path)
        manifest = app_config.manifest
    except (InvalidManifestError, ModuleError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Property", style="bold cyan", no_wrap=True)
    table.add_column("Value", style="white")

    table.add_row("Title", manifest.title)
    table.add_row("Description", manifest.description or "N/A")
    table.add_row("Version", manifest.version)
    table.add_row("Import Path", import_path)

    if manifest.depends_on:
        table.add_row("Dependencies", "\n".join(manifest.depends_on))
    else:
        table.add_row("Dependencies", "None")

    table.add_row("Bootstrap", "Yes" if app_config.bootstrap_module else "No")
    table.add_row("Models", "Yes" if app_config.models_module else "No")

    if manifest.commands:
        table.add_row("Commands", manifest.commands)

    console.print(table)


def _bootstrap_migrations_manager():
    """Build a MigrationsManager from the current settings."""
    from bedrock.database import db
    from bedrock.database.config import DbSettings
    from bedrock.database.migrations_manager import MigrationsManager
    from bedrock.module import apps

    database_url = DbSettings().SQLALCHEMY_DATABASE_URI

    if db._database_url is not None:
        database_url = db._database_url
    return MigrationsManager(registry=apps, database_url=database_url)


def _resolve_playbook_path(package_dir: Path, relative_path: str | None) -> Path:
    """Resolve a module playbook file path from a package directory.

    Args:
        package_dir: Root package directory for the module.
        relative_path: Optional relative path inside ``playbook/``.

    Returns:
        Absolute path to the requested playbook file.

    Raises:
        ValueError: If the provided path is absolute or uses parent traversal.
        FileNotFoundError: If the playbook directory or target file is missing.
    """
    playbook_dir = package_dir / "playbook"
    if not playbook_dir.is_dir():
        raise FileNotFoundError(f"Missing playbook directory at '{playbook_dir}'.")

    playbook_relative_path = Path(relative_path) if relative_path is not None else Path("PLAYBOOK.md")

    if playbook_relative_path.is_absolute():
        raise ValueError("Playbook path must be relative to the module's playbook directory.")

    if ".." in playbook_relative_path.parts:
        raise ValueError("Playbook path must not contain parent directory traversal.")

    playbook_path = playbook_dir / playbook_relative_path
    if not playbook_path.is_file():
        raise FileNotFoundError(f"Missing playbook file at '{playbook_path}'.")

    return playbook_path


@apps.command()
def playbook(
    module: str = typer.Argument(..., help="Python import path of the module, e.g., ''bedrock.contrib.cache''."),
    path: str | None = typer.Argument(
        None,
        help="Relative path inside the module's ''playbook/'' directory, e.g., ''references/some-file.md''.",
    ),
) -> None:
    """Print a module playbook file.

    Args:
        module: Python import path of the module.
        path: Optional relative path inside the module's ``playbook/`` directory.

    Raises:
        typer.Exit: If the module or playbook file cannot be resolved.
    """
    console = Console()
    playbook_relative_path = path if isinstance(path, str) else None

    try:
        app_config = build_app_config(module)
        playbook_path = _resolve_playbook_path(app_config.package_dir, playbook_relative_path)
        playbook_content = playbook_path.read_text(encoding="utf-8")
    except (InvalidManifestError, ModuleError, OSError, ValueError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    console.print(playbook_content, markup=False, highlight=False, end="")


@apps.command()
def install(
    import_path: str = typer.Argument(..., help="Python import path of the module, e.g., ''bedrock.contrib.cache''."),
    skip_migrations: bool = typer.Option(
        False, "--skip-migrations", help="Skip running database migrations during installation."
    ),
) -> None:
    """Install a Bedrock module.

    For the module this command:

    1. Ensures the database schema is up to date (create tables on first install,
       upgrade if behind head).
    2. Runs the optional ``installation.py`` lifecycle hooks
       (``pre_install`` → ``install`` → ``post_install``).

    Args:
        import_path: Python import path of the module.
        skip_migrations: Skip database migrations if True.

    Raises:
        typer.Exit: If the module cannot be loaded or installation fails.
    """
    from bedrock.module import apps

    console = Console()
    apps.populate([import_path])

    try:
        app_config = apps.get(import_path)
    except KeyError as exc:
        console.print(f"[bold red]✗[/bold red] Module '{import_path}' not found.")
        raise typer.Exit(1) from exc

    console.print(f"[bold blue]Installing module:[/bold blue] {app_config.name} {app_config.manifest.version}")

    manager = None if skip_migrations else _bootstrap_migrations_manager()

    if manager is not None and app_config.models_module is not None:
        from bedrock.database.migrations_manager import MigrationError

        try:
            status = manager.ensure_schema(app_config.name)
            _status_labels = {
                "created": "[bold green]✓[/bold green] Schema created (tables initialised and stamped at head).",
                "upgraded": "[bold green]✓[/bold green] Schema upgraded to head.",
                "up-to-date": "[dim]Schema already at head — no migration needed.[/dim]",
            }
            console.print(_status_labels.get(status, f"[dim]Schema {status}.[/dim]"))
        except MigrationError as exc:
            console.print(f"[bold red]✗ Migration failed for {app_config.name}:[/bold red] {exc}")
            raise typer.Exit(1) from exc
    elif manager is not None and app_config.models_module is None:
        console.print(f"[dim]No models module for {app_config.name} — skipping schema step.[/dim]")

    if not os.path.exists(app_config.package_dir / "installation.py"):
        console.print(
            f"[bold yellow]![/bold yellow] No installation.py found for {app_config.name}, skipping installation hooks."
        )
        console.print("[bold green]✓[/bold green] Module installed successfully.")
        return

    installation = load_optional_callable(f"{app_config.name}.installation:install")
    if not installation:
        console.print(
            f"[bold yellow]![/bold yellow] No 'install' function found in installation.py for {app_config.name}, skipping."
        )
        console.print("[bold green]✓[/bold green] Module installed successfully.")
        return

    pre_install = load_optional_callable(f"{app_config.name}.installation:pre_install")
    if pre_install:
        console.print(f"[bold blue]Running pre-install hook for {app_config.name}...[/bold blue]")
        pre_install()

    installation()

    post_install = load_optional_callable(f"{app_config.name}.installation:post_install")
    if post_install:
        console.print(f"[bold blue]Running post-install hook for {app_config.name}...[/bold blue]")
        post_install()

    console.print("[bold green]✓[/bold green] Module installed successfully.")


__all__ = ["apps"]
