"""Celery task base class that gives each task its own Bedrock DI scope and database session."""

import functools
import inspect
from collections.abc import Callable
from typing import Any, ClassVar

from bedrock.database import db
from bedrock.di import container
from celery import Task

from .worker import run_on_worker_loop


class BedrockTask(Task):
    """Run each task inside ``container.scope("task")`` with a database session bound for it.

    Use it as the app's task class (``Celery(..., task_cls=BedrockTask)``) or per task (``base=BedrockTask``).

    A ``def`` task runs inside ``db.session_scope()``; an ``async def`` task runs on the worker process's event
    loop inside ``db.asession_scope()``. Either way the task never commits on its behalf: services work in
    ``db.session`` / ``db.asession`` and commit, and whatever is still uncommitted when the task ends is rolled
    back. Because a session is bound, ``db.session_scope()`` / ``db.asession_scope()`` raise ``RuntimeError``
    inside a task; use ``db.independent_session()`` / ``db.aindependent_session()`` for side transactions.
    """

    _runs_async: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Turn an ``async def`` ``run`` into a sync one that runs it on the worker loop.

        Celery calls ``run`` synchronously, with the task's request pushed for the calling thread. Running the
        coroutine to completion inside that call keeps ``self.request`` and ``self.retry()`` working.
        """
        super().__init_subclass__(**kwargs)
        raw = cls.__dict__.get("run")
        fn = raw.__func__ if isinstance(raw, staticmethod) else raw
        if not inspect.iscoroutinefunction(fn):
            return
        runner = _run_with_async_session(fn)
        cls.run = staticmethod(runner) if isinstance(raw, staticmethod) else runner
        cls._runs_async = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the task inside its DI scope and database session.

        Raises:
            DatabaseNotConfiguredError: When ``db.init()`` has not been called in the worker process.
            ImproperlyConfigured: For an async task when the worker process was not set up, or when the pool
                runs tasks concurrently.
        """
        if self._runs_async:
            with container.scope("task"):
                return super().__call__(*args, **kwargs)
        with container.scope("task"), db.session_scope() as session:
            try:
                return super().__call__(*args, **kwargs)
            finally:
                # Discard uncommitted work so the scope's own commit has nothing left.
                session.rollback()


def _run_with_async_session(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap the coroutine function *fn* so a sync call runs it on the worker loop with a bound async session."""

    async def in_session(*args: Any, **kwargs: Any) -> Any:
        async with db.asession_scope() as session:
            try:
                return await fn(*args, **kwargs)
            finally:
                await session.rollback()

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        return run_on_worker_loop(lambda: in_session(*args, **kwargs))

    return run
