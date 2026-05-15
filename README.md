# Bedrock

A modular Python application framework with manifest-driven module loading, lifecycle management, and contrib modules for common capabilities.

Framework-agnostic core — no HTTP dependency in the runtime.

## Status

**Early-stage (0.1.0).** The core runtime works and is under active development, but APIs may change.

What exists today:

- Module registry with dependency resolution and lifecycle hooks
- Dependency injection container with singleton/transient/scoped lifetimes
- Hook system for structured call/response extension points (sync, async, robust)
- SQLAlchemy 2.0 database layer with Alembic migrations
- Cache system with memory and Redis.
- Signal/event system (inspired by Blinker)
- Typer-based CLI for module and database management
- Utility library (lazy loading, introspection)

What's still planned:

- Additional contrib modules
- Broader test coverage (currently: signal, cache schema, cache locks, lazy settings)

## Quick start

```bash
# Clone and install locally (not yet published to PyPI)
git clone <repo-url>
cd bedrock
uv sync

# Verify
uv run bedrock -v

# Run tests
uv run pytest packages/bedrock/tests/
```

### Basic usage

```python
import bedrock

# Initialize the runtime (discovers and loads modules)
bedrock.setup()

# Access key singletons
from bedrock.module import apps          # ModuleRegistry
from bedrock.database import db          # DatabaseManager
```

## Monorepo structure

This repository is a `uv` workspace:

```
packages/
├── bedrock/           # Core runtime
└──bedrock-cli/       # Scaffolding CLI (early, 0.0.1)
```

## Core runtime (`packages/bedrock`)

### Standard module structure

```
my_app/
├── __init__.py
├── manifest.yaml    # Module identity and dependencies
├── models.py        # SQLAlchemy models (optional)
├── entities.py      # Pydantic models for validation
├── service.py       # Business logic
├── exc.py           # Module exceptions
└── bootstrap.py     # Lifecycle hooks
```

### Module system

Manifest-driven module loading with explicit dependency management:

```python
from bedrock.module.registry import ModuleRegistry

apps = ModuleRegistry()
apps.install("inventory")  # Validates dependencies, loads in order
apps.populate()            # Runs on_load hooks for all modules
```

**Module manifest** (`manifest.yaml`):

```yaml
title: inventory
description: Inventory management module
version: 0.1.0
depends_on:
  - products
  - warehouse
commands: "commands:app"  # Optional: Typer app for CLI integration
```

**Lifecycle hooks** (in `bootstrap.py`):

```python
def on_load():
    """Called when module is first loaded."""
    pass

def ready():
    """Called when all modules are loaded and ready."""
    pass

def on_shutdown():
    """Called during graceful shutdown."""
    pass
```

Hooks may declare keyword parameters by name and receive runtime objects automatically:

```python
def on_load(*, registry, app, container, hooks):
    """registry=ModuleRegistry, app=AppConfig, container=DI container, hooks=HookRegistry"""
    pass
```

### Dependency injection

Lightweight DI container with three service lifetimes:

```python
from bedrock.di import container, provider, inject, Lifetime

@provider
class MyService:
    pass

@inject(svc=MyService)
def do_work(*, svc):
    svc.do_something()

# Override for tests
with container.override(MyService, FakeService()):
    do_work()
```

### Hook system

Structured extension points where modules declare specs and register implementations:

```python
from bedrock.hooks import hooks

ns = hooks.namespace("auth")

@ns.spec
def authenticate(user, password): ...

@ns.impl(priority=10)
def check_password(user, password): ...

ns.call("authenticate", user="alice", password="s3cret")
```

### Database layer

SQLAlchemy 2.0 integration with declarative models, session management, and Alembic migrations:

```python
from bedrock.database import db, BedrockModel
from sqlalchemy import Column, String, Integer

class Product(BedrockModel):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)

# Context-local session management
with db.session() as session:
    product = Product(name="Widget")
    session.add(product)
    session.commit()

# Query builder with filters, sorting, pagination
from bedrock.database.service import search_filter_sort_paginate

results = search_filter_sort_paginate(
    model=Product,
    filters={"name": {"operator": "ilike", "value": "%widget%"}},
    sort_by="name",
    page=1,
    per_page=20
)
```

**CLI for migrations:**

```bash
bedrock db revision --message "add products table"
bedrock db upgrade
bedrock db history
```

## CLI commands

```bash
# Module management
bedrock app inspect <module>   # Validate manifest and bootstrap
bedrock app info <module>      # Display module details
bedrock app install <module>   # Run migrations and hooks

# Database migrations
bedrock db revision --message "description"
bedrock db upgrade [revision]
bedrock db downgrade [revision]
bedrock db heads               # Show latest revisions
bedrock db current             # Show current revision
bedrock db history             # Show migration history

# Run module commands
bedrock run <module> <command>  # Execute module-specific CLI
```

## Dependencies

**Core runtime:**
- `pydantic` / `pydantic-settings` — Data validation and settings
- `sqlalchemy` >= 2.0 — Database ORM
- `alembic` — Database migrations
- `pyyaml` — Manifest parsing
- `loguru` — Logging
- `orjson` — Fast JSON serialization
- `typer` — CLI framework

**Optional:**
- `redis` — Redis cache backend (`uv add bedrock[cache-redis]`)

## Architecture principles

1. **Framework-agnostic core** — No HTTP dependencies in the module system
2. **Explicit modularity** — Manifests declare identity and dependencies
3. **Clean layer boundaries** — Models → Entities → Services → API
4. **Predictable conventions** — Strict structure for humans and AI

## Testing

```bash
uv run pytest packages/bedrock/tests/
```

Coverage is limited — currently covers signal system and lazy settings.

## Documentation

Docs site source lives in `docs-web/` (Fumadocs/Next.js):

```bash
cd docs-web
pnpm install
pnpm dev
```

## License
Bedrock is licensed under the MIT License. See [LICENSE](LICENSE) for details.
