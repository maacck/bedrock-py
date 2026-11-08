# Module Creation & Lifecycle Guide

Complete guide to creating and structuring Bedrock modules for application development.

## Table of Contents

1. [Module Directory Structure](#module-directory-structure)
2. [Manifest Schema](#manifest-schema)
3. [Entity Pattern](#entity-pattern)
4. [Exception Pattern](#exception-pattern)
5. [Bootstrap Hooks](#bootstrap-hooks)
6. [Signal System](#signal-system)
7. [Settings Pattern](#settings-pattern)
8. [Installation Hooks](#installation-hooks)
9. [CLI Commands](#cli-commands)

---

## Module Directory Structure

```
<name>/
├── __init__.py          # Package marker (can re-export public API)
├── manifest.yaml        # Required: module metadata
├── entities.py          # Pydantic models (extend BedrockEntity)
├── service.py           # Business logic (sync + async pairs)
├── exc.py               # Module exceptions (extend BedrockExc)
├── bootstrap.py         # Lifecycle hooks: on_load, ready, on_shutdown
├── models.py            # SQLAlchemy models (extend BedrockModel) — optional
├── installation.py      # install(), pre_install(), post_install() hooks — optional
└── commands.py          # Typer CLI app — optional
```

Not every module needs every file. Minimum viable module: `__init__.py` + `manifest.yaml`.

---

## Manifest Schema

**File**: `manifest.yaml` at module package root

### Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `title` | `str` | Yes | — | Human-friendly display name |
| `version` | `str` | No | `"unknown"` | Module version string |
| `description` | `str \| None` | No | `None` | Short description |
| `depends_on` | `list[str]` | No | `[]` | Import paths of dependencies |
| `commands` | `str \| None` | No | `None` | Relative Typer app import path (e.g. `commands:app`) |

### Example

```yaml
title: User Management
version: "0.1.0"
description: User authentication and profile management
depends_on:
  - myproject.core
commands: commands:app
```

### Key Notes

- The module `name` at runtime is the **Python import path** (e.g. `myproject.modules.users`), NOT a field in the manifest
- `depends_on` values are fully qualified import paths (e.g. `myproject.core`, not `core`)
- `commands` uses colon syntax: `module_part:attribute` (e.g. `commands:app`)

---

## Entity Pattern

**File**: `entities.py`
**Base class**: `BedrockEntity` from `bedrock.entities`

`BedrockEntity` is a Pydantic `BaseModel` with `arbitrary_types_allowed=True`. All domain entities extend it.

### Example

```python
from bedrock.entities import BedrockEntity


class UserBase(BedrockEntity):
    name: str
    email: str


class UserEntity(BedrockEntity):
    id: int
    is_active: bool = True

```


### Rules

- All domain entities MUST extend `BedrockEntity`
- Use Pydantic v2 syntax (`model_config = ConfigDict(...)`, not `class Config`)
- Entities are pure data models — no business logic, no HTTP types
- Use `| None` for optional fields with default `None`
- Write Model that is mainly for validation and data transfer to `scehmas.py`.


## Schemas Pattern

**File**: `schemas.py`
**Base class**: `BedrockEntity` from `bedrock.entities`

`BedrockEntity` is a Pydantic `BaseModel` with `arbitrary_types_allowed=True`. All domain entities extend it.

### Example

```python
from bedrock.entities import BedrockEntity

class UserCreate(BedrockEntity):
    name: str
    email: str
    password: str
    
class UserUpdate(BedrockEntity):
    name: str | None = None
    email: str | None = None
    password: str | None = None
```

---

## Exception Pattern

**File**: `exc.py`
**Base class**: `BedrockExc` from `bedrock.exc`

`BedrockExc` is an `Exception` subclass with a `detail` class attribute and an `__init__` that accepts an optional `msg` to override it.

### Example

```python
from bedrock.exc import BedrockExc


class UserError(BedrockExc):
    """Base exception for user module."""
    detail: str = "User operation failed."


class UserNotFoundError(UserError):
    """Raised when user is not found."""
    detail: str = "User not found."


class DuplicateEmailError(UserError):
    """Raised when email already exists."""
    detail: str = "Email already registered."


class InvalidPasswordError(UserError):
    """Raised when password validation fails."""
    detail: str = "Invalid password."
```

### Usage

```python
# Raise with default detail
raise UserNotFoundError()  # → "User not found."

# Raise with custom message
raise UserNotFoundError("User with ID 42 not found")

# Catch all module errors
try:
    user_service.get_user(42)
except UserError as e:
    print(e.detail)
```

### Rules

- Every exception MUST set a `detail` class attribute with a default message
- Constructor accepts optional `msg` to override `detail`
- Create a module-level base exception (e.g. `UserError`) for catching all module errors
- Use specific subclasses for distinct error conditions

---

## Bootstrap Hooks

**File**: `bootstrap.py`
**Contract**: Bedrock always calls hooks with keyword arguments. Use keyword-only signatures.

### Available Hooks

| Hook | When Called | Use Case |
|------|------------|----------|
| `on_load` | During `install()`, after module is added | Early initialization, dependency checks |
| `ready` | After ALL modules are installed | Configure services, start background tasks |
| `on_shutdown` | During `shutdown()` / `ashutdown()`, in REVERSE order | Cleanup, close connections |

### Available Named Parameters

The registry inspects each hook's signature and injects only the parameters it declares:

| Parameter | Type | Description |
|-----------|------|-------------|
| `registry` | `ModuleRegistry` | The global module registry |
| `app` | `AppConfig` | This module's configuration |
| `container` | `Container` | The global DI container (`from bedrock.di`) |
| `hooks` | `HookRegistry` | The global hook registry (`from bedrock.hooks`) |

### Example

```python
def on_load(*, registry, app) -> None:
    """Called during module installation."""
    if not registry.is_installed("myproject.core"):
        raise RuntimeError("myproject.core must be installed first")


def ready(*, registry, app, container, hooks) -> None:
    """Called after all modules are installed."""
    from .service import user_service
    user_service.initialize()
    # Register services in the DI container
    container.register(IUserRepo, factory=UserRepo, lifetime=Lifetime.SINGLETON)


def on_shutdown(*, registry, app) -> None:
    """Called during shutdown."""
    from .service import user_service
    user_service.cleanup()
```

### Rules

- Use keyword-only signatures (`def on_load(*, registry, app)`) — Bedrock always passes keyword arguments
- Declare at least one supported parameter or `**kwargs`; the registry injects the declared supported parameters, or all four for `**kwargs`
- Hooks are called by the registry — do NOT import or call them manually
- `ready` is the most common hook for service initialization
- `on_shutdown` runs in REVERSE install order (last installed = first shutdown)
- Hooks and signal receivers may be plain functions or `async def`; a hook that returns an awaitable is awaited. See `Async lifecycle` below

### Lifecycle failures and retries

`install()` is failure-atomic: it removes modules introduced by the operation and calls `on_shutdown` in reverse order for those that completed `on_load`. Pre-existing modules remain installed. The installation phase of `populate()` is atomic across all its paths. Cleanup failures raise `ModuleStartupRollbackError`, with the original failure followed by cleanup failures in `failures`; clean rollback re-raises the original error. Registration side effects outside the module registry are not automatically undone, so hooks must clean up their own resources.

Each `AppConfig.lifecycle_state` progresses through `ModuleLifecycleState.LOADING`, `LOADED`, `READY`, and `SHUTDOWN` (a loaded module may shut down before becoming ready). If a ready hook fails, installed modules remain; retrying `mark_ready()` skips hooks that already succeeded. Shutdown attempts every remaining module in reverse order and raises an aggregated `ModuleLifecycleError` with `failures` entries identifying module, phase, and error. Failed shutdown hooks are retried; completed shutdowns are not. `AppConfig.lifecycle_state` is read-only; the registry changes it through `transition_to()`. `registry.ready` stays `True` when a module is installed afterwards, and that module stays `loaded` until `mark_ready()` runs again, so check `lifecycle_state` per module. Neither `mark_ready()` nor `shutdown()` may be called re-entrantly from hooks or signal receivers, and neither may be called from an `on_load` hook or `module_loaded` receiver while a module installs: raise an error there to abort and roll back the installation instead. `on_load` may install further modules one at a time (a concurrent sibling `ainstall()`, e.g. via `asyncio.gather`, raises `ModuleLifecycleError` before loading anything), but no hook or receiver may install a new module during `mark_ready()` or `shutdown()` (it raises `ModuleLifecycleError` and leaves the registry unchanged): install before `mark_ready()`, or `install()` then `mark_ready()` again.

### Async lifecycle

Each operation has an `a`-prefixed twin: `ainstall()`, `apopulate()`, `amark_ready()`, `ashutdown()`, and `bedrock.asetup()` / `bedrock.shutdown()` / `bedrock.ashutdown()` at process level. Both families run one implementation, so rollback, retry, idempotency and aggregate errors are identical.

- Async entrypoints await async hooks on the running loop and run plain hooks in a worker thread, in dependency order and reverse order on shutdown.
- Sync entrypoints call plain hooks directly and run async hooks on Bedrock's process-wide runtime loop, which `bedrock.run_async()` also uses and only `bedrock.shutdown()` closes (so a client opened in `on_load` closes on the same loop). They work only when no loop is running in the thread; otherwise they raise `ModuleAsyncRequiredError` naming the async entrypoint, with no nested loop and no unawaited coroutine. `mark_ready()` and `shutdown()` check for coroutine hooks first and change nothing.
- Sync code that calls async services (Celery tasks, CLI commands, scripts) uses `bedrock.run_async(coro)`, not `asyncio.run()` or `async_to_sync`: it runs on the runtime loop, so clients from `on_load` and the async engine stay usable across calls. The caller drives the loop and blocks; threads take turns. It works without `setup()`. It raises `RuntimeError` inside a running loop (await instead), after `asetup()` on a still-open loop, and from a `to_thread` worker of a coroutine running there. Never call `bedrock.shutdown()` / `ashutdown()` from inside `run_async()` work (raises `RuntimeError`); shut down after it returns.
- Start and stop through the same family (`asetup()` + `ashutdown()` or `setup()` + `shutdown()`); loop-bound clients cannot move between loops.
- Concurrent tasks and threads wait for the running operation. A hook calling back in is rejected as described above. Cancelling an async operation leaves every module valid; call `ashutdown()` again to resume, so keep hooks retry-safe. A sync hook or receiver already running in a worker thread cannot be stopped: cancellation waits for it to return (repeated cancels do not skip the wait) before rolling back or releasing the operation, and an awaitable it returns after cancellation is released, not awaited. Cancelling during install rollback does not abort cleanup: every introduced module is still unwound in reverse order, then `CancelledError` is raised (cleanup failures are logged and attached as notes). A cleanup hook that itself raises `CancelledError` or `KeyboardInterrupt` leaves its module and dependencies registered for `shutdown()`.
- A module owning sync and async clients keeps `on_shutdown` a plain function that closes the sync client and returns the awaitable closing the async one (or declares `async def on_shutdown`).
- The database is process infrastructure: `bedrock.shutdown()` / `await bedrock.ashutdown()` release initialized sync and async engines after every module hook, even if one failed, and do nothing if the database was never initialized.

For named class extension points, register trusted classes or lazy paths in `on_load`; see `references/class-registry-guide.md`.

---

## Signal System

Bedrock provides lifecycle signals for cross-module communication. Connect handlers to react when modules load, become ready, or shut down.

### Available Signals

```python
from bedrock.module.signals import (
    module_loaded,     # After each module's on_load hook
    module_ready,      # After each module's ready hook
    module_shutdown,   # After each module's on_shutdown hook
    registry_ready,    # After all modules ready
    registry_shutdown, # When registry shuts down
)
```

### Connecting to Signals

```python
from bedrock.module.signals import registry_ready, module_loaded


# Decorator form
@registry_ready.connect
def on_ready(sender, **kwargs):
    print("All modules ready!")


# Explicit connect
def on_module_loaded(sender, **kwargs):
    print(f"Module loaded: {sender}")

module_loaded.connect(on_module_loaded)
```

### Sending Signals

Most signals are sent automatically by the registry. If you create custom signals in your module:

```python
from bedrock.signal import Signal

# Define a custom signal
user_created = Signal("user_created")

# Send (sync) — in pure sync context, adapts async receivers
user_created.send(sender, user=new_user)

# Send (async) — canonical for mixed sync/async receivers
await user_created.asend(sender, user=new_user)
```

> **Sync/async contract:** `send()` is sync-facing. In a pure sync context it can adapt async receivers. Inside a running event loop, it raises `RuntimeError` if an async receiver is reached. Use `await asend()` for async or mixed contexts. See `references/signals-guide.md` for details.

---

## Signals vs Hooks

See `SKILL.md` → "Choosing a Mechanism" for when to use a direct call, DI, `ClassRegistry`, `ProviderCatalog`, hooks, or signals, including failure semantics. For extension points where every implementation must succeed, see `references/hooks-guide.md` → "Extension Points That Must All Succeed".

---

## Dependency Injection

The DI container (`bedrock.di.container`) provides service registration and resolution with three lifetimes.

### Registering Services

```python
from bedrock.di import container, Lifetime

# Register a factory (singleton by default)
container.register(IUserRepo, factory=UserRepo, lifetime=Lifetime.SINGLETON)

# Register a pre-built instance
container.register_instance("cache", my_cache_service)

# Or use the @provider decorator (auto-registers in global container)
from bedrock.di import provider

@provider
class UserService:
    ...

@provider(IUserRepo, lifetime=Lifetime.TRANSIENT)
class PostgresUserRepo:
    ...
```

For normal Bedrock modules, prefer the default global container shown above.
Reach for a custom `Container()` only when you need an isolated registration
graph, such as tests or sandboxed plugin execution.

```python
from bedrock.di import Container, Lifetime

custom = Container()


@custom.provider(IUserRepo, lifetime=Lifetime.SINGLETON)
class SandboxUserRepo:
    ...


@custom.inject(repo=IUserRepo)
def run_preview(*, repo: IUserRepo) -> None:
    ...
```

### Resolving Services

```python
from bedrock.di import container

# By type
repo = container.resolve(IUserRepo)

# By string key
cache = container.resolve("cache")

# Check registration
if container.is_registered(IUserRepo):
    repo = container.resolve(IUserRepo)
```

### Scoped Services

```python
from bedrock.di import container, Lifetime

container.register(IDbSession, factory=create_session, lifetime=Lifetime.SCOPED)

# Scoped services share one instance within a scope
with container.scope("request"):
    s1 = container.resolve(IDbSession)
    s2 = container.resolve(IDbSession)
    assert s1 is s2  # Same instance
```

### Testing with Overrides

```python
from bedrock.di import container

# Temporarily replace a service
with container.override(IUserRepo, FakeUserRepo()):
    # Code here sees the fake
    repo = container.resolve(IUserRepo)
    assert isinstance(repo, FakeUserRepo)
# Original restored automatically
```

### Using @inject

```python
from bedrock.di import inject

@inject(repo=IUserRepo, cache="cache")
def get_user_profile(user_id: int, *, repo, cache):
    cached = cache.get(f"user:{user_id}")
    if cached:
        return cached
    return repo.get_by_id(user_id)
```

---

## Settings Pattern

Create module-level settings using `BaseSettings`. Wrap module-level singletons with `SettingsProxy` to defer environment variable reading until first attribute access, preventing import-time side effects.

### Creating Module Settings

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

### Usage

```python
# First access triggers actual env var reading
if my_settings.DEBUG:
    print(f"API key: {my_settings.API_KEY}")
```

### Rules

- Use `SettingsProxy` for module-level singletons that may be imported before env vars are ready
- Use `BaseSettings` directly when env vars are guaranteed ready at construction time
- Use `env_prefix` in `model_config` to namespace env vars (e.g. `MYMODULE_API_KEY`)
- Never force early initialization by accessing settings at import time

### Built-in Settings References

| Settings Class | Env Prefix | Location |
|---------------|------------|----------|
| `BedrockSettings` | `BEDROCK_` | `bedrock.settings` |
| `BedrockRuntimeSettings` | `BEDROCK_RUNTIME_` | `bedrock.settings` |
| `DbSettings` | `DATABASE_` | `bedrock.database.config` |

---

## Installation Hooks

**File**: `installation.py` (optional)
**Used by**: `bedrock manage install`

### Available Hooks

| Hook | When Called |
|------|------------|
| `pre_install()` | After migrations, before `install()` |
| `install()` | After migrations |
| `post_install()` | After install() |

### Example

```python
from bedrock.database import db


def pre_install() -> None:
    """Run after migrations, before install()."""
    print("Preparing installation...")


def install() -> None:
    """Run after migrations — seed data, etc."""
    from .models import UserModel
    with db.session_scope() as session:
        if not session.query(UserModel).first():
            session.add(UserModel(name="admin", email="admin@example.com", hashed_password="..."))

def post_install() -> None:
    """Run after install — final checks."""
    print("Installation complete!")
```

### Invocation

```bash
bedrock manage -A myproject.modules.users install
```

---

## CLI Commands

**File**: `commands.py` (optional)
**Framework**: Typer

### Creating Commands

```python
import typer
import bedrock

app = typer.Typer(name="users", help="User management commands")


@app.command()
def create_user(name: str, email: str) -> None:
    """Create a new user."""
    bedrock.setup()
    from .service import user_service
    from .entities import UserCreateRequest
    user = user_service.create_user(UserCreateRequest(name=name, email=email, password="temp"))
    typer.echo(f"Created user: {user.name} ({user.email})")


@app.command()
def list_users() -> None:
    """List all users."""
    bedrock.setup()
    from .service import user_service
    users = user_service.list_users()
    for user in users:
        typer.echo(f"  {user.name} <{user.email}>")
```

### Registration

In `manifest.yaml`:

```yaml
commands: commands:app
```

The `commands` field is a relative import path (`module:attribute`). The registry resolves it to the full path (e.g. `myproject.modules.users.commands:app`) and mounts it.

### Invocation

```bash
bedrock run --app myproject.modules.users myproject.modules.users create-user Alice alice@example.com
bedrock run --app myproject.modules.users myproject.modules.users list-users
```

---

## Playbooks

### What is a Playbook

A playbook is AI-readable documentation bundled inside a module package. It teaches agents how to use the module without scanning source code. When an agent encounters a module it doesn't recognize, it reads the playbook to learn the API, patterns, and gotchas.

### Directory Structure

```
my_module/
├── __init__.py
├── manifest.yaml
├── service.py
└── playbook/
    ├── PLAYBOOK.md
    └── references/
        ├── api-reference.md
        └── examples/
            └── usage.py
```

`PLAYBOOK.md` is required. The `references/` directory holds supplementary docs and code examples.

### When to Write One

**Rule of thumb**: If another project will import this module but won't have its source code in the repo, write a playbook.

This applies to modules published as separate packages (PyPI, private repos) or shared across multiple applications. If the module lives in the same repo and agents can scan it directly, a playbook is optional.

### What to Put in PLAYBOOK.md

The main playbook should answer five questions:

1. **What does this module do?** One paragraph summary.
2. **How to import and initialize.** Exact import paths and setup steps.
3. **Core API.** The main classes and functions developers will call.
4. **Common patterns.** Copy-pasteable code examples for typical workflows.
5. **Anti-patterns.** What NOT to do, and why.

Keep it concise. Agents scan quickly; they don't need prose. Show code over explanations.

### CLI Access

```bash
# Read the main playbook
bedrock app playbook <module_import_path>

# Read a reference file
bedrock app playbook <module_import_path> <filename>

# Examples
bedrock app playbook myapp.auth
bedrock app playbook myapp.auth references/api-reference.md
bedrock app playbook myapp.auth examples/usage.py
```

### Playbook vs Agent Skills

| Approach | Scope | When Loaded |
|----------|-------|-------------|
| Skills | Global knowledge (Git, testing, frameworks) | Conversation start |
| Playbooks | Local knowledge (this specific module) | On-demand via CLI |

Agent skills load at conversation start and stay in context the entire time. If a project uses 10 modules with 10 skills, that's 10 sets of instructions competing for context window space, most irrelevant to the current task.

Playbooks solve this. They live inside the module package and are only read when the agent explicitly calls `bedrock app playbook`. No context pollution, no wasted tokens.
