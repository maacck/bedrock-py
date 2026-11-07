"""Entity models for the Bedrock module runtime."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import ConfigDict, Field, PrivateAttr, computed_field
from typer import Typer

from ..constants import StrEnum
from ..entities import BedrockEntity
from ..utils.lazyload import load_string
from .exc import ModuleLifecycleError


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


class ModuleLifecycleState(StrEnum):
    """Lifecycle stage of an installed module.

    A module only moves forward through ``LOADING -> LOADED -> READY -> SHUTDOWN``.
    ``READY`` may be skipped when a loaded module is shut down (or unwound after
    a failed startup) before it was readied.
    """

    LOADING = "loading"
    LOADED = "loaded"
    READY = "ready"
    SHUTDOWN = "shutdown"


_ALLOWED_TRANSITIONS: dict[ModuleLifecycleState, frozenset[ModuleLifecycleState]] = {
    ModuleLifecycleState.LOADING: frozenset({ModuleLifecycleState.LOADED}),
    ModuleLifecycleState.LOADED: frozenset({ModuleLifecycleState.READY, ModuleLifecycleState.SHUTDOWN}),
    ModuleLifecycleState.READY: frozenset({ModuleLifecycleState.SHUTDOWN}),
    ModuleLifecycleState.SHUTDOWN: frozenset(),
}


class AppConfig(BedrockEntity):
    """Fully loaded module registered in the Bedrock runtime.

    Attributes:
        name: Import path used to install this module, e.g. ``"bedrock_app"``.
        manifest: Parsed manifest metadata.
        package_module: The imported top-level Python module.
        bootstrap_module: The imported ``bootstrap`` submodule, if present.
        models_module: The imported ``models`` submodule, if present.
        lifecycle_state: Current lifecycle stage of this module. Read-only: it starts at
            ``LOADING`` and changes only through ``transition_to``, which rejects invalid
            transitions. Assigning to it raises ``AttributeError``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    manifest: ModuleManifest
    package_module: ModuleType
    package_dir: Path
    bootstrap_module: ModuleType | None = None
    models_module: ModuleType | None = None
    _lifecycle_state: ModuleLifecycleState = PrivateAttr(default=ModuleLifecycleState.LOADING)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lifecycle_state(self) -> ModuleLifecycleState:
        """Return the current lifecycle stage of this module."""
        return self._lifecycle_state

    def transition_to(self, target: ModuleLifecycleState) -> None:
        """Move this module to a later lifecycle state.

        Args:
            target: The state to move to, as a ``ModuleLifecycleState`` or its string value.

        Raises:
            ModuleLifecycleError: If ``target`` is not a valid next state from the current one.
            ValueError: If ``target`` is not the name of a lifecycle state.
        """
        current = self._lifecycle_state
        target = ModuleLifecycleState(target)
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise ModuleLifecycleError(
                f"Module '{self.name}' cannot move from lifecycle state '{current.value}' to '{target.value}'."
            )
        self._lifecycle_state = target

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
    "ModuleLifecycleState",
    "ModuleManifest",
]
