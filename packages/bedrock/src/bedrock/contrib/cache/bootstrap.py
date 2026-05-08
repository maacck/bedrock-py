"""Bootstrap hooks for the example Bedrock module."""

from __future__ import annotations

from bedrock.module import AppConfig, ModuleRegistry


def ready(registry: ModuleRegistry, app: AppConfig) -> None:
    """Called when all modules are installed and ready."""
    from .service import cache

    cache.configure()


def on_shutdown(registry: ModuleRegistry, app: AppConfig) -> None:
    """Called when the registry shuts down."""
    from .service import cache

    cache.close()
