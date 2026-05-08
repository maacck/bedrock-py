"""Rich console instance and helper output functions for bedrock-cli."""

from __future__ import annotations

from rich.console import Console

console = Console()


def info(message: str) -> None:
    """Print an informational message.

    Args:
        message: The message to display.
    """
    console.print(f"  {message}", style="dim")


def success(message: str) -> None:
    """Print a success message.

    Args:
        message: The message to display.
    """
    console.print(f"[bold green]✓[/bold green] {message}")


def warning(message: str) -> None:
    """Print a warning message.

    Args:
        message: The message to display.
    """
    console.print(f"[bold yellow]![/bold yellow] {message}")


def error(message: str) -> None:
    """Print an error message.

    Args:
        message: The message to display.
    """
    console.print(f"[bold red]✗[/bold red] {message}")
