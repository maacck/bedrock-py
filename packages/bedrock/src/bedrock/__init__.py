from .conf import BedrockSettings
from .entities import BedrockEntity
from .logging import get_logger
from .module import ModuleRegistry, apps

__all__ = [
    "ModuleRegistry",
    "apps",
    "BedrockEntity",
    "BedrockSettings",
    "setup",
    "get_logger",
    "StringHelpers",
    "random_password",
    "generate_token",
    "is_strong_password",
]


def setup(app: str | None = None) -> None:
    """Bootstrap the Bedrock application registry.

    Reads the target app from the ``app`` argument, falling back to the
    ``BEDROCK_APP`` environment variable (via :attr:`bedrock.settings.settings`).
    Calls :meth:`~bedrock.module.ModuleRegistry.populate` on the global
    :data:`apps` registry, which installs modules and fires lifecycle hooks.

    This function is idempotent: if the registry is already ready it returns
    immediately without re-populating.

    Args:
        app: Dotted import path of the root application module to install,
            e.g. ``"myproject.app"``.  When *None* the value is read from
            ``BEDROCK_APP`` in the environment.

    Raises:
        ImproperlyConfigured: When no app name can be resolved.

    Example — ASGI entry-point (``myproject/asgi.py``)::

        import bedrock

        bedrock.setup()

        from myproject.api import create_app

        application = create_app()

    Example — Celery entry-point (``myproject/celery.py``)::

        import bedrock

        bedrock.setup()

        from celery import Celery

        app = Celery("myproject")
    """
    if apps.ready:
        return

    from bedrock.exc import ImproperlyConfigured
    from bedrock.settings import settings

    app_name = app or settings.APP
    if app_name is None:
        raise ImproperlyConfigured(
            "No app specified. Pass an app name to setup() or set the BEDROCK_APP environment variable."
        )

    apps.populate([app_name])
