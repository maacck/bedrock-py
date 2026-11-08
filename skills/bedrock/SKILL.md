---
name: bedrock
description: >
  Comprehensive guide for building applications with the `bedrock-py` (a modular Python framework).
  Use this skill whenever working with Bedrock modules' manifests, services, entities, database models,
  CLI commands, or any Bedrock-specific patterns. Also use when creating new Bedrock modules,
  debugging module lifecycle issues, writing bootstrap hooks, configuring database migrations,
  writing tests for Bedrock modules, or building applications on top of Bedrock. Trigger on: "python project dependencies contains 'bedrock-py'", "working on a Bedrock module", "writing a manifest.yaml", "using bedrock CLI commands", "testing a Bedrock module"
---

# Bedrock Application Development

Bedrock is a modular Python framework for building applications with manifest-driven module loading, lifecycle orchestration, and clean architecture conventions. This skill teaches you how to use Bedrock to build maintainable, extensible applications.

## Quick Reference

| Concern | Singleton | Import |
|---------|-----------|--------|
| Module Registry | `apps` | `from bedrock.module import apps` |
| Database | `db` | `from bedrock.database import db` |
| DI Container | `container` | `from bedrock.di import container` |
| Hook Registry | `hooks` | `from bedrock.hooks import hooks` |
| Settings | `settings` | `from bedrock.settings import settings` |

Named implementation classes: `from bedrock.common.registry import ClassRegistry` (a class, not a singleton).
Named providers (instances or factories, lazy paths, install hints): `from bedrock.provider import ProviderCatalog` (a class, not a singleton).
Test fixtures (isolated runtime, temporary modules, DB rollback): `pytest_plugins = ["bedrock.testing"]` in the root `conftest.py`; see section 14.

## When to Read References

- **Creating or modifying a module** → Read `references/module-guide.md`
- **Using dependency injection** → Read `references/di-guide.md`
- **Selecting named implementation classes** → Read `references/class-registry-guide.md`
- **Selecting named providers / lazy optional implementations** → Read `references/provider-guide.md`
- **Using the hook system** → Read `references/hooks-guide.md`
- **Using database features** → Read `references/database-guide.md`
- **Using CLI commands** → Read `references/cli-guide.md`
- **Using signals / events** → Read `references/signals-guide.md`
- **Understanding project structure** → Read `references/module-hierarchy.md`
- **Understanding architecture / layer boundaries** → Read `references/architecture.md`
- **Writing tests: isolated runtime, temporary modules, DB rollback** → Read `references/testing-guide.md`
- **Using caching / distributed locks** → View `bedrock app playbook bedrock.contrib.cache`
- **Instrumenting metrics** → View `bedrock app playbook bedrock.contrib.metric`
- **Using file/object storage** → View `bedrock app playbook bedrock.contrib.storage`

## Contrib Packages

Bedrock ships first-party contrib packages, each documented by a playbook (view with `bedrock app playbook <import_path>`):

| Package | Import | Purpose |
|---------|--------|---------|
| Cache | `bedrock.contrib.cache` | In-memory / Redis cache with TTL, distributed locks, typed namespaces |
| Metrics | `bedrock.contrib.metric` | Counter/gauge/timer instrumentation with StatsD, Sentry, and Prometheus providers |
| Storage | `bedrock.contrib.storage` | File/object storage with local and S3-compatible backends |

## Quick Start: New Project

Create a Bedrock application with this structure:

```
myproject/
├── __init__.py
├── app.py             
├── settings.py         # Application settings
├── manifest.yaml
├── entities.py
├── service.py
├── exc.py
└── bootstrap.py
```

```python
import bedrock
from bedrock.logging import configure_logging

configure_logging()
bedrock.setup("myproject")
```

`configure_logging()` belongs in the process entry point. Do not call it at import time inside a module that other code imports.

Settings (`settings.py`):

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from bedrock.conf import SettingsProxy


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYAPP_")
    DEBUG: bool = False
    DATABASE_URL: str = "sqlite:///app.db"


