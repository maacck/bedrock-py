"""Run Bedrock once per Celery worker process, and the event loop that async tasks share.

Celery's default prefork pool imports the app module in the parent process and forks the children that run
tasks. Anything Bedrock opens before the fork (connection pools, clients created by ``on_load`` hooks, event
loops) would be shared by every child. Bedrock is therefore set up after the fork, in each child, and shut
down when that child exits. Pools without child processes (solo, threads, gevent, eventlet) set Bedrock up
once in the worker process.

Setup runs ``bedrock.asetup()`` on an event loop owned by the worker process. Async tasks run on that same
loop, so async clients opened by lifecycle hooks are usable from tasks.
"""

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import bedrock
from bedrock.exc import ImproperlyConfigured
from celery import signals

_PREFORK_POOL = "celery.concurrency.prefork:TaskPool"


class _WorkerProcess:
    """The event loop a worker process set Bedrock up on, and the thread that drives it."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread_id = threading.get_ident()


_process: _WorkerProcess | None = None


def setup_worker_process(app: str | None = None) -> None:
    """Set Bedrock up in this process on a new event loop that async tasks will share.

    :func:`connect_worker` calls this at the right moment in a worker. Call it directly where no worker
    runs the tasks, for example in a test that executes tasks eagerly. Does nothing if already set up.

    Args:
        app: Dotted import path of the root application module, passed to ``bedrock.asetup()``. When
            *None*, ``BEDROCK_APP`` is read from the environment.

    Raises:
        ImproperlyConfigured: When no app name can be resolved.
        ModuleLifecycleError: When a module fails to load; the event loop is closed again.
    """
    global _process
    if _process is not None:
        return
    process = _WorkerProcess()
    try:
        process.loop.run_until_complete(bedrock.asetup(app))
    except BaseException:
        _close_loop(process.loop)
        raise
    _process = process


def shutdown_worker_process() -> None:
    """Shut Bedrock down on the worker loop, then close the loop. Does nothing if not set up.

    Raises:
        ModuleLifecycleError: When a module or database step failed; the loop is closed regardless.
    """
    global _process
    process, _process = _process, None
    if process is None:
        return
    try:
        process.loop.run_until_complete(bedrock.ashutdown())
    finally:
        _close_loop(process.loop)


def connect_worker(app: str | None = None) -> None:
    """Connect Celery worker signals so each worker process sets Bedrock up and shuts it down.

    Call once in the module that creates the Celery app. With the prefork pool, every child process runs
    :func:`setup_worker_process` after it is forked and :func:`shutdown_worker_process` before it exits; the
    parent process never sets Bedrock up. Other pools set it up once in the worker process.

    Args:
        app: Dotted import path of the root application module. When *None*, ``BEDROCK_APP`` is read from
            the environment when the worker starts.
    """

    def on_worker_init(sender: Any, **kwargs: Any) -> None:
        if not _is_prefork(sender.pool_cls):
            setup_worker_process(app)

    def on_process_init(**kwargs: Any) -> None:
        setup_worker_process(app)

    def on_shutdown(**kwargs: Any) -> None:
        shutdown_worker_process()

    signals.worker_init.connect(on_worker_init, weak=False, dispatch_uid="bedrock_celery.worker_init")
    signals.worker_process_init.connect(on_process_init, weak=False, dispatch_uid="bedrock_celery.process_init")
    signals.worker_process_shutdown.connect(on_shutdown, weak=False, dispatch_uid="bedrock_celery.process_shutdown")
    signals.worker_shutdown.connect(on_shutdown, weak=False, dispatch_uid="bedrock_celery.worker_shutdown")


def run_on_worker_loop[T](make_coro: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run the coroutine built by *make_coro* to completion on the worker process's event loop.

    Args:
        make_coro: Builds the coroutine; called only once the loop is known to be usable.

    Returns:
        The coroutine's result.

    Raises:
        ImproperlyConfigured: When the process was not set up, or the loop is driven by another thread or
            is already running another task (pools that run tasks concurrently).
    """
    process = _process
    if process is None:
        raise ImproperlyConfigured(
            "Async tasks run on the event loop of the worker process, which is not set up. Call "
            "bedrock_celery.connect_worker() in the module that creates the Celery app, or "
            "bedrock_celery.setup_worker_process() when running tasks without a worker."
        )
    if process.thread_id != threading.get_ident() or process.loop.is_running():
        raise ImproperlyConfigured(
            "Async tasks run one at a time on the thread that set the worker process up. Use the prefork or "
            "solo pool for workers that run async tasks."
        )
    return process.loop.run_until_complete(make_coro())


def _is_prefork(pool_cls: object) -> bool:
    """Return whether the worker's pool forks child processes; ``pool_cls`` may still be an alias here."""
    if isinstance(pool_cls, str):
        from celery.concurrency import ALIASES

        return ALIASES.get(pool_cls, pool_cls) == _PREFORK_POOL
    from celery.concurrency.prefork import TaskPool

    return isinstance(pool_cls, type) and issubclass(pool_cls, TaskPool)


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel tasks left on ``loop``, finalize async generators and the default executor, then close it."""
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()
