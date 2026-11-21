"""Application inspection and management commands for Bedrock modules."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from bedrock.utils import inspect_func

from ..module.entities import AppConfig
from ..module.exc import InvalidManifestError, InvalidModuleCallableError, ModuleError
from ..module.manifest import build_app_config, load_manifest
from ..utils.lazyload import load_optional_callable

if TYPE_CHECKING:
    from bedrock.database import MigrationsManager

    from ..module.registry import ModuleRegistry

apps = typer.Typer(
    rich_markup_mode="rich",
    help="Inspect modules, read their playbooks, or run installation hooks.",
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


def _package_dir(import_path: str) -> Path:
    """Return a module's package directory after validating its manifest, without importing its code."""
    load_manifest(import_path)
    spec = find_spec(import_path)
    assert spec is not None and spec.origin is not None  # load_manifest raised otherwise
    return Path(spec.origin).parent


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
        playbook_path = _resolve_playbook_path(_package_dir(module), playbook_relative_path)
        playbook_content = playbook_path.read_text(encoding="utf-8")
    except (InvalidManifestError, ModuleError, OSError, ValueError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from exc

    console.print(playbook_content, markup=False, highlight=False, end="")


def _dependency_closure(registry: ModuleRegistry, target: str) -> list[str]:
    """Return *target* and its transitive dependencies in dependency order, each module once."""
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dependency in registry.get(name).manifest.depends_on:
            visit(dependency)
        order.append(name)

    visit(target)
    return order


def _install_module(console: Console, app_config: AppConfig, manager: MigrationsManager | None) -> None:
    """Run the schema step (unless *manager* is ``None``) and the installation hooks of one module."""
    console.print(f"[bold blue]Installing module:[/bold blue] {app_config.name} {app_config.manifest.version}")

    if manager is not None and app_config.models_module is not None:
        from bedrock.database import MigrationError

        try:
            status = manager.ensure_schema(app_config.name)
            _status_labels = {
                "created": "[bold green]✓[/bold green] Schema created (migrations applied).",
                "upgraded": "[bold green]✓[/bold green] Schema upgraded to head.",
                "up-to-date": "[dim]Schema already at head — no migration needed.[/dim]",
            }
            console.print(_status_labels.get(status, f"[dim]Schema {status}.[/dim]"))
        except MigrationError as exc:
            console.print(f"[bold red]✗ Migration failed for {app_config.name}:[/bold red] {exc}")
            raise typer.Exit(1) from exc
    elif app_config.models_module is None:
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


@apps.command()
def install(
    target: Annotated[str, typer.Argument(help="Import path of the module to install, e.g. 'myerp.wms'.")],
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            help="Import path of the app to load for context; TARGET must be this app or one of its dependencies. "
            "Defaults to TARGET. Selects what is loaded only, never what is installed.",
        ),
    ] = None,
    with_dependencies: Annotated[
        bool,
        typer.Option(
            "--with-dependencies",
            help="Also install TARGET's dependencies, each once, in dependency order.",
        ),
    ] = False,
    skip_migrations: Annotated[
        bool,
        typer.Option("--skip-migrations", help="Skip running database migrations during installation."),
    ] = False,
) -> None:
    """Install the Bedrock module TARGET.

    Only TARGET is installed unless ``--with-dependencies`` is given. For each installed
    module this command:

    1. Applies pending migration scripts, including required migration dependencies.
       ``--skip-migrations`` skips only this step.
    2. Runs the optional ``installation.py`` lifecycle hooks
       (``pre_install`` → ``install`` → ``post_install``), even when the schema is already current.

    Examples::

        bedrock app install myerp.wms
        bedrock app install myerp.wms --with-dependencies
        bedrock app install myerp.wms --root myerp.app

    Raises:
        typer.Exit: If the module cannot be loaded, is not part of ROOT, or installation fails.
    """
    from bedrock.module import apps

    console = Console()
    root = root or target
    try:
        apps.populate([root])
        if root != target and target not in _dependency_closure(apps, root):
            raise KeyError(target)
        names = _dependency_closure(apps, target) if with_dependencies else [target]
        modules = [apps.get(name) for name in names]
    except ModuleError as exc:
        console.print(f"[bold red]✗[/bold red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    except KeyError as exc:
        console.print(
            f"[bold red]✗[/bold red] Module '{escape(target)}' is not in '{escape(root)}' or its dependencies."
        )
        raise typer.Exit(1) from exc

    manager = None
    if not skip_migrations and any(module.models_module is not None for module in modules):
        from .db import build_migrations_manager

        manager = build_migrations_manager(apps)

    for module in modules:
        _install_module(console, module, manager)


__all__ = ["apps"]