app_settings: AppSettings = SettingsProxy(AppSettings)  # type: ignore[assignment]
```

Application manifest (`myproject/manifest.yaml`):

```yaml
title: User Management
version: "0.1.0"
description: User authentication and profiles
```

## Quick Start: New Module

To add a module to an existing Bedrock project:

1. Create `manifest.yaml` with a title (version defaults to `"unknown"`)
2. Create `__init__.py` package marker (required)
3. Create `entities.py` for domain data models
4. Create `exc.py` for module exceptions
5. Create `service.py` for business logic
6. Create `bootstrap.py` for lifecycle hooks (optional)
7. Create `models.py` for database tables (optional)
8. Create `installation.py` for install hooks (optional)

## Core Workflow

### 1. Bootstrap the Application

Every Bedrock process starts with `bedrock.setup()`:

```python
import bedrock
from bedrock.logging import configure_logging

configure_logging()
bedrock.setup()  # Reads BEDROCK_APP env var, or pass app path explicitly
bedrock.setup("myproject.app")  # Explicit app path
```

Call `bedrock.setup()` once at process start. It is safe to call multiple times, and it does not configure logging. In an async process call `await bedrock.asetup()` and stop with `await bedrock.ashutdown()` (sync: `bedrock.shutdown()`), which stops modules in reverse order and then releases the initialized database. Use one family end to end; see `references/module-guide.md` (Async lifecycle).

Importing Bedrock does not remove loguru handlers or install Bedrock's sink. `configure_logging()` installs Bedrock's stderr sink (or `LOG_FILE` when that variable is set) and, on a later call, replaces only that sink. Handlers already present, including Loguru's preconfigured stderr handler, stay. `LOG_LEVEL`, `LOG_FORMAT`, `LOG_FILE`, `LOG_ROTATION`, `LOG_RETENTION`, `LOG_COMPRESSION`, and `LOG_SERIALIZE` are read when it runs. The `bedrock` CLI calls `configure_cli_logging()` before dispatching commands. That removes Loguru's preconfigured stderr handler so CLI lines are not duplicated and `LOG_*` controls CLI stderr; host handlers stay. Commands inside a module do not need to configure logging.

### 2. Module Structure (Standard)

```
<name>/
├── __init__.py          # Package marker
├── manifest.yaml        # Required: title; optional: version, depends_on
├── entities.py          # Pydantic models (extend BedrockEntity)
├── service.py           # Business logic (sync + async pairs)
├── exc.py               # Module exceptions (extend BedrockExc)
├── bootstrap.py         # Lifecycle hooks: on_load, ready, on_shutdown
├── models.py            # SQLAlchemy models (extend BedrockModel) — optional
├── installation.py      # install(), pre_install(), post_install() hooks — optional
└── commands.py          # Typer CLI app — optional
```

Not every module needs every file. Minimum viable module: `__init__.py` + `manifest.yaml`.

### 3. Manifest Schema

```yaml
title: My Module           # Required: display name
version: "0.1.0"           # Optional: defaults to "unknown"
description: Short desc    # Optional
depends_on:                # Optional: import paths of dependencies
  - myproject.users
commands: commands:app      # Optional: relative Typer app import path
```

The `name` at runtime is the Python import path (e.g. `myproject.modules.users`), NOT a field in the manifest. Dependencies use fully qualified import paths.

### 4. Entity Pattern

Extend `BedrockEntity` for all domain data models:

```python
from bedrock.entities import BedrockEntity


class UserCreateRequest(BedrockEntity):
    name: str
    email: str
    password: str


class UserResponse(BedrockEntity):
    id: int
    name: str
    email: str
    is_active: bool = True
```

`BedrockEntity` is a Pydantic `BaseModel` with `arbitrary_types_allowed=True`. Use Pydantic v2 syntax.

### 5. Exception Pattern

Create module-level exceptions by subclassing `BedrockExc`:

```python
from bedrock.exc import BedrockExc


class UserError(BedrockExc):
    """Base exception for user module."""
    detail: str = "User operation failed."

