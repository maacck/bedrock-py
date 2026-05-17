---
name: bedrock
description: >
  Comprehensive guide for building applications with the `bedrock-py` (a modular Python framework).
  Use this skill whenever working with Bedrock modules' manifests, services, entities, database models,
  CLI commands, or any Bedrock-specific patterns. Also use when creating new Bedrock modules,
  debugging module lifecycle issues, writing bootstrap hooks, configuring database migrations,
  or building applications on top of Bedrock. Trigger on: "python project dependencies contains 'bedrock-py'", "working on a Bedrock module", "writing a manifest.yaml", "using bedrock CLI commands"
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

## When to Read References

- **Creating or modifying a module** → Read `references/module-guide.md`
- **Using dependency injection** → Read `references/di-guide.md`
- **Using the hook system** → Read `references/hooks-guide.md`
- **Using database features** → Read `references/database-guide.md`
- **Using CLI commands** → Read `references/cli-guide.md`
- **Using signals / events** → Read `references/signals-guide.md`
- **Understanding project structure** → Read `references/module-hierarchy.md`
- **Understanding architecture / layer boundaries** → Read `references/architecture.md`

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

bedrock.setup("myproject.modules.users")
```

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

First module (`modules/users/manifest.yaml`):

```yaml
title: User Management
version: "0.1.0"
description: User authentication and profiles
```

## Quick Start: New Module

To add a module to an existing Bedrock project:

1. Create `manifest.yaml` with title and version (required)
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
bedrock.setup()  # Reads BEDROCK_APP env var, or pass app path explicitly
bedrock.setup("myproject.app")  # Explicit app path
```

Call once at process start. Safe to call multiple times.

### 2. Module Structure (Standard)

```
<name>/
├── __init__.py          # Package marker
├── manifest.yaml        # Required: title, version, depends_on
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
version: "0.1.0"           # Required: module version
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

### 6. Exception Pattern

Create module-level exceptions by subclassing `BedrockExc`:

```python
from bedrock.exc import BedrockExc


class UserError(BedrockExc):
    """Base exception for user module."""
    detail: str = "User operation failed."

```

Every exception MUST set a `detail` class attribute with a default message. Constructor accepts optional `msg` to override. Create a module-level base exception (e.g. `UserError`) for catching all module errors.

### 7. Bootstrap Hooks

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

Hooks use keyword-only signatures. The registry inspects each hook's parameters and injects only what it declares: `registry` (ModuleRegistry), `app` (AppConfig), `container` (DI container), `hooks` (HookRegistry).

### 8. Database Models

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

### 9. Database Queries

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

**Filter operators**: `==`, `!=`, `>`, `<`, `>=`, `<=`, `like`, `ilike`, `in`, `not_in`, `between`, `has`, `any`, `text_search`, `fuzzy_search`

**Boolean combinators**: `or`, `and`

```json
{"field": "name", "op": "==", "value": "Alice"}
{"or": [{"field": "age", "op": ">", "value": 30}, {"field": "name", "op": "==", "value": "Bob"}]}
```

**Nested filters**: Use `.` (join) or `:` (any/has) notation for related models.

### 10. Settings

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


class DbSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATABASE_")
    URL: str = "sqlite:///app.db"
    ECHO: bool = False
```

**Rules**:
- Use `SettingsProxy` for module-level singletons that may be imported before env vars are ready
- Use `BaseSettings` directly when env vars are guaranteed ready at construction time
- Always use `env_prefix` in `model_config` to namespace environment variables

### 11. Signal System

Bedrock provides lifecycle signals for cross-module notification (fire-and-forget):

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

### 12. Hook System (Call/Response)

For structured, multi-implementation extension points that return values, use the hook system instead of signals:

```python
from bedrock.hooks import HookNamespace

auth = HookNamespace("auth")

@auth.spec(firstresult=True)
def authenticate(request):
    """Hook spec: first non-None result wins."""

@auth.impl(priority=10)
def default_auth(request):
    return verify_token(request.token)

# Dispatch
request = {"token": "..."}
results = auth.call("authenticate", request=request)
```

**Signals vs Hooks**: Signals are notification-only (no return value). Hooks are call/response (implementations return values, ordered by priority, with optional `firstresult` short-circuit).

## Anti-Patterns (Avoid These)

1. **Don't put HTTP types in service/entity layers** — keep Request/Response at adapter boundaries or in separate `schemas.py`
2. **Don't subclass `BedrockExc` without `detail`** — error messages depend on it
3. **Don't bypass `ModuleRegistry.install()`** to load modules manually — dependency resolution and hooks will be skipped
4. **Don't call `db.init()` or `apps.populate()` more than once** per process

## Commands Reference

See `references/cli-guide.md` for the complete CLI command reference.

Key commands:
`<module>` is app import path (e.g. `my_erp.oms`)
- `bedrock run --app <module>` — Run app commands
- `bedrock manage install` — Install modules with migrations + hooks
- `bedrock db revision <module> -m "msg"` — Create migration
- `bedrock db upgrade <module>` — Apply migrations
- `bedrock app info <module>` — Inspect module
- `bedrock app playbook <module>` — Inspect module
