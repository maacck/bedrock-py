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
from sqlalchemy import String, Boolean
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

# Choose one initialization method:
db.init(url="postgresql://user:pass@localhost/mydb")

# Or use settings from environment variables:
# from bedrock.database.config import DbSettings
# db.init(settings=DbSettings())
```

**Important**: Initialize before database access. A later `db.init()` replaces the synchronous engine and session factory, closes the sync session bound in the current context, and disposes the previous synchronous engine. If an async engine has already opened a connection, `await db.aclose()` before `init()`; `init()` does not sync-dispose that engine. Failed engine construction leaves the previous configuration usable.

An explicit URL selects the connection URL; engine pool options still come from `DbSettings`, including its configured database type.

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

Accessing `db.session` before entering the scope binds a session, so call `db.clear_session()` first if necessary. Commit expires ORM instances and the scope closes the session; return plain data or refresh and load required values before detaching instances.

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
- Independent side effects that must persist even if the caller rolls back (a transactional outbox should instead share the caller's transaction)
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
from bedrock.module import apps

# Populate apps with the target app and its dependencies before creating the manager.

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
bedrock db check <app>
bedrock db history <app>
bedrock db uninstall <app>
```

The CLI populates the registry for `<app>` and builds its database URL from `DATABASE_` settings, so secret handling applies there too. Use `--` before a negative positional target, for example `bedrock db downgrade myproject.modules.users -- -1`.

`upgrade` and `downgrade` (including `ensure_schema` and `uninstall`) take a PostgreSQL advisory lock or MySQL/MariaDB `GET_LOCK`. SQLite runs unlocked; migrate it from one process. The wait is `DATABASE_MIGRATION_LOCK_TIMEOUT`. A timeout raises `MigrationError`.

`bedrock db check <app>` prints each loaded branch's current and head revisions without changing the database. It exits non-zero when any branch is `behind` or `unknown` (including a database that has never been migrated).

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
| `limit` | `int` | `10` | Results per page; integer from 1 through 1000, excluding booleans |
| `page` | `int` | `1` | Page number (1-indexed) |
| `sort_key` | `str \| list[tuple[str, str]] \| None` | `None` | Column name or list of column/direction pairs |
| `sort_dir` | `str \| None` | `None` | Direction for a string `sort_key`; `"desc"` descends, otherwise ascending |
| `q` | `str \| None` | `None` | Search term passed to `model.text_search()` |
| `filter_specs` | `dict \| list[dict] \| None` | `None` | Filter specification or list (see Filter Syntax) |
| `sqla_filters` | `list` | `None` | Pre-built SQLAlchemy filter clauses |
| `join_models` | `list` | `None` | Explicit join targets |
| `options` | `list` | `None` | SQLAlchemy query options (e.g. `joinedload`) |
| `additional_select` | `list \| None` | `None` | Extra selected expressions; use `return_raw=True` to access them |
| `show_all` | `bool` | `False` | Disable pagination (return all results) |
| `count_pk` | `str \| None` | `None` | Column to count; defaults to the first primary key |
| `return_raw` | `bool` | `False` | Return raw rows instead of scalars |
| `group_by` | `str \| None` | `None` | Column or expression to GROUP BY |
| `auto_join_` | `bool` | `True` | Auto-join relationships from filters |

`limit` is validated even with `show_all=True`; invalid values raise `bedrock.exc.InvalidQueryLimitError`. With `show_all=True`, pagination is disabled but `page_info.has_more` is still calculated from `offset` and `limit`.

`q` searches the model's `__searchable_columns__` using `__search_op__` (default `ilike`); an empty searchable-column list produces a no-op filter. Counts reflect joined rows, not distinct model instances. With `group_by`, the count query is grouped and only its first scalar is returned.

---

## Filter Syntax

