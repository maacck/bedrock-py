"""CLI sub-command apps for bedrock-cli."""

from __future__ import annotations

from bedrock_cli.commands.inspect import app as inspect_app
from bedrock_cli.commands.make import app as make_app
from bedrock_cli.commands.new import app as new_app
from bedrock_cli.commands.validate import app as validate_app

__all__ = ["inspect_app", "make_app", "new_app", "validate_app"]
