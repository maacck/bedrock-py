"""Entity models for the Bedrock module runtime."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from pydantic import Field
from typer import Typer

from ..entities import BedrockEntity
from ..utils.lazyload import load_string


class ModuleManifest(BedrockEntity):
    """Validated representation of a Bedrock ``manifest.yaml`` file.

    Attributes:
        title: Human-friendly display name used in logging and error messages.
        description: Optional short description of the module.
        version: Module version string.
        depends_on: Ordered list of import paths this module depends on.
        commands: a typer app's relative import path 'commands:app'
    """

    title: str
    description: str | None = None
    version: str = "unknown"
    depends_on: list[str] = Field(default_factory=list)
    commands: str | None = None


class AppConfig(BedrockEntity):
    """Fully loaded module registered in the Bedrock runtime.

    Attributes:
        name: Import path used to install this module, e.g. ``"bedrock_app"``.
        manifest: Parsed manifest metadata.
        package_module: The imported top-level Python module.
        bootstrap_module: The imported ``bootstrap`` submodule, if present.
        models_module: The imported ``models`` submodule, if present.
        lifecycle_state: Current lifecycle stage of this module.
    """

    name: str
    manifest: ModuleManifest
    package_module: ModuleType
    package_dir: Path
    bootstrap_module: ModuleType | None = None
    models_module: ModuleType | None = None
    lifecycle_state: Literal["loaded", "ready", "shutdown"] = "loaded"

    def get_hook(self, hook_name: str) -> Any | None:
        """Return a bootstrap hook function by name, or ``None`` if absent.

        Args:
            hook_name: Name of the hook function to look up in ``bootstrap.py``.

        Returns:
            The hook callable if present, otherwise ``None``.
        """
        if self.bootstrap_module is None:
            return None
        return getattr(self.bootstrap_module, hook_name, None)

    def commands(self) -> Typer | None:
        """Return a Typer app instance if the module declares commands, otherwise None."""
        if not self.manifest.commands:
            return None

        try:
            # Resolve relative paths like 'commands:app' to fully qualified paths
            # like 'bedrock_example.commands:app'
            module_part, _class_part = self.manifest.commands.split(":", 1)
            if "." not in module_part:
                full_path = f"{self.name}.{self.manifest.commands}"
            else:
                full_path = self.manifest.commands

            typer_app = load_string(full_path)
            if not isinstance(typer_app, Typer):
                raise TypeError(f"Declared commands app '{full_path}' is not a Typer instance.")
            return typer_app
        except Exception as e:
            raise ImportError(
                f"Failed to import commands app '{self.manifest.commands}' for module '{self.name}': {e}"
            ) from e


__all__ = [
    "AppConfig",
    "ModuleManifest",
]
