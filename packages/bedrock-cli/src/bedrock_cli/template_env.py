"""Jinja2 template environment factory for bedrock-cli scaffolding.

Provides a pre-configured environment with custom filters for
converting between naming conventions used across generated files.
"""

from __future__ import annotations

import re

from jinja2 import Environment, PackageLoader


def _to_snake(value: str) -> str:
    """Convert a string to snake_case.

    Args:
        value: Input string in any casing style.

    Returns:
        The snake_case representation.
    """
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", value)
    return value.lower()


def _to_camel(value: str) -> str:
    """Convert a string to PascalCase (UpperCamelCase).

    Args:
        value: Input string in snake_case or kebab-case.

    Returns:
        The PascalCase representation.
    """
    parts = value.replace("-", "_").split("_")
    return "".join(p.capitalize() for p in parts if p)


def build_template_environment() -> Environment:
    """Build and return a configured Jinja2 Environment for bedrock-cli templates.

    The environment loads templates from the ``templates`` package directory
    and registers the following custom filters:

    - ``snake``: converts to snake_case
    - ``camel``: converts to PascalCase

    Returns:
        A ready-to-use Jinja2 Environment instance.
    """
    env = Environment(
        loader=PackageLoader("bedrock_cli", "templates"),
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.filters["snake"] = _to_snake
    env.filters["camel"] = _to_camel
    return env
