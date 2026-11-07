"""Bootstrap hooks for the metrics module."""

from __future__ import annotations

from collections.abc import Awaitable

from bedrock.module import AppConfig, ModuleRegistry


def on_shutdown(*, registry: ModuleRegistry, app: AppConfig) -> Awaitable[None] | None:
    """Flush and close providers, handing any asynchronous release back to the registry.

    The hook stays synchronous so applications with only synchronous providers can shut down from any
    context. When a provider defines ``aflush`` or ``aclose`` it returns ``metrics.aclose()`` and the registry
    awaits it before it reports the module stopped.
    """
    from .service import metrics

    metrics.close()
    if metrics.has_async_resources():
        return metrics.aclose()
    return None
