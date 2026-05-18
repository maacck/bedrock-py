# Database Operations Guide

Complete guide to database operations in Bedrock applications.

## Table of Contents

1. [BedrockModel Usage](#bedrockmodel-usage)
2. [Database Initialization](#database-initialization)
3. [Session Management](#session-management)
4. [Migrations](#migrations)
5. [Query Patterns](#query-patterns)
6. [Filter Syntax](#filter-syntax)
7. [Relationships](#relationships)
8. [Common Patterns](#common-patterns)
9. [Configuration](#configuration)
10. [Model Observer](#model-observer)
11. [DatabaseManager API](#databasemanager-api)

---

## BedrockModel Usage

All database models extend `BedrockModel` using SQLAlchemy 2.0 `Mapped` syntax.

```python
from bedrock.database.base import BedrockModel
from sqlalchemy import String, Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column


class UserModel(BedrockModel):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
```

### CrudMixin Methods

`BedrockModel` includes `CrudMixin`, which provides:

| Method | Description |
|--------|-------------|
| `dict()` | Serialize model instance to a dictionary of column values |
| `update(**kwargs)` | Update model attributes from keyword arguments |
| `delete()` | Delete the instance from its attached session |


---

## Database Initialization

Initialize the database once per process before any database access:

```python
from bedrock.database import db

# With explicit URL
db.init(url="postgresql://user:pass@localhost/mydb")

# With settings object
from bedrock.database.config import DbSettings
settings = DbSettings()  # Reads from environment variables
db.init(settings=settings)
```

**Important**: Call `db.init()` exactly once per process. It is not idempotent.

---

## Session Management

Access the current context-local session via the `db` singleton:

```python
from bedrock.database import db

# Get current session (auto-created on first access per context)
session = db.session

# Use in service layer
def create_user(name: str, email: str) -> UserModel:
    user = UserModel(name=name, email=email)
    db.session.add(user)
    db.session.commit()
    return user
```

### Transaction Scopes with `session_scope()`

The recommended way to manage transactions — automatic commit on success, rollback on exception:

```python
from bedrock.database import db

# Automatic commit on clean exit
with db.session_scope() as session:
    user = UserModel(name="Alice", email="alice@example.com")
    session.add(user)
    # Commits automatically when block exits without exception

# Automatic rollback on exception
try:
    with db.session_scope() as session:
        session.add(UserModel(name="Bob", email="bob@example.com"))
        raise ValueError("Something went wrong")  # Rolls back
except ValueError:
    pass  # "Bob" was never persisted
```

**Key behaviors:**
- Binds session to `ContextVar` so `db.session` works inside the block
- Commits on clean exit, rolls back on any exception
- Always closes session and resets `ContextVar` on exit
- Raises `RuntimeError` if called while a session is already bound

### Independent Sessions with `independent_session()`

For side-effect writes that must not interfere with the current transaction:

```python
from bedrock.database import db

with db.session_scope() as session:
    order = Order(total=100)
    session.add(order)

    # Write audit log in separate transaction
    with db.independent_session() as audit_session:
        audit_log = AuditLog(action="order_created")
        audit_session.add(audit_log)
        # Commits independently — does not affect outer session

    # db.session still returns the outer session
    assert db.session is session
```

**Use cases:**
- Audit logging (persists even if main transaction rolls back)
- Event publishing or outbox patterns
- Reads that must not see uncommitted data from the caller
- Background tasks with their own transaction lifecycle

### Manual Session Binding

For middleware or request lifecycle management:

```python
# Bind an external session to current context
db.set_session(my_session)

# Clear binding when done (closes session, then clears ContextVar)
db.clear_session()
```

### SessionFactory as Context Manager

`SessionFactory` can be used as a context manager — closes and unbinds session on exit:

```python
with db.session_factory:
    session = db.session
    session.add(UserModel(name="Charlie", email="charlie@example.com"))
    session.commit()
# Session is closed and unbound here
```

---

## Migrations

Bedrock uses per-app Alembic branches via `MigrationsManager`. Each app gets its own branch label (equal to its import path), preventing cross-app migration conflicts.

### Setup

```python
from bedrock.database import MigrationsManager

mgr = MigrationsManager(
    registry=apps,          # Populated ModuleRegistry
    database_url="postgresql://user:pass@localhost/mydb",
)
```

### First Install — ensure_schema

```python
status = mgr.ensure_schema("myproject.modules.users")
# Returns: "created" | "upgraded" | "up-to-date"
```

Behavior:
- **No revisions applied**: Creates tables from SQLAlchemy metadata, stamps at head
- **Behind head**: Runs `upgrade` to bring branch current
- **At head**: No-op

### Creating Revisions

```python
mgr.revision(
    app_import_path="myproject.modules.users",
    message="add email column",
    autogenerate=True,  # default
)
```

### Upgrade / Downgrade

```python
mgr.upgrade("myproject.modules.users")             # upgrade to head
mgr.upgrade("myproject.modules.users", target="+1") # one step forward
mgr.downgrade("myproject.modules.users", target="-1")
mgr.downgrade("myproject.modules.users", target="base")
```

### CLI Commands

```bash
bedrock db revision <app> -m "description"
bedrock db upgrade <app> [target]
bedrock db downgrade <app> <target>
bedrock db heads <app>
bedrock db current <app>
bedrock db history <app>
bedrock db uninstall <app>
```

### Migration Directory Layout

Each app stores migrations alongside its code:

```
my_app/users/
├── migrations/
│   ├── abc123_2025-01-15_add_users_table.py
│   └── def456_2025-02-01_add_email_column.py
├── models.py
└── manifest.yaml
```

---

## Query Patterns

### search_filter_sort_paginate

The primary query builder for filtered, sorted, paginated results.

```python
from bedrock.database import db
from bedrock.database.service import search_filter_sort_paginate

result = search_filter_sort_paginate(
    db_session=db.session,
    model=UserModel,
    filter_specs=[
        {"field": "is_active", "op": "==", "value": True},
    ],
    sort_key="name",
    sort_dir="asc",
    page=1,
    limit=10,
)
```

**Return value**:

```python
{
    "items": [<UserModel>, ...],
    "total": 42,
    "page_info": {
        "total": 42,
        "limit": 10,
        "offset": 0,
        "page": 1,
        "query": "",
        "filters": [...],
        "paginated": True,
        "has_more": True,
    },
}
```

### Full Parameter Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `db_session` | `Session` | **required** | SQLAlchemy session |
| `model` | `Type[BedrockModel]` | **required** | Target model class |
| `limit` | `int` | `10` | Results per page |
| `page` | `int` | `1` | Page number (1-indexed) |
| `sort_key` | `str \| None` | `None` | Column name to sort by |
| `sort_dir` | `str \| None` | `None` | `"asc"` or `"desc"` |
| `q` | `str \| None` | `None` | Full-text search query |
| `filter_specs` | `list[dict]` | `None` | Filter spec list (see Filter Syntax) |
| `sqla_filters` | `list` | `None` | Pre-built SQLAlchemy filter clauses |
| `join_models` | `list` | `None` | Explicit join targets |
| `options` | `list` | `None` | SQLAlchemy query options (e.g. `joinedload`) |
| `show_all` | `bool` | `False` | Disable pagination (return all results) |
| `return_raw` | `bool` | `False` | Return raw rows instead of scalars |
| `group_by` | `str \| None` | `None` | Column or expression to GROUP BY |
| `auto_join_` | `bool` | `True` | Auto-join relationships from filters |

---

## Filter Syntax

Filters are JSON-serializable dicts passed as `filter_specs` to `search_filter_sort_paginate`.

### Basic Filter

```python
{"field": "name", "op": "==", "value": "Alice"}
```

### Available Operators

| Operator | Alias | Description | Example Value |
|----------|-------|-------------|---------------|
| `==` | `eq` | Equal | `"Alice"` |
| `!=` | `neq` | Not equal | `"Bob"` |
| `>` | `gt` | Greater than | `30` |
| `<` | `lt` | Less than | `30` |
| `>=` | `ge` | Greater than or equal | `30` |
| `<=` | `le` | Less than or equal | `30` |
| `like` | — | SQL LIKE (case-sensitive) | `"%alice%"` |
| `ilike` | — | SQL ILIKE (case-insensitive) | `"%alice%"` |
| `not_ilike` | — | Negated ILIKE | `"%alice%"` |
| `in` | — | Value in list | `[1, 2, 3]` |
| `not_in` | — | Value not in list | `[1, 2, 3]` |
| `between` | — | Value between two bounds | `[18, 65]` |
| `is_null` | — | Field is NULL | *(no value)* |
| `is_not_null` | — | Field is not NULL | *(no value)* |
| `any` | — | Relationship list contains match | *(nested filter)* |
| `not_any` | — | Negated any | *(nested filter)* |
| `has` | — | Relationship scalar matches | *(nested filter)* |
| `text_search` | — | Full-text search on relationship | `"search term"` |
| `fuzzy_search` | — | ILIKE with automatic `%` wrapping | `"alic"` |

### Negation

Prefix the field name with `!` to negate any operator:

```python
{"field": "!is_active", "op": "==", "value": True}  # WHERE NOT is_active = True
```

### Boolean Combinators

Combine filters with `or` / `and`:

```python
# OR
{"or": [
    {"field": "age", "op": ">", "value": 30},
    {"field": "name", "op": "==", "value": "Bob"},
]}

# AND (explicit)
{"and": [
    {"field": "is_active", "op": "==", "value": True},
    {"field": "role", "op": "in", "value": ["admin", "staff"]},
]}
```

### Nested Filters — Dot Notation (JOIN)

Use `.` to traverse relationships via SQL JOIN:

```python
# Filter users by their department's name (joins department table)
{"field": "department.name", "op": "==", "value": "Engineering"}
```

### Nested Filters — Colon Notation (any/has)

Use `:` to traverse relationships via `any()` or `has()` (auto-detected from relationship type):

```python
# Filter users who have any order with status "shipped"
{"field": "orders:status", "op": "==", "value": "shipped"}

# Multi-level nesting
{"field": "orders:items:product_name", "op": "ilike", "value": "%widget%"}
```

### Filter Examples

```python
# Pagination with multiple filters
result = search_filter_sort_paginate(
    db_session=db.session,
    model=UserModel,
    filter_specs=[
        {"field": "is_active", "op": "==", "value": True},
        {"field": "email", "op": "ilike", "value": "%@company.com"},
        {"or": [
            {"field": "role", "op": "==", "value": "admin"},
            {"field": "age", "op": ">=", "value": 21},
        ]},
    ],
    sort_key="name",
    sort_dir="asc",
    page=1,
    limit=25,
)

# Between filter
{"field": "created_at", "op": "between", "value": ["2025-01-01", "2025-12-31"]}

# Fuzzy search (wraps value with %)
{"field": "name", "op": "fuzzy_search", "value": "alic"}
# Equivalent to: WHERE name ILIKE '%alic%'
```

---

## Relationships

Define relationships using SQLAlchemy 2.0 `Mapped` syntax:

### Foreign Key + Relationship

```python
from bedrock.database.base import BedrockModel
from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship


class DepartmentModel(BedrockModel):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))

    # One-to-many: department has many users
    users: Mapped[list["UserModel"]] = relationship(back_populates="department")


class UserModel(BedrockModel):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))

    # Many-to-one: user belongs to a department
    department: Mapped["DepartmentModel"] = relationship(back_populates="users")
```

### Querying Across Relationships

Once relationships are defined, use dot/colon notation in filters:

```python
# JOIN-based: filter users in "Engineering" department
filter_specs=[{"field": "department.name", "op": "==", "value": "Engineering"}]

# any/has-based: departments that have any active user
filter_specs=[{"field": "users:is_active", "op": "==", "value": True}]
```

---

## Common Patterns

### CRUD in Service Layer

```python
from bedrock.database import db
from bedrock.database.service import search_filter_sort_paginate


def get_user(user_id: int) -> UserModel | None:
    """Fetch a single user by primary key."""
    return db.session.get(UserModel, user_id)


def list_users(page: int = 1, limit: int = 10, filters: list | None = None) -> dict:
    """List users with filtering and pagination."""
    return search_filter_sort_paginate(
        db_session=db.session,
        model=UserModel,
        filter_specs=filters or [],
        page=page,
        limit=limit,
        sort_key="name",
        sort_dir="asc",
    )


def create_user(name: str, email: str) -> UserModel:
    """Create a new user."""
    user = UserModel(name=name, email=email)
    db.session.add(user)
    db.session.commit()
    db.session.refresh(user)
    return user


def update_user(user_id: int, **kwargs) -> UserModel:
    """Update a user's fields."""
    user = db.session.get(UserModel, user_id)
    user.update(**kwargs)  # CrudMixin method
    db.session.commit()
    return user


def delete_user(user_id: int) -> None:
    """Delete a user."""
    user = db.session.get(UserModel, user_id)
    db.session.delete(user)
    db.session.commit()
```

### Pagination Helper

```python
def paginated_response(result: dict) -> dict:
    """Transform search_filter_sort_paginate output for API response."""
    return {
        "data": [item.dict() for item in result["items"]],
        "pagination": {
            "total": result["page_info"]["total"],
            "page": result["page_info"]["page"],
            "limit": result["page_info"]["limit"],
            "has_more": result["page_info"]["has_more"],
        },
    }
```

### Show All (No Pagination)

```python
result = search_filter_sort_paginate(
    db_session=db.session,
    model=UserModel,
    show_all=True,
)
all_users = result["items"]
```

---

## Configuration

Database settings use environment variables with the `DATABASE_` prefix. `DbSettings` is a Pydantic `BaseSettings` subclass, so it reads from the environment automatically.

### Environment Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DATABASE_TYPE` | `str` | `"sqlite"` | Database dialect (`sqlite`, `postgresql`, `mysql`) |
| `DATABASE_DRIVER` | `str` | `"pysqlite"` | DBAPI driver (`pysqlite`, `psycopg2`, `pymysql`) |
| `DATABASE_HOST` | `str \| None` | `None` | Database host (required for non-SQLite) |
| `DATABASE_PORT` | `int \| None` | `None` | Database port (required for non-SQLite) |
| `DATABASE_USERNAME` | `str \| None` | `None` | Database username |
| `DATABASE_PASSWORD` | `str \| None` | `None` | Database password |
| `DATABASE_MAX_CONNECTIONS` | `int` | `10` | Maximum connection pool size |
| `DATABASE_POOL_TIMEOUT` | `int` | `30` | Seconds to wait for a connection |
| `DATABASE_POOL_SIZE` | `int` | `10` | Number of connections to maintain |
| `DATABASE_POOL_RECYCLE` | `int` | `1800` | Seconds before recycling a connection |
| `DATABASE_SCHEMA` | `str` | `"bedrock.db"` | Database name or file path |

### SQLite

```bash
export DATABASE_TYPE=sqlite
export DATABASE_DRIVER=pysqlite
export DATABASE_SCHEMA=bedrock.db
```

For in-memory databases, set `DATABASE_SCHEMA=:memory:`.

### PostgreSQL

```bash
export DATABASE_TYPE=postgresql
export DATABASE_DRIVER=psycopg2
export DATABASE_HOST=localhost
export DATABASE_PORT=5432
export DATABASE_USERNAME=myuser
export DATABASE_PASSWORD=mypassword
export DATABASE_SCHEMA=myapp
```

### MySQL

```bash
export DATABASE_TYPE=mysql
export DATABASE_DRIVER=pymysql
export DATABASE_HOST=localhost
export DATABASE_PORT=3306
export DATABASE_USERNAME=root
export DATABASE_PASSWORD=secret
export DATABASE_SCHEMA=myapp
```

### Programmatic Configuration

Pass a `DbSettings` instance directly to `db.init()`:

```python
from bedrock.database import db, DbSettings

settings = DbSettings(
    TYPE="postgresql",
    DRIVER="psycopg2",
    HOST="localhost",
    PORT=5432,
    USERNAME="admin",
    PASSWORD="secret",
    SCHEMA="myapp",
)

db.init(settings=settings)
```

You can also override the connection URL entirely:

```python
db.init(url="postgresql+psycopg2://admin:secret@localhost:5432/myapp")
```

---

## Model Observer

The observer system hooks into SQLAlchemy's `before_flush` event to invoke callbacks when models are inserted, updated, or deleted.

### Event Identifiers

| Identifier | Fires When |
|------------|------------|
| `on_insert` | A new instance is added to the session |
| `on_update` | An existing instance is modified |
| `on_delete` | An instance is marked for deletion |

### Registering Observers

Use the `@observes_model` decorator:

```python
from bedrock.database import BedrockModel
from bedrock.database.observer import observes_model, observer
from sqlalchemy import Column, Integer, String

class Catalog(BedrockModel):
    __tablename__ = "catalog"
    id = Column(Integer, primary_key=True)
    name = Column(String(100))

@observes_model(Catalog, "on_insert", "on_delete")
def log_catalog_changes(session, target, identifier):
    print(f"Catalog {target.id} was {identifier}")
```

The callback receives three arguments: the session, the model instance, and the event identifier string.

### Callback Signature

```
callback(session, target, identifier) -> None
```

- `session`: The active SQLAlchemy session being flushed.
- `target`: The model instance that triggered the event.
- `identifier`: One of `"on_insert"`, `"on_update"`, or `"on_delete"`.

### Custom Observer Instances

Pass a custom `ModelObserver` via the `observer` keyword:

```python
from bedrock.database.observer import ModelObserver

custom_observer = ModelObserver()

@observes_model(Catalog, "on_insert", observer=custom_observer)
def handle_insert(session, target, identifier):
    # Only fires on the custom observer
    pass
```

### Weak References

Observer callbacks are stored as weak references. Bound methods use a `WeakMethod` wrapper to prevent memory leaks. If the object owning a bound method is garbage collected, the callback is silently skipped.

---

## DatabaseManager API

The `db` singleton is a `DatabaseManager` instance. It owns the engine, session factory, and settings. All access goes through this object.

| Method / Property | Description |
|-------------------|-------------|
| `db.init(url, settings)` | Initialize engine and session factory. Call once at process start. |
| `db.engine` | The SQLAlchemy engine. Raises `DatabaseNotConfiguredError` if not initialized. |
| `db.session` | The current context-local session. Creates one on first access within a context. |
| `db.session_factory` | The underlying session factory. |
| `db.settings` | The resolved database settings. |
| `db.set_session(session)` | Bind an existing session to the current execution context. |
| `db.clear_session()` | Close and unbind the current context-local session. |
| `db.session_scope()` | Context manager — transactional scope with auto commit/rollback. Binds to context. |
| `db.independent_session()` | Context manager — isolated transaction that does not bind to context. |

### Error Handling

Accessing `db.engine`, `db.session`, or `db.settings` before calling `db.init()` raises `DatabaseNotConfiguredError`. Always initialize the database before using any of these properties.
