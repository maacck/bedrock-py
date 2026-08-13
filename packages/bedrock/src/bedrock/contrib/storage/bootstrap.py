"""Lifecycle hooks for the storage module.

Strict mode: the service never configures itself eagerly, so there is no
``ready`` hook — applications must call ``storage.configure(...)`` explicitly
at startup. ``on_shutdown`` releases whatever backend was configured.
"""

from __future__ import annotations

from bedrock.module import AppConfig, ModuleRegistry


def on_shutdown(*, registry: ModuleRegistry, app: AppConfig) -> None:
    """Called when the registry shuts down; closes the storage singleton."""
    from .service import storage

    storage.close()
