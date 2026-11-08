# Architecture Reference

Bedrock's layered architecture, data flow, and singleton lifecycle.

---

## Core Principles

1. **Framework-agnostic core** — No HTTP dependencies in the module system. FastAPI support belongs in an adapter layer.
2. **Explicit modularity** — Modules declare identity and dependencies in `manifest.yaml`. No import-time side effects for registration.
3. **Clean layer boundaries** — Models → Entities → Services → API. Each layer has a single responsibility.
4. **Predictable conventions** — Strict file structure helps developers and AI assistants produce accurate code.

---

## Layer Boundaries

| Layer | Files | Responsibility |
|-------|-------|----------------|
| **API** | adapter/routes | HTTP specifics, request/response handling |
| **Logic** | `service.py`, `exc.py` | Business rules, orchestration, exceptions |
| **Validation** | `entities.py` | Pydantic models for input/output contracts |
| **Data** | `models.py` | SQLAlchemy models, ORM, persistence |

### Constraints

- **API → Logic**: API calls services, never the reverse
- **Logic → Data**: Services access persistence through models only
- **Logic → Validation**: Services use Pydantic entities for contracts
- **No HTTP in Logic**: Service functions never accept `Request` or `Response`
- **No ORM in API**: Controllers never interact with SQLAlchemy sessions directly

---

## Data Flow

```
Inbound → Validation → Dispatch → Business Logic → Persistence → Response
```

1. **Inbound** — Request arrives at API layer (HTTP, CLI, background task)
2. **Validation** — Adapter deserializes payload into Pydantic entity, rejects malformed input
3. **Dispatch** — API calls service function with validated entities
4. **Business Logic** — Service orchestrates: queries data layer, applies rules, emits signals
5. **Persistence** — Data layer executes ORM operations in managed session
6. **Response** — Service returns entities, API serializes for transport

Same business logic works from FastAPI routes, Celery tasks, or CLI commands.

---

## Singleton Lifecycle

### Key Singletons

| Singleton | Class | Import |
|-----------|-------|--------|
| `apps` | `ModuleRegistry` | `from bedrock.module import apps` |
| `db` | `DatabaseManager` | `from bedrock.database import db` |
| `container` | `Container` | `from bedrock.di import container` |
| `hooks` | `HookRegistry` | `from bedrock.hooks import hooks` |

### Initialization Sequence

```
import time → apps exists (empty), db exists (unconfigured), container exists, hooks exists
    ↓
bedrock.setup() → apps.populate(module_list)
    ↓
on_load hooks → modules may initialize resources such as db
    ↓
apps.mark_ready() → ready hooks fire
    ↓
hooks.validate() → warn about orphaned impls or empty specs
    ↓
registry_ready signal emitted
```

### Shutdown

Reverse install order: shutdown attempts each module's `on_shutdown` and reports failures together in `ModuleLifecycleError.failures`; `registry_shutdown` fires after all modules shut down. Failed shutdown hooks can be retried; completed shutdowns are skipped. Each operation also has an async twin (`ainstall`, `apopulate`, `amark_ready`, `ashutdown`) that runs the same state machine; hooks may be plain or `async def`, sync entrypoints inside a running loop raise `ModuleAsyncRequiredError`, and `bedrock.shutdown()` / `ashutdown()` release the database after all module hooks. `mark_ready()` and `shutdown()` are not re-entrant, are rejected from `on_load` hooks and `module_loaded` receivers while a module installs, and no new module can be installed from hooks or receivers during either.

Installation is failure-atomic: newly installed modules are removed on failure and successfully loaded modules receive best-effort reverse shutdown. `populate()` rolls back its whole installation phase, but a ready failure retains installed modules for retry. Cleanup failures raise `ModuleStartupRollbackError`. Each module's read-only `lifecycle_state` is a `ModuleLifecycleState` (`loading`, `loaded`, `ready`, `shutdown`).

---

## Runtime Primitives

### DI Container (`bedrock.di`)

A lightweight dependency injection container with three lifetimes: `SINGLETON`, `TRANSIENT`, and `SCOPED`. Thread-safe singleton resolution via double-checked locking.

- **`container.register(key, factory=..., lifetime=...)`** — Register a service factory.
- **`container.resolve(key)`** — Resolve by type or string key.
- **`container.scope(name)`** — Context manager for scoped services.
- **`container.override(key, instance)`** — Context manager for test doubles.
- **`@provider`** / **`@inject(**mappings)`** — Decorator shortcuts.

The container is injected into bootstrap hooks that declare a `container` parameter.

### Hook System (`bedrock.hooks`)

Structured call/response protocol for multi-implementation extension points. Signals also return receiver results, but hooks provide priority ordering and `firstresult` short-circuit.

- **`@hookspec`** / **`@hookimpl(priority=0)`** — Marker decorators for scanning into a namespace.
- **`HookNamespace(name)`** — Module-facing scoped API: `ns.spec()`, `ns.impl()`, `ns.call()`, `ns.acall()`.
- **`hooks.call(fqn, **kwargs)`** / **`hooks.acall(fqn, **kwargs)`** — Dispatch to all implementations.

The hook registry is injected into bootstrap hooks that declare a `hooks` parameter.

For selecting a concrete implementation class by name, use `ClassRegistry` rather than a hook or DI instance registration; see `references/class-registry-guide.md`.

---

## When to Use Each Layer

| Question | Layer | File |
|----------|-------|------|
| Where do I validate input? | Validation | `entities.py` |
| Where do I define DB tables? | Data | `models.py` |
| Where do I put business rules? | Logic | `service.py` |
| Where do I handle HTTP? | API | adapter/routes |
| Where do I define exceptions? | Logic | `exc.py` |
| Where do I run startup code? | Lifecycle | `bootstrap.py` |

### Decision Rules

- Touches `Request`/`Response` → **API layer**
- Imports `sqlalchemy` → **Data layer**
- Imports `pydantic` for shapes → **Validation layer**
- Contains logic/orchestration → **Logic layer**
- Runs once at startup → **Lifecycle hooks** (`bootstrap.py`)