```

Every exception MUST set a `detail` class attribute with a default message. Constructor accepts optional `msg` to override. Create a module-level base exception (e.g. `UserError`) for catching all module errors.

### 6. Bootstrap Hooks

Lifecycle hooks execute at specific points during module loading. Bedrock always calls hooks with keyword arguments — use keyword-only signatures:

```python
# bootstrap.py
from bedrock.module import ModuleRegistry, AppConfig


def on_load(*, registry: ModuleRegistry, app: AppConfig) -> None:
    """Called during install(), after module is added."""
    pass


def ready(*, registry: ModuleRegistry, app: AppConfig) -> None:
    """Called after ALL modules are installed."""
    from .service import user_service
    user_service.initialize()


def on_shutdown(*, registry: ModuleRegistry, app: AppConfig) -> None:
    """Called during shutdown, in REVERSE install order."""
    pass
```

| Hook | When Called | Use Case                                |
|------|------------|-----------------------------------------|
| `on_load` | During `install()`, after module is added | Early initialization, dependency checks |
| `ready` | After ALL modules are installed | Configure services, initialization      |
| `on_shutdown` | During `shutdown()`, in REVERSE order | Cleanup, close connections              |

Hooks should use keyword-only signatures. The registry injects the supported parameters they declare: `registry` (ModuleRegistry), `app` (AppConfig), `container` (DI container), `hooks` (HookRegistry); declare at least one or `**kwargs`.

Installation is failure-atomic: newly introduced modules are removed on failure, with best-effort reverse shutdown of modules that completed `on_load`. `populate()` rolls back its entire installation phase; a later readiness failure leaves modules installed for retry. Modules track `ModuleLifecycleState` (`loading`, `loaded`, `ready`, `shutdown`). Shutdown attempts all modules and reports failures in `ModuleLifecycleError.failures`; rollback cleanup failures raise `ModuleStartupRollbackError`. `mark_ready()` and `shutdown()` are not re-entrant. See `references/module-guide.md`.

### 7. Database Models

Extend `BedrockModel` for database tables:

```python
from bedrock.database.base import BedrockModel
from sqlalchemy import String, Boolean
from sqlalchemy.orm import Mapped, mapped_column


class UserModel(BedrockModel):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
```

`BedrockModel` provides these methods on all instances:
- `.dict()` — Serialize to dict
- `.update(**kwargs)` — Update fields
- `.delete()` — Delete instance

### 8. Database Queries

Use `search_filter_sort_paginate` for filtered, paginated queries:

```python
from bedrock.database import db
from bedrock.database.service import search_filter_sort_paginate

result = search_filter_sort_paginate(
    db_session=db.session,
    model=UserModel,
    filter_specs=[{"field": "name", "op": "==", "value": "Alice"}],
    sort_key="name",
    sort_dir="asc",
    page=1,
    limit=10,
)
# Returns: {"items": [...], "total": N, "page_info": {"page": 1, "limit": 10, "has_more": True}}
```

**Filter operators**: `is_null`, `is_not_null`, `==`/`eq`, `!=`/`neq`, `>`/`gt`, `<`/`lt`, `>=`/`ge`, `<=`/`le`, `like`, `ilike`, `not_ilike`, `in`, `not_in`, `between`, `has`, `any`, `not_any`, `text_search`, `fuzzy_search`. `limit` must be an integer from 1 to 1000 inclusive, even with `show_all=True`.

**Boolean combinators**: `or`, `and`

```json
{"field": "name", "op": "==", "value": "Alice"}
{"or": [{"field": "age", "op": ">", "value": 30}, {"field": "name", "op": "==", "value": "Bob"}]}
```

**Nested filters**: `.` traverses relationships via joins; `:` selects any/has but the current formatter can fail on relationship type lookup. Prefer explicit SQLAlchemy relationship clauses in `sqla_filters`; see `references/database-guide.md`.

### 9. Settings

Create module-level settings using `BaseSettings`. Wrap with `SettingsProxy` when you need deferred initialization (module-level singletons where env vars may not be ready at import time):

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from bedrock.conf import SettingsProxy


class MyModuleSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYMODULE_")
    API_KEY: str = ""
    DEBUG: bool = False
    MAX_RETRIES: int = 3


my_settings: MyModuleSettings = SettingsProxy(MyModuleSettings)  # type: ignore[assignment]
```