Filters are JSON-serializable dicts passed as `filter_specs` to `search_filter_sort_paginate`. A single dict or a list is accepted; list entries are combined with AND. The default operator is `eq`. Filter trees are limited to depth 50 and deeper trees raise `FilterDepthExceededError`.

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
| `any` | — | Relationship collection test | *(nested filter; see limitation below)* |
| `not_any` | — | Negated relationship collection test | *(nested filter; see limitation below)* |
| `has` | — | Relationship scalar test | *(nested filter; see limitation below)* |
| `text_search` | — | Search a related model's configured columns | `"search term"` |
| `fuzzy_search` | — | ILIKE with automatic `%` wrapping | `"alic"` |

`text_search` targets a relationship and calls the related model's `text_search()`; it uses `any()` or `has()` according to relationship cardinality. `like` case sensitivity depends on the database dialect. Other than `is_null` and `is_not_null`, operators require a `value` key.

The current generic filter formatter attempts column type lookup on relationship attributes. Consequently `any`, `not_any`, `has`, and colon traversal can raise `BadFilterFormatError` instead of producing a query; use explicit SQLAlchemy relationship clauses in `sqla_filters` until this is fixed. `text_search` handles relationships separately.

### Negation

Prefix a field name with `!` to negate its operator (for relationship paths, prefer explicit SQLAlchemy clauses):

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

The parser accepts `:` paths and selects `any()` or `has()` from relationship cardinality, but the current formatter's relationship type lookup can fail as described above. Use SQLAlchemy clauses instead:

```python
# Filter users who have an order with status "shipped"
sqla_filters = [UserModel.orders.any(Order.status == "shipped")]
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
{"field": "age", "op": "between", "value": [18, 65]}

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

Use dot notation for JOIN-based filters and explicit SQLAlchemy relationship clauses for any/has queries:

```python
# JOIN-based: filter users in "Engineering" department
filter_specs=[{"field": "department.name", "op": "==", "value": "Engineering"}]

# any/has-based: departments that have any active user
sqla_filters=[DepartmentModel.users.any(UserModel.is_active == True)]
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
    if user is None:
        raise LookupError(f"User {user_id} not found")
    user.update(**kwargs)  # CrudMixin method
    db.session.commit()
    return user


def delete_user(user_id: int) -> None:
    """Delete a user."""
    user = db.session.get(UserModel, user_id)
    if user is None:
        raise LookupError(f"User {user_id} not found")
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

`show_all=True` does not bypass limit validation, and `has_more` remains based on the requested page and limit rather than the number of returned items.

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
| `DATABASE_PASSWORD` | `SecretStr \| None` | `None` | Database password. Omitted from `repr` and validation errors. Use `get_secret_value()` only when opening a connection. |
| `DATABASE_ASYNC_DRIVER` | `str \| None` | `None` | Async DBAPI (`aiosqlite`, `asyncpg`, `psycopg`, `asyncmy`, `aiomysql`). Required for async sessions. |
| `DATABASE_MAX_CONNECTIONS` | `int` | `10` | Per-process ceiling. Non-SQLite engines set `max_overflow = MAX_CONNECTIONS - POOL_SIZE`. Must be `>= POOL_SIZE`. |
| `DATABASE_POOL_TIMEOUT` | `int` | `30` | Seconds to wait for a connection |
| `DATABASE_POOL_SIZE` | `int` | `10` | Steady-state pool size |
| `DATABASE_POOL_RECYCLE` | `int` | `1800` | Seconds before recycling a connection |
| `DATABASE_MIGRATION_LOCK_TIMEOUT` | `float` | `30` | Seconds to wait for the migration lock. `0` does not wait. MySQL rounds a positive value up to whole seconds. |
| `DATABASE_SCHEMA` | `str` | `"bedrock.db"` | Database name or file path |

