"""Entry point for the bedrock-cli command-line tool."""

from __future__ import annotations

import typer

from bedrock_cli.commands import inspect_app, make_app, new_app, validate_app

app = typer.Typer(
    name="bedrock",
    help="Bedrock CLI — scaffold and manage Bedrock modular applications.",
    no_args_is_help=True,
)

app.add_typer(new_app, name="new")
app.add_typer(make_app, name="make")
app.add_typer(inspect_app, name="inspect")
app.add_typer(validate_app, name="validate")


def main() -> None:
    """CLI entry point invoked by the ``bedrock`` console script."""
    app()


if __name__ == "__main__":
    main()