When environment variables are guaranteed to be ready at construction time (e.g. inside a `ready()` hook), use `BaseSettings` directly:

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class MyDbSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYDB_")
    URL: str = "sqlite:///app.db"
    ECHO: bool = False
```

**Rules**:
- Use `SettingsProxy` for module-level singletons that may be imported before env vars are ready
- Use `BaseSettings` directly when env vars are guaranteed ready at construction time
- Always use `env_prefix` in `model_config` to namespace environment variables

### 10. Signal System

Signals announce that something happened; the sender must not depend on who listens or what they return (see section 13):

```python
from bedrock.signal import Signal

# Define a custom signal
user_created = Signal("user_created")

# Connect receivers (decorator form)
@user_created.connect
def on_user_created(sender, user):
    print(f"User created: {user.name}")

# Connect receivers (explicit form)
def on_user_created(sender, user):
    print(f"User created: {user.name}")

user_created.connect(on_user_created)

# Send signal (sync) — in pure sync context, adapts async receivers
user_created.send(sender, user=new_user)

# Send signal (async) — canonical for mixed sync/async receivers
await user_created.asend(sender, user=new_user)
```

> **Sync/async contract:** `send()` is sync-facing. In a pure sync context it can adapt async receivers. Inside a running event loop, it raises `RuntimeError` if an async receiver is reached. Use `await asend()` for async or mixed contexts. See `references/signals-guide.md` for details.

**Built-in lifecycle signals**:

```python
from bedrock.module.signals import (
    module_loaded,     # After each module's on_load hook
    module_ready,      # After each module's ready hook
    module_shutdown,   # After each module's on_shutdown hook
    registry_ready,    # After all modules ready
    registry_shutdown, # When registry shuts down
)
```

**Common patterns**:

| Signal | Use Case |
|--------|----------|
| `module_loaded` | React to a specific module being loaded |
| `registry_ready` | Run setup that needs ALL modules available |
| `module_shutdown` | Coordinate cleanup across modules |

### 11. Hook System (Call/Response)

For structured, multi-implementation extension points that return values, use the hook system instead of signals:

```python
from bedrock.hooks import HookNamespace

auth = HookNamespace("auth")

@auth.spec(firstresult=True)
def authenticate(request):
    """Hook spec: first non-None result wins."""

@auth.impl(priority=10)
def authenticate(request):
    return verify_token(request["token"])

# Dispatch
request = {"token": "..."}
results = auth.call("authenticate", request=request)
```

**Signals vs Hooks vs DI**: see section 13.

### 12. Named Implementation Classes

Use a class registry to choose implementations by name; use DI for instances and lifetimes:

```python
from bedrock.common.registry import ClassRegistry

class Codec:
    pass

class Utf8Codec(Codec):
    pass

