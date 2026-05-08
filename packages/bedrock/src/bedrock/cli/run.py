import sys

import click
import typer
from click import Context
from typer.core import TyperGroup

from bedrock.settings import settings

_APP_LOAD_STATES: dict[str, object] = {"loaded": False}
_PENDING_APP_NAME: str | None = None


def _parse_args_for_app(args: list[str]) -> str | None:
    """Return the value of ``--app`` / ``-a`` from a raw argument list."""
    for i, arg in enumerate(args):
        if arg in ("--app", "-a") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--app="):
            return arg.split("=", 1)[1]
        if arg.startswith("-a="):
            return arg.split("=", 1)[1]
    return None


def _parse_run_args_for_app(argv: list[str]) -> str | None:
    """Scan *argv* for the first ``--app`` value that appears after ``run``."""
    run_seen = False
    for i, arg in enumerate(argv):
        if arg == "run":
            run_seen = True
            continue
        if run_seen:
            if arg in ("--app", "-a") and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith("--app="):
                return arg.split("=", 1)[1]
            if arg.startswith("-a="):
                return arg.split("=", 1)[1]
    return None


class _AppCommandGroup(TyperGroup):
    """Click group that lazily loads app Typer subcommands before resolution."""

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: object,
    ) -> Context:
        global _PENDING_APP_NAME
        app = _parse_args_for_app(args) or _PENDING_APP_NAME or settings.APP
        if app and not _APP_LOAD_STATES.get("loaded"):
            self._load_app_commands(app)

        ctx = super().make_context(info_name, args, parent, **extra)
        ctx._raw_args = list(args)
        return ctx

    def _load_app_commands(self, app_name: str):
        from bedrock.module import apps

        apps.populate([app_name])

        for app_instance in apps.all():
            commands = app_instance.commands()
            if commands is not None:
                # Wrap in a parent Typer so get_command returns a Group containing the app as a subcommand
                parent = typer.Typer()
                parent.add_typer(commands, name=app_instance.name)
                parent_cmd = typer.main.get_command(parent)
                # Extract the app subcommand from the parent
                if hasattr(parent_cmd, "commands") and app_instance.name in parent_cmd.commands:
                    self.commands[app_instance.name] = parent_cmd.commands[app_instance.name]

        _APP_LOAD_STATES["loaded"] = True

    def format_help(self, ctx: Context, formatter: click.HelpFormatter):
        if not _APP_LOAD_STATES.get("loaded"):
            global _PENDING_APP_NAME
            app = ctx.params.get("app")
            if not app:
                raw_args: list[str] = getattr(ctx, "_raw_args", [])
                app = _parse_args_for_app(raw_args) or _PENDING_APP_NAME or settings.APP
            if app:
                self._load_app_commands(app)
        return super().format_help(ctx, formatter)

    def get_help(self, ctx: Context) -> str:
        if not _APP_LOAD_STATES.get("loaded"):
            global _PENDING_APP_NAME
            app = ctx.params.get("app")
            if not app:
                raw_args: list[str] = getattr(ctx, "_raw_args", [])
                app = _parse_args_for_app(raw_args) or _PENDING_APP_NAME or settings.APP
            if app:
                self._load_app_commands(app)
        return super().get_help(ctx)

    def resolve_command(self, ctx: Context, args: list[str]) -> tuple[str, click.Command, list[str]]:
        if not _APP_LOAD_STATES.get("loaded"):
            global _PENDING_APP_NAME
            app = ctx.params.get("app")
            if not app:
                raw_args: list[str] = getattr(ctx, "_raw_args", [])
                app = _parse_args_for_app(raw_args) or _PENDING_APP_NAME or settings.APP
            if app:
                self._load_app_commands(app)
        return super().resolve_command(ctx, args)

    def get_command(self, ctx: Context, cmd_name: str) -> click.Command | None:
        if not _APP_LOAD_STATES.get("loaded"):
            global _PENDING_APP_NAME
            app = ctx.params.get("app")
            if not app:
                raw_args: list[str] = getattr(ctx, "_raw_args", [])
                app = _parse_args_for_app(raw_args) or _PENDING_APP_NAME or settings.APP
            if app:
                self._load_app_commands(app)
        return super().get_command(ctx, cmd_name)

    def list_commands(self, ctx: Context) -> list[str]:
        if not _APP_LOAD_STATES.get("loaded"):
            global _PENDING_APP_NAME
            app = ctx.params.get("app")
            if not app:
                raw_args: list[str] = getattr(ctx, "_raw_args", [])
                app = _parse_args_for_app(raw_args) or _PENDING_APP_NAME or settings.APP
            if app:
                self._load_app_commands(app)
        return super().list_commands(ctx)


def _create_run_app() -> typer.Typer:
    """Create the ``run`` Typer with dynamic app command loading."""
    global _PENDING_APP_NAME

    raw_args = sys.argv[1:]
    _PENDING_APP_NAME = _parse_run_args_for_app(raw_args) or settings.APP

    run_app = typer.Typer(
        cls=_AppCommandGroup,
        help="Run a Bedrock application's commands",
    )

    @run_app.callback(invoke_without_command=True)
    def main(
        ctx: typer.Context, app: str | None = typer.Option(None, "--app", "-a", help="Specify the module to load")
    ):
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())
            ctx.exit(0)

    return run_app


run_app = _create_run_app()
