import typer
from typer import Typer

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


def entrypoint():
    bedrock_cli()


if __name__ == "__main__":
    entrypoint()
