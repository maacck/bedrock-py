# bedrock-celery

Celery integration for [Bedrock](https://bedrock-py.com). Your application creates and owns the Celery app;
this package supplies pieces you add as needed:

- `connect_worker(app)`: sets Bedrock up in each worker process (after the fork with the prefork pool) and
  shuts it down when the process exits.
- `BedrockTask`: each task runs in its own Bedrock DI scope and database session. Services commit; uncommitted
  work is rolled back when the task ends. `async def` tasks run on the worker process's event loop with
  `db.asession`.
- `setup_worker_process(app)` / `shutdown_worker_process()`: the same lifecycle without a worker, for tests.

```python
from bedrock.database import db
from bedrock_celery import BedrockTask, connect_worker
from celery import Celery

app = Celery("shop", broker="redis://localhost:6379/0", task_cls=BedrockTask)
db.init()
connect_worker("shop.inventory")
```

See the Celery integration docs at https://bedrock-py.com/docs/integrations/celery for installation, the
transaction convention, async tasks, and testing.