Defaults (`POOL_SIZE=10`, `MAX_CONNECTIONS=10`) cap a non-SQLite process at 10 connections. The previous engine left SQLAlchemy's implicit overflow of 10 in place (20 total). Raise `DATABASE_MAX_CONNECTIONS` to restore that capacity. SQLite ignores pool settings. The URL is built with `sqlalchemy.engine.URL.create`. `PASSWORD` is a `SecretStr`; the rendered URL is not part of settings `repr`.

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
| `on_insert` | A new instance is pending in the session at flush |
| `on_update` | An existing instance is dirty at flush |
| `on_delete` | An instance is marked for deletion at flush |

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
    __observers__ = None  # Required by observes_model registration

@observes_model(Catalog, "on_insert", "on_delete")
def log_catalog_changes(session, target, identifier):
    print(f"Catalog {target.id} was {identifier}")
```

The callback receives three arguments: the session, the model instance, and the event identifier string.

Callbacks run before SQL is flushed; database-generated IDs may not yet be assigned on insert.

### Callback Signature

```
callback(session, target, identifier) -> None
```

- `session`: The active SQLAlchemy session being flushed.
- `target`: The model instance that triggered the event.
- `identifier`: One of `"on_insert"`, `"on_update"`, or `"on_delete"`.

### Custom Observer Instances

Pass a custom `ModelObserver` via the `observer` keyword. Registrations are stored on the model class, not isolated per observer; multiple registered observer instances listen to the same session flush and can invoke the same callbacks more than once.

```python
from bedrock.database.observer import ModelObserver

custom_observer = ModelObserver()

@observes_model(Catalog, "on_insert", observer=custom_observer)
def handle_insert(session, target, identifier):
    # Registered through custom_observer
    pass
```

### Weak References

Observer callbacks are stored as weak references; keep a strong reference to callbacks and their owning objects for as long as they are registered. A collected function or bound-method owner is not silently skipped by the current implementation and can cause a flush-time error.

---

## DatabaseManager API

The `db` singleton is a `DatabaseManager` instance. It owns the engine, session factory, and settings. All access goes through this object.

| Method / Property | Description |
|-------------------|-------------|
| `db.init(url, settings)` | Initialize or replace the engine and session factory. |
| `db.engine` | The SQLAlchemy engine. Raises `DatabaseNotConfiguredError` if not initialized. |
| `db.session` | The current context-local session. Creates one on first access within a context. |
| `db.session_factory` | The underlying session factory. |
| `db.settings` | The resolved database settings. |
| `db.set_session(session)` | Bind an existing session to the current execution context. |
| `db.clear_session()` | Close and unbind the current context-local session. |
| `db.session_scope()` | Context manager — transactional scope with auto commit/rollback. Binds to context. |
| `db.independent_session()` | Context manager — isolated transaction that does not bind to context. |
| `db.aengine` | Async engine for the URL resolved by `init()`, including an explicit `url=`. Only the driver is swapped for `DATABASE_ASYNC_DRIVER`. |
| `db.asession` | Context-local async session. Separate context variable from `db.session`. |
| `db.asession_scope()` | Async transactional scope. Same commit, rollback, and nesting rules as `session_scope()`. |
| `db.aindependent_session()` | Async transaction that does not bind `db.asession`. |
| `db.close()` | Dispose the sync engine. A built async engine stays usable. Idempotent. `init()` works again afterwards. |
| `db.aclose()` | Dispose the async engine. The sync engine stays usable. Idempotent. Does not shut down the module registry. If both engines were built, call `close()` and `await aclose()`. |

Async drivers install as extras: `aiosqlite` → `bedrock-core[async-sqlite]`; `asyncpg` and `psycopg` → `bedrock-core[async-postgresql]`; `asyncmy` and `aiomysql` → `bedrock-core[async-mysql]`. A missing package raises `DatabaseConfigurationError` and names the extra.

### Error Handling

Accessing `db.engine`, `db.session`, `db.session_factory`, or `db.settings`, or using session management methods, before calling `db.init()` raises `DatabaseNotConfiguredError`. Always initialize the database before using them.
