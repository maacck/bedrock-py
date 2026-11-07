import typer
from typer import Typer

from bedrock.logging import configure_cli_logging

from .apps import apps
from .db import db_app
from .manage import manage_app
from .run import run_app

bedrock_cli = Typer(rich_markup_mode="rich", help="Bedrock CLI for managing Bedrock applications and modules.")

bedrock_cli.add_typer(run_app, name="run")
bedrock_cli.add_typer(apps, name="app")
bedrock_cli.add_typer(manage_app, name="manage")
bedrock_cli.add_typer(db_app, name="db")


@bedrock_cli.callback(invoke_without_command=True)
def bedrock_callback(ctx: typer.Context):
    """ """

    if ctx.invoked_subcommand is None:
        pass


def entrypoint() -> None:
    """Install Bedrock's log sink, then dispatch CLI commands.

    Logging stays out of import and :func:`bedrock.setup`. This process entry
    point retires Loguru's preconfigured stderr handler and installs Bedrock's
    sink so ``LOG_*`` controls CLI stderr. Host handlers are kept.
    """
    configure_cli_logging()
    bedrock_cli()


if __name__ == "__main__":
    entrypoint()
