"""Set Bedrock up once in every Celery worker process that runs tasks, and shut it down when that process exits.

Celery's default prefork pool imports the app module in the parent process and forks the children that run tasks.
Anything Bedrock opens before the fork (connection pools, clients created by ``on_load`` hooks) would be shared by
every child. Bedrock is therefore set up after the fork, in each child, and shut down when that child exits. Pools
without child processes (solo, threads, gevent, eventlet) set Bedrock up once in the worker process.
"""

import logging
import os
import signal
from contextlib import suppress
from typing import Any

import bedrock
from celery import signals

logger = logging.getLogger(__name__)

_PREFORK_POOL = "celery.concurrency.prefork:TaskPool"


def connect_worker(app: str | None = None) -> None:
    """Connect Celery worker signals so each worker process runs ``bedrock.setup()`` and ``bedrock.shutdown()``.

    Call once in the module that creates the Celery app. With the prefork pool, every child process sets Bedrock up
    after it is forked and shuts it down before it exits; the parent process never sets Bedrock up. Other pools set
    it up once in the worker process. Hooks run exactly as under a plain ``bedrock.setup()``: synchronous hooks in
    the calling thread, async hooks on Bedrock's runtime loop, which ``async def`` tasks also use.

    If ``bedrock.setup()`` fails, the failure is logged and the whole worker stops instead of consuming tasks
    without Bedrock. Celery would otherwise log the exception from the signal handler and keep running.

    Args:
        app: Dotted import path of the root application module. When *None*, ``BEDROCK_APP`` is read from the
            environment when the worker starts.
    """

    def on_worker_init(sender: Any, **kwargs: Any) -> None:
        if not _is_prefork(sender.pool_cls):
            _setup_or_stop(app, pool_child=False)

    def on_process_init(**kwargs: Any) -> None:
        _setup_or_stop(app, pool_child=True)

    def on_shutdown(**kwargs: Any) -> None:
        bedrock.shutdown()

    signals.worker_init.connect(on_worker_init, weak=False, dispatch_uid="bedrock_celery.worker_init")
    signals.worker_process_init.connect(on_process_init, weak=False, dispatch_uid="bedrock_celery.process_init")
    signals.worker_process_shutdown.connect(on_shutdown, weak=False, dispatch_uid="bedrock_celery.process_shutdown")
    signals.worker_shutdown.connect(on_shutdown, weak=False, dispatch_uid="bedrock_celery.worker_shutdown")


def _setup_or_stop(app: str | None, *, pool_child: bool) -> None:
    """Run ``bedrock.setup()``; on failure stop the worker, which Celery would not do for a signal handler error.

    ``SystemExit`` passes through Celery's signal dispatcher, which only catches ``Exception``. In the worker
    process it aborts startup. A prefork child also asks its parent to shut down, so the pool does not keep
    replacing children that fail the same way.
    """
    try:
        bedrock.setup(app)
    except Exception:
        logger.exception("Bedrock setup failed; stopping the Celery worker so it does not run tasks without Bedrock.")
        with suppress(Exception):
            bedrock.shutdown()  # release modules that loaded before the failure
        if pool_child:
            os.kill(os.getppid(), signal.SIGTERM)
        raise SystemExit(1) from None


def _is_prefork(pool_cls: object) -> bool:
    """Return whether the worker's pool forks child processes; ``pool_cls`` may still be an alias here."""
    if isinstance(pool_cls, str):
        from celery.concurrency import ALIASES

        return ALIASES.get(pool_cls, pool_cls) == _PREFORK_POOL
    from celery.concurrency.prefork import TaskPool

    return isinstance(pool_cls, type) and issubclass(pool_cls, TaskPool)
