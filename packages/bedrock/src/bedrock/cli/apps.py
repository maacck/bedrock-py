"""Application inspection and management commands for Bedrock modules."""

from __future__ import annotations

from collections.abc import Callable

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


def _check_installation_hooks(func: Callable):
    if not inspect_func.func_accepts_kwargs(func):
        raise InvalidModuleCallableError(
            f"Installation hook '{func.__module__}.{func.__name__}' must accept **kwargs for future extensibility."
        )


@apps.command()
def inspect(
    import_path: str = typer.Argument(..., help="Python import path of the module, e.g., ''bedrock.contrib.cache''."),
) -> None:
    """Inspect a module''s manifest format and loadability.

    Validates the manifest.yaml structure, checks bootstrap and models
    submodules, and reports whether the module can be successfully loaded.

    Args:
        import_path: Python import path of the module.

    Raises:
        typer.Exit: If the manifest or module fails to load.
    """
    console = Console()

    try:
        load_manifest(import_path)
        console.print(f"[bold green]\u2713[/bold green] Manifest for ''[bold]{import_path}[/bold]'' is valid.")
    except InvalidManifestError as exc:
        console.print(f"[bold red]\u2717[/bold red] Manifest validation failed: {exc}")
        raise typer.Exit(1) from exc

    try:
        app_config = build_app_config(import_path)
        console.print(f"[bold green]\u2713[/bold green] Module ''[bold]{import_path}[/bold]'' loads successfully.")

        if app_config.bootstrap_module:
            console.print("[bold green]\u2713[/bold green] Bootstrap submodule is available.")
        else:
            console.print("[bold yellow]![/bold yellow] No bootstrap submodule found.")

        if app_config.models_module:
            console.print("[bold green]\u2713[/bold green] Models submodule is available.")
        else:
            console.print("[bold yellow]![/bold yellow] No models submodule found.")
    except ModuleError as exc:
        console.print(f"[bold red]\u2717[/bold red] Module load failed: {exc}")
        raise typer.Exit(1) from exc

    # check installation
    try:
        installation = load_optional_callable(f"{app_config.name}.installation:install")
        if not installation:
            console.print(
                f"[bold red]\u2717[/bold red] No 'install' function found in installation.py for {app_config.name}."
            )
            raise typer.Exit(1)
        _check_installation_hooks(installation)
        pre_install = load_optional_callable(f"{app_config.name}.installation:pre_install")
        if pre_install:
            _check_installation_hooks(pre_install)
        installation()
        post_install = load_optional_callable(f"{app_config.name}.installation:post_install")
        if post_install:
            _check_installation_hooks(post_install)
        console.print(
            f"[bold green]\u2713[/bold green] Installation hooks for '{app_config.name}' are valid and executable."
        )
    except (InvalidModuleCallableError, ModuleError) as exc:
        console.print(f"[bold red]\u2717[/bold red] Installation hook check failed: {exc}")
        raise typer.Exit(1) from exc


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


@apps.command()
def install(
    import_path: str = typer.Argument(..., help="Python import path of the module, e.g., ''bedrock.contrib.cache''."),
) -> None:
    """Install a Bedrock module.

    This command is a skeleton placeholder and is not yet implemented.

    Args:
        import_path: Python import path of the module.
    """
    console = Console()
    console.print("[bold yellow]⚠[/bold yellow] Install command is not yet implemented.")
    console.print(f"[dim]Module: {import_path}[/dim]")


__all__ = ["apps"]
