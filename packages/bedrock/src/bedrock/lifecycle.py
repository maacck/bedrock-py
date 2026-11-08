"""Top-level process lifecycle: bootstrap the module registry and release process infrastructure.

``setup()`` / ``asetup()`` start the global registry. ``shutdown()`` / ``ashutdown()`` stop every module and then
release infrastructure that is process-owned rather than module-owned, such as the database engines. Use one pair
consistently: the async pair when the process runs an event loop, the sync pair otherwise.
"""

import sys
from typing import TYPE_CHECKING

from .module import ModuleAsyncRequiredError, apps
from .module._runtime import AwaitEffect, CallEffect, loop_is_running
from .module.registry import Finalizer

if TYPE_CHECKING:
    from .database import DatabaseManager


def _resolve_app(app: str | None) -> str:
    """Return the root application import path from ``app`` or ``BEDROCK_APP``.

    Raises:
        ImproperlyConfigured: When no app name can be resolved.
    """
    from bedrock.exc import ImproperlyConfigured
    from bedrock.settings import settings

    app_name = app or settings.APP
    if app_name is None:
        raise ImproperlyConfigured(
            "No app specified. Pass an app name to setup() or set the BEDROCK_APP environment variable."
        )
    return app_name


def setup(app: str | None = None) -> None:
    """Bootstrap the Bedrock application registry.

    Reads the target app from the ``app`` argument, falling back to the
    ``BEDROCK_APP`` environment variable (via :attr:`bedrock.settings.settings`).
    Calls :meth:`~bedrock.module.ModuleRegistry.populate` on the global
    :data:`apps` registry, which installs modules and fires lifecycle hooks.

    This function is idempotent: if the registry is already ready it returns
    immediately without re-populating.

    Synchronous and asynchronous hooks both work here. Asynchronous hooks run on a persistent
    private event loop that stays open until :func:`shutdown`, so use this entrypoint only in
    a thread without a running event loop and stop the process with :func:`shutdown`. In a
    running loop use :func:`asetup`.

    It does not configure logging. Importing Bedrock leaves existing loguru
    sinks in place. A process that wants Bedrock's stderr or ``LOG_FILE`` sink
    calls :func:`bedrock.logging.configure_logging` itself. The Bedrock CLI
    calls :func:`bedrock.logging.configure_cli_logging`, which also retires
    Loguru's preconfigured stderr handler.

    Args:
        app: Dotted import path of the root application module to install,
            e.g. ``"myproject.app"``.  When *None* the value is read from
            ``BEDROCK_APP`` in the environment.

    Raises:
        ImproperlyConfigured: When no app name can be resolved.
        ModuleAsyncRequiredError: When a hook needs an event loop but one is already running.

    Example — ASGI entry-point (``myproject/asgi.py``)::

        import bedrock
        from bedrock.logging import configure_logging

        configure_logging()
        bedrock.setup()

        from myproject.api import create_app

        application = create_app()

    Example — Celery entry-point (``myproject/celery.py``)::

        import bedrock
        from bedrock.logging import configure_logging

        configure_logging()
        bedrock.setup()

        from celery import Celery

        app = Celery("myproject")
    """
    if apps.ready:
        return
    apps.populate([_resolve_app(app)])


async def asetup(app: str | None = None) -> None:
    """Bootstrap the Bedrock application registry, awaiting asynchronous hooks.

    Same contract as :func:`setup`: idempotent once the registry is ready, no logging
    configuration. Asynchronous hooks and signal receivers run on the running loop and
    synchronous ones in a worker thread. Pair it with :func:`ashutdown`.

    Args:
        app: Dotted import path of the root application module to install. When *None*
            the value is read from ``BEDROCK_APP`` in the environment.

    Raises:
        ImproperlyConfigured: When no app name can be resolved.

    Example — ASGI lifespan::

        @asynccontextmanager
        async def lifespan(app):
            await bedrock.asetup()
            try:
                yield
            finally:
                await bedrock.ashutdown()
    """
    if apps.ready:
        return
    await apps.apopulate([_resolve_app(app)])


def shutdown() -> None:
    """Shut down every module, then release process infrastructure.

    Runs :meth:`~bedrock.module.ModuleRegistry.shutdown` on the global registry. After every module
    ``on_shutdown`` hook, ``module_shutdown`` receiver and ``registry_shutdown`` receiver has run, the
    initialized database is released: ``db.close()`` for the synchronous engine and ``db.aclose()`` for the
    asynchronous engine, each only if that engine exists. The database is attempted even when a module
    fails, and a database failure is reported together with module failures in one
    :class:`~bedrock.module.ModuleLifecycleError`. When the database was never initialized this does
    nothing for it, and cleanup never initializes or rebuilds anything.

    Releasing an asynchronous engine runs on the registry's persistent adapter loop. An engine whose
    connections were opened on a different loop may fail to close from here; stop such a process with
    :func:`ashutdown` on the loop that opened them.

    Repeated calls are harmless. Call it from a thread without a running event loop.

    Raises:
        ModuleLifecycleError: After all attempts, if any module or database step failed.
        ModuleAsyncRequiredError: If an asynchronous engine or hook has to be released inside a running
            event loop. Raised before anything is shut down.
    """
    if loop_is_running() and _db_manager_has_async_engine():
        raise ModuleAsyncRequiredError(
            "The database has an initialized async engine, which cannot be released from shutdown() inside a "
            "running event loop. Use await bedrock.ashutdown() instead."
        )
    apps._shutdown_with_cleanups(_database_finalizers)


async def ashutdown() -> None:
    """Shut down every module, then release process infrastructure, awaiting asynchronous hooks.

    Same contract as :func:`shutdown`. The coroutine completes only after every module hook and both
    database engines are released, and it leaves no task running. The synchronous ``db.close()`` runs inline
    on the event loop thread.

    Raises:
        ModuleLifecycleError: After all attempts, if any module or database step failed.
    """
    await apps._ashutdown_with_cleanups(_database_finalizers)


def _database_manager() -> "DatabaseManager | None":
    """Return the ``db`` singleton if the database package was ever imported, else ``None``.

    A process whose modules never imported ``bedrock.database`` cannot have initialized an engine, so
    shutdown does not import SQLAlchemy and Alembic just to find nothing to close.
    """
    module = sys.modules.get("bedrock.database.manager")
    return getattr(module, "db", None) if module is not None else None


def _db_manager_has_async_engine() -> bool:
    db = _database_manager()
    return db is not None and db.has_async_engine


def _database_finalizers() -> list[Finalizer]:
    """Build the database release steps for the engines that exist right now."""
    db = _database_manager()
    if db is None:
        return []
    steps: list[Finalizer] = [("bedrock.database", "database_close", CallEffect(db.close))]
    if db.has_async_engine:
        steps.append(("bedrock.database", "database_aclose", AwaitEffect(db.aclose)))
    return steps


__all__ = ["ashutdown", "asetup", "setup", "shutdown"]