codecs = ClassRegistry[Codec](base_class=Codec)
codecs.register("utf8", Utf8Codec)
codec = codecs.get("utf8")()
```

Register extensions explicitly in `bootstrap.py:on_load`. See `references/class-registry-guide.md` for lazy registration and errors. When callers need validated instances, factory functions, structural implementations, or install hints for optional SDKs, use `bedrock.provider.ProviderCatalog` instead; see `references/provider-guide.md`.

### 13. Choosing a Mechanism

Pick the first row that fits:

| Need | Use |
|------|-----|
| Call a module already declared in `depends_on` | Direct import of its `service.py`; no mechanism |
| Share one instance, swap it in tests, or control its lifetime (`SINGLETON` / `SCOPED` / `TRANSIENT`) | DI `container` |
| Select one implementation **class** by name and use the class itself (codec, type dispatch) | `ClassRegistry` |
| Select one **provider** by name and get a validated instance or its factory (gateway, exporter, optional SDK) | `ProviderCatalog` (`bedrock.provider`) |
| Owner defines an extension point; other modules contribute behavior; the caller uses results, ordering, or first match | Hooks (`HookNamespace`) |
| Announce that something happened; the sender must not depend on listeners or their results | Signals |

Failure and execution semantics often decide between hooks and signals:

| | Hooks | Signals |
|---|-------|---------|
| Order | Priority (lower runs first) | Unspecified |
| On first exception | `call()` / `acall()` raise it; later implementations do not run; earlier side effects stay | `send()` / `asend()` raise it; remaining receivers do not run |
| Run all, collect exceptions | `call_robust()` (sync only) | `send_robust()` / `asend_robust()` |
| Async | `acall()` (sync implementations run in a thread) | `asend()`; lifecycle signals always use sync `send()` |

**Every implementation must succeed** → hooks with `firstresult=False` (default); implementations signal failure by raising a `BedrockExc` subclass, never by returning `False`. Hooks provide no rollback: see `references/hooks-guide.md` → "Extension Points That Must All Succeed" for fail-fast with DB rollback, collect-all reporting, and external side effects.

### 14. Testing

`bedrock.testing` is a pytest plugin. Enable it once in the project's root `conftest.py` with `pytest_plugins = ["bedrock.testing"]`, then request fixtures by name:

| Fixture | Gives the test |
|---------|----------------|
| `bedrock_runtime` | A fresh module registry that `bedrock.setup()` / `asetup()` act on; `.registry`, `.events` (lifecycle `LifecycleEvent(kind, module)`); teardown shuts modules down and restores signals, DI, hooks, env, settings |
| `module_factory` | `module_factory(name, *, manifest=None, bootstrap=None, models=None, files=None)` writes a temporary Bedrock module; removed (with its models) after the test |
| `db_session` | A session whose writes, including `commit()` inside `db.session_scope()`, are rolled back after the test; `db.init()` must run in a session-scoped fixture first |
| `clean_container`, `override_service`, `clean_hooks`, … | DI and hook isolation |

```python
import bedrock


def test_orders_need_billing(bedrock_runtime, module_factory, db_session):
    module_factory("billing")
    module_factory("orders", manifest={"title": "Orders", "version": "1.0.0", "depends_on": ["billing"]})

    bedrock.setup("orders")

    assert [e.module for e in bedrock_runtime.events if e.kind == "loaded"] == ["billing", "orders"]
```

Async tests: `await bedrock.asetup(...)` and `await bedrock_runtime.registry.ashutdown()` before returning; for async DB rollback wrap `arollback_session()` in your own `pytest_asyncio` fixture. Prefer `bedrock_runtime.registry.shutdown()` over `bedrock.shutdown()` in tests, because the latter also closes `db`. See `references/testing-guide.md` for teardown order, limits, and SQLite notes.

## Anti-Patterns (Avoid These)

1. **Don't put HTTP types in service/entity layers** — keep Request/Response at adapter boundaries or in separate `schemas.py`
2. **Don't subclass `BedrockExc` without `detail`** — error messages depend on it
3. **Don't bypass `ModuleRegistry.install()`** to load modules manually — dependency resolution and hooks will be skipped
4. **Don't recursively call `mark_ready()` or `shutdown()`** from lifecycle hooks or signal receivers
5. **Don't register extensions via import-time side effects or accept import paths from requests** — register trusted paths in `on_load`
6. **Don't hand-roll test isolation** — no `monkeypatch.setattr(lifecycle, "apps", ...)`, `db.set_session()` rollback fixtures, or hand-written packages on `sys.path`; use `bedrock_runtime`, `db_session`, and `module_factory`

## Commands Reference

See `references/cli-guide.md` for the complete CLI command reference.

Key commands:
`<module>` is app import path (e.g. `my_erp.oms`)
- `bedrock run --app <app> <module> <command>` — Run a command from `<module>`'s Typer app (commands are grouped by module import path)
- `bedrock manage install` — Install modules with migrations + hooks
- `bedrock db revision <module> -m "msg"` — Create migration
- `bedrock db upgrade <module>` — Apply migrations (PostgreSQL/MySQL take a migration lock; SQLite is single-process)
- `bedrock db check <module>` — Exit non-zero unless every loaded branch is at head
- `bedrock app info <module>` — Inspect module
- `bedrock app playbook <module>` — View module playbook
