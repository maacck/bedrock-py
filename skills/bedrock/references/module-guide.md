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
| `version` | `str` | Yes | — | Module version string |
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
**Signature**: `(registry: ModuleRegistry, app: AppConfig) -> None`

### Available Hooks

| Hook | When Called | Use Case |
|------|------------|----------|
| `on_load` | During `install()`, after module is added | Early initialization, dependency checks |
| `ready` | After ALL modules are installed | Configure services, start background tasks |
| `on_shutdown` | During `shutdown()`, in REVERSE order | Cleanup, close connections |

### Example

```python

def on_load(**kwargs) -> None:
    """Called during module installation."""
    if not registry.is_installed("myproject.core"):
        raise RuntimeError("myproject.core must be installed first")


def ready(**kwargs) -> None:
    """Called after all modules are installed."""
    from .service import user_service
    user_service.initialize()


def on_shutdown(**kwargs) -> None:
    """Called during shutdown."""
    from .service import user_service
    user_service.cleanup()
```

### Rules

- Hook signature is ALWAYS `(registry: ModuleRegistry, app: AppConfig) -> None`
- Hooks are called by the registry — do NOT import or call them manually
- `ready` is the most common hook for service initialization
- `on_shutdown` runs in REVERSE install order (last installed = first shutdown)

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

# Send (sync)
user_created.send(sender, user=new_user)

# Send (async)
await user_created.asend(sender, user=new_user)
```

---

## Settings Pattern

Create module-level settings using `LazySettings`. The settings object defers environment variable reading until first attribute access, preventing import-time side effects.

### Creating Module Settings

```python
from bedrock.conf import LazySettings
from pydantic_settings import SettingsConfigDict


class MyModuleSettings(LazySettings):
    model_config = SettingsConfigDict(env_prefix="MYMODULE_")
    API_KEY: str = ""
    DEBUG: bool = False
    MAX_RETRIES: int = 3


my_settings = MyModuleSettings()  # Returns a proxy — no env vars read yet
```

### Usage

```python
# First access triggers actual env var reading
if my_settings.DEBUG:
    print(f"API key: {my_settings.API_KEY}")
```

### Rules

- ALWAYS extend `LazySettings`, NEVER `BaseSettings` directly
- Use `env_prefix` in `model_config` to namespace env vars (e.g. `MYMODULE_API_KEY`)
- Instantiate at module level — the proxy ensures lazy initialization
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
| `pre_install()` | Before migrations |
| `install()` | After migrations |
| `post_install()` | After install() |

### Example

```python
from bedrock.database import db


def pre_install() -> None:
    """Run before database migrations."""
    print("Preparing installation...")


def install() -> None:
    """Run after migrations — seed data, etc."""
    from .models import UserModel
    with db.session as session:
        if not session.query(UserModel).first():
            session.add(UserModel(name="admin", email="admin@example.com", hashed_password="..."))
            session.commit()


def post_install() -> None:
    """Run after install — final checks."""
    print("Installation complete!")
```

### Invocation

```bash
bedrock manage install -A myproject.modules.users
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
bedrock run --app myproject.modules.users create-user --name Alice --email alice@example.com
bedrock run --app myproject.modules.users list-users
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

# Read reference files
bedrock app playbook-ref <module_import_path> <filename>

# Examples
bedrock app playbook myapp.auth
bedrock app playbook-ref myapp.auth references/api-reference.md
bedrock app playbook-ref myapp.auth examples/usage.py
```

### Playbook vs Agent Skills

| Approach | Scope | When Loaded |
|----------|-------|-------------|
| Skills | Global knowledge (Git, testing, frameworks) | Conversation start |
| Playbooks | Local knowledge (this specific module) | On-demand via CLI |

Agent skills load at conversation start and stay in context the entire time. If a project uses 10 modules with 10 skills, that's 10 sets of instructions competing for context window space, most irrelevant to the current task.

Playbooks solve this. They live inside the module package and are only read when the agent explicitly calls `bedrock app playbook`. No context pollution, no wasted tokens.
