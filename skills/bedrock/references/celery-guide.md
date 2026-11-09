# Celery Workers (`bedrock-celery`)

`bedrock-celery` is a separate package (`pip install bedrock-celery`), not part of `bedrock-core`. It is an adapter: Celery types stay in `adapters/worker/`, never in `service.py`, `entities.py`, or `bootstrap.py`. Tasks translate arguments and call services.

## Layout

```text
shop/
+-- inventory/            # Bedrock module: no Celery imports
|   +-- service.py
+-- adapters/
    +-- worker/
        +-- app.py        # creates the Celery app, calls connect_worker()
        +-- inventory/
            +-- tasks.py  # tasks call inventory.service
```

## App

```python
# shop/adapters/worker/app.py
from bedrock_celery import BedrockTask, connect_worker
from celery import Celery

app = Celery("shop", broker="redis://localhost:6379/0", task_cls=BedrockTask)
connect_worker("shop.inventory")  # or None to read BEDROCK_APP
```

- `connect_worker(app)` runs `bedrock.setup()` in each process that runs tasks (after the fork with prefork; once in the worker for solo/threads/gevent/eventlet) and `bedrock.shutdown()` when that process exits. Never call `bedrock.setup()` at import time in the app module: under prefork it would open pools in the parent and share them across forked children.
- If `bedrock.setup()` fails, the failure is logged and the worker stops instead of consuming tasks without Bedrock.
- Call `db.init()` (or configure the database through settings) before tasks run; `connect_worker` does not configure logging either.

## Tasks

```python
# shop/adapters/worker/inventory/tasks.py
from shop.adapters.worker.app import app
from shop.inventory import service


@app.task
def restock(sku: str, qty: int) -> int:
    return service.restock(sku, qty)          # uses db.session


@app.task(bind=True)
async def arestock(self, sku: str, qty: int) -> int:
    return await service.arestock(sku, qty)   # uses db.asession; self.request / self.retry() work
```

- `BedrockTask` runs each task inside `container.scope("task")` with a bound session: `db.session` for `def` tasks, `db.asession` for `async def` tasks. `Lifetime.SCOPED` services resolve once per task.
- Services commit; the task never commits for them. Whatever is uncommitted when the task ends is rolled back.
- Because a session is bound, `db.session_scope()` / `db.asession_scope()` raise `SessionAlreadyBoundError` inside a task. Use `db.independent_session()` / `db.aindependent_session()` for side transactions. An `async def` task binds only `db.asession`; reading `db.session` there raises `SessionNotBoundError`.
- `async def` tasks run with `bedrock.run_async()` on Bedrock's runtime loop, so async clients opened by `on_load` hooks stay usable. Never use `asyncio.run()` or `async_to_sync` in tasks.
- Class-based tasks: `run` is classified by the class that defines it. A subclass inheriting an async `run` stays async; one overriding it with `def run` gets a sync session.

## Tests

```python
import bedrock
import pytest


@pytest.fixture
def bedrock_app():
    bedrock.setup("shop.inventory")
    yield
    bedrock.shutdown()


def test_arestock(bedrock_app):
    assert arestock.apply(args=("A1", 3)).get() == 3
```

`task.apply()` runs the task eagerly with the same scope and session handling. If `db.init()` was not called, every `BedrockTask` fails with `DatabaseNotConfiguredError`; the first async task builds the async engine and needs `DATABASE_ASYNC_DRIVER`.

## Not provided

Task discovery, beat schedules, retry policies, and exception mapping stay in the application.
