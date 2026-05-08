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

### Initialization Sequence

```
import time → apps exists (empty), db exists (unconfigured)
    ↓
bedrock.setup() → apps.populate(module_list)
    ↓
on_load hooks → modules call db.init(url) if needed
    ↓
apps.mark_ready() → ready hooks fire
    ↓
registry_ready signal emitted
```

### Shutdown

Reverse install order: each module's `on_shutdown` hook runs, then `registry_shutdown` signal fires. Close connections and release resources here.

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
