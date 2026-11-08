"""Bootstrap hooks for the cache module."""

from __future__ import annotations

from collections.abc import Awaitable

from bedrock.module import AppConfig, ModuleRegistry


def ready(*, registry: ModuleRegistry, app: AppConfig) -> None:
    """Called when all modules are installed and ready."""
    from .service import cache

    cache.configure()


def on_shutdown(*, registry: ModuleRegistry, app: AppConfig) -> Awaitable[None] | None:
    """Close synchronous clients now and hand any asynchronous close back to the registry.

    The hook stays synchronous so memory-only applications can shut down from any context. When a backend
    still holds async clients, such as Redis, it returns ``cache.aclose()`` and the registry awaits it before
    it reports the module stopped.
    """
    from .service import cache

    cache.close()
    if cache.has_async_resources():
        return cache.aclose()
    return None
