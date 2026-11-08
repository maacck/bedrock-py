# Testing Guide

**Package**: `bedrock.testing` (pytest plugin; pytest is not a `bedrock-core` dependency)

Enable once in the project's root `conftest.py`:

```python
pytest_plugins = ["bedrock.testing"]
```

| Fixture | Use for | Context manager behind it |
|---|---|---|
| `bedrock_runtime` | Tests that call `bedrock.setup()` / `asetup()` or assert lifecycle order | `isolated_runtime()` |
| `module_factory` | Temporary Bedrock modules (manifest, bootstrap, models, any file) | `ModuleFactory` |
| `db_session` | Database writes that must not survive the test | `rollback_session()` / `arollback_session()` |
| `clean_container`, `di_container`, `override_service` | DI isolation (`references/di-guide.md`) | — |
| `clean_hooks`, `hook_registry`, `hook_namespace` | Hook isolation (`references/hooks-guide.md`) | — |

Do not hand-roll replacements: no `monkeypatch.setattr(lifecycle, "apps", ...)`, no `db.set_session()` rollback
fixtures, no hand-written temporary packages on `sys.path`.

## `bedrock_runtime`

```python
import bedrock


def test_orders_start_after_billing(bedrock_runtime, module_factory):
    module_factory("billing")
    module_factory("orders", manifest={"title": "Orders", "version": "1.0.0", "depends_on": ["billing"]})

    bedrock.setup("orders")

    loaded = [event.module for event in bedrock_runtime.events if event.kind == "loaded"]
    assert loaded == ["billing", "orders"]
```

- During the test every alias of the global registry (`bedrock.apps`, `bedrock.module.apps`, `bedrock.lifecycle.apps`)
  is `bedrock_runtime.registry`, a fresh `ModuleRegistry`. Real entrypoints work and are never short-circuited.
- `bedrock_runtime.events`: `list[LifecycleEvent(kind, module)]`. Kinds in emission order for one module:
  `loaded`, `ready`, `registry_ready`, `shutdown`, `registry_shutdown`. `module` is the import path; `None` for
  `registry_*`. Rollback of a failed install appears as `shutdown`. Only this registry's events are recorded.
- Teardown (pass or fail), every step attempted, first failure raised afterwards:
  1. `registry.shutdown()` if any module is not shut down;
  2. remove receivers connected to `bedrock.module.signals` during the test;
  3. `container.reset()` and `hooks.reset()`;
  4. restore `os.environ`;
  5. drop the cache of every `SettingsProxy` (also done at start, so `monkeypatch.setenv` reaches settings an earlier
     test loaded);
  6. restore the original global registry.
- Async tests: `await bedrock.asetup(...)`, then `await bedrock_runtime.registry.ashutdown()` before returning.
  Teardown has no event loop and can only call the sync `shutdown()`.
- `bedrock.shutdown()` / `ashutdown()` also close the database (as in production). Inside tests prefer
  `bedrock_runtime.registry.shutdown()` so a session-scoped `db` stays usable.
- Hooks must use the injected `registry` keyword; module-level `from bedrock import apps` keeps the old object.
- A module whose `on_shutdown` failed is not shut down; teardown retries it. Tests that inject shutdown failures must
  stop injecting them before the test ends, or teardown reports the failure.

## `module_factory`

`module_factory(name, *, manifest=None, bootstrap=None, models=None, files=None) -> str`

- `manifest`: written to `manifest.yaml`, replaces the default `{"title": name, "version": "0.1.0"}` entirely.
- `bootstrap` / `models`: source for `bootstrap.py` / `models.py`. `files`: `{relative_path: source}` for anything
  else (`installation.py`, `commands.py`, `domains/x/service.py`). Sources are dedented.
- `name`: plain identifier (no dots), unique within the test; otherwise `ValueError`.
- `module_factory.root`: directory holding the packages (on `sys.path`).
- Teardown removes the packages from `sys.modules` and their models from `BedrockModel.metadata` and the mapper
  registry, so the next test can reuse the same module name and model classes without SQLAlchemy warnings.

## Database rollback

```python
# conftest.py
@pytest.fixture(scope="session", autouse=True)
def _database():
    db.init(url="postgresql+psycopg://app:app@localhost:5432/app_test")
    BedrockModel.metadata.create_all(db.engine)
    yield
    db.close()


# test
def test_place_order(db_session):
    place_order(sku="A1")  # service uses db.session_scope() and commits
    assert db_session.scalar(select(Order).where(Order.sku == "A1")) is not None
```

- All sessions from `db` (`db.session`, `db.session_scope()`, `db.independent_session()`) share one connection
  inside an outer transaction; `commit()` commits a savepoint; the outer transaction rolls back at the end.
- The yielded session is not bound to `db.session`, so service code may open `db.session_scope()`.
- SQLite is handled (the test connection emits explicit `BEGIN`, restored afterwards).
- Limits: `independent_session()` sees the caller's uncommitted data; `db.engine.connect()` bypasses the rollback;
  calling `db.init()` in the test ends isolation; uninitialized `db` raises `DatabaseNotConfiguredError`.
- Async (no async fixture is shipped, to avoid depending on pytest-asyncio):

```python
@pytest_asyncio.fixture
async def adb_session():
    async with arollback_session() as session:
        yield session
```

- Both context managers accept another manager: `rollback_session(manager)`.

## Not covered yet

Fakes for `cache`, `storage`, and `metrics` contrib services. Do not assign their private `_backend`; configure a
registered backend (for example `CACHE_BACKEND=memory`) instead.
