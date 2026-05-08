"""Jinja2 template environment factory for bedrock-cli scaffolding."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import BaseLoader, ChoiceLoader, Environment, FileSystemLoader, PackageLoader

_USER_TEMPLATE_DIR = "_bedrock_gen"


def _to_snake(value: str) -> str:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", value)
    return value.lower()


def _to_camel(value: str) -> str:
    parts = value.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts if p)


def _build_loader() -> BaseLoader:
    """Build a loader that checks project-local _bedrock_gen/ first, then built-in templates."""
    builtin_loader = PackageLoader("bedrock_cli", "templates")
    user_dir = Path(".").resolve() / _USER_TEMPLATE_DIR
    if user_dir.is_dir():
        return ChoiceLoader([FileSystemLoader(str(user_dir)), builtin_loader])

    return builtin_loader


def build_template_environment() -> Environment:
    """Build a Jinja2 Environment with project-local template override support.

    Template resolution order:
        1. ``<project_root>/_bedrock_gen/`` (if exists)
        2. Built-in ``bedrock_cli/templates/``
    """
    env = Environment(
        loader=_build_loader(),
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.filters["snake"] = _to_snake
    env.filters["camel"] = _to_camel
    return env
