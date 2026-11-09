"""Bootstrap hooks for the rate-limit module."""

from __future__ import annotations

from collections.abc import Awaitable

from bedrock.module import AppConfig, ModuleRegistry


def ready(*, registry: ModuleRegistry, app: AppConfig) -> None:
    """Configure the backend named by ``RATE_LIMIT_BACKEND`` (redis when unset) once all modules are loaded.

    Custom backends registered in an ``on_load`` hook can be selected this way, since ``ready`` runs after every
    module was installed.
    """
    from .service import rate_limit

    rate_limit.configure()


def on_shutdown(*, registry: ModuleRegistry, app: AppConfig) -> Awaitable[None] | None:
    """Close synchronous clients now and hand any asynchronous close back to the registry."""
    from .service import rate_limit

    rate_limit.close()
    if rate_limit.has_async_resources():
        return rate_limit.aclose()
    return None
