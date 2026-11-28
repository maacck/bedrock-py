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
10. [Model Events](#model-events)
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
    __filterable_fields__ = ["name", "email", "is_active"]  # client-filterable; PKs always are

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

`db.session` returns the session bound to the current context and never creates one. Reading it with nothing bound raises `SessionNotBoundError`. The entry point binds it; services only read it and commit:

| Entry point | Binds the session with |
|-------------|------------------------|
| Script, CLI command, custom worker, WebSocket handler, startup hook | `with db.session_scope():` or `@db.session_scope()` |
| HTTP request or task handled by an integration | The integration's middleware or task class, which opens one scope per request or task |
| Test | `with db.session_scope():` in the test; the `db_session` fixture rolls it back |
| Session managed by another framework | `db.set_session(...)` + `db.clear_session()` in `finally` |

```python
from bedrock.database import db

# service.py: never opens a scope
def create_user(name: str, email: str) -> UserModel:
    user = UserModel(name=name, email=email)
    db.session.add(user)
    db.session.commit()
    return user

# entry point
with db.session_scope():
    create_user("Alice", "alice@example.com")
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
- Raises `SessionAlreadyBoundError` (a `BedrockExc` and `RuntimeError`) if called while a session is already bound

Commit expires ORM instances and the scope closes the session; return plain data or refresh and load required values before detaching instances.

### Decorating Entry Points

`session_scope()` and `asession_scope()` also work as decorators. Each call opens a new scope with its own session and transaction, and the decorator may be applied before `db.init()`:

```python
@db.session_scope()
def import_users(path: str) -> None:
    for name, email in read_rows(path):
        create_user(name, email)

@db.asession_scope()
async def nightly_sync() -> None:
    await sync_accounts(db.asession)
```

Use parentheses (`@db.session_scope()`). Decorate entry points only: a decorated function called while a session is bound (for example from a request) raises `SessionAlreadyBoundError`.

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
try:
    ...
finally:
    db.clear_session()  # closes the session, then clears ContextVar
```

---

## Migrations

Bedrock runs migrations itself via `MigrationsManager`. Executed migrations are recorded in the `bedrock_migrations` table, keyed by `(app, revision)`; each app's scripts live in its own `migrations/` directory. Alembic is used only as a library to execute and autogenerate scripts. Table ownership limits autogeneration. It does not sandbox historical scripts.

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
- Runs the module's migration scripts up to head, together with the migrations they depend on. It never uses `create_all` or stamps.
- A module with models but no migration scripts raises `MigrationError`: run `bedrock db revision <app> -m "initial schema"` first.
- Returns `"created"` when the module had no applied migrations and some ran, `"upgraded"` when migrations ran, `"up-to-date"` when none were pending.

### Creating Revisions

```python
mgr.revision(
    app_import_path="myproject.modules.users",
    message="add email column",
    autogenerate=True,  # default
)
```

New revisions are named `<number>_<slug>` (`0001_initial`, `0002_add_email_column`): the module's highest number plus one. Existing random IDs are kept; the next name continues from the old head. Generation requires a single head, and autogeneration also requires the module to be at head. It writes qualified `app:revision` entries into `depends_on` for the modules it depends on (they must be direct manifest dependencies; bare names are rejected). `branch_labels` and `MigrationsManager(script_location=...)` no longer exist; scripts that still declare `branch_labels` load normally and the label is ignored.

A generated revision is a reviewable draft: review, edit, and commit it. Runtime uses only the committed declared graph (`down_revision` plus qualified `depends_on`) and never infers ordering from foreign keys; add a missing `depends_on` by hand.

### Table Ownership

Ordinary ORM models need no annotation. Resolve ownership from the mapped class's defining package, matched to the longest loaded Bedrock module import path on a complete package boundary. Imported domain models belong to their owning module; imports and re-exports of foreign models do not transfer ownership. Import required domain models from `models.py`; unimported files are not discovered.

Unmapped `Table` objects, including association tables, need `info["bedrock_module"]`. In `myapp.tags.models`, after the users module defines `users` in the shared metadata:

```python
from sqlalchemy import Column, ForeignKey, Integer, String, Table
from bedrock.database import BedrockModel

user_labels = Table(
    "user_labels",
    BedrockModel.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("label", String(50), primary_key=True),
    info={"bedrock_module": "myapp.tags"},
)
```

Declare `myapp.users` in the tags module's manifest `depends_on`. Full metadata remains available for foreign keys, but referencing a table does not transfer ownership. Migration ordering between modules is recorded in the scripts' `depends_on` (see Creating Revisions).

- An ORM model can assert an owner with `__table_args__ = {"info": {"bedrock_module": "myapp.tags"}}`; it must agree with the model's defining module.
- Validate every table in shared metadata before autogeneration. Missing/conflicting owners, invalid declarations, unloaded apps, and owners without a models module raise `MigrationError`. This is a compatibility tightening, not a silent skip.
- Validate every mapper sharing a table: a resolved model does not cover an unresolved model. A valid explicit table declaration can supply missing ownership, but cannot override known conflicts between loaded modules.
- Same-module mappings of one table, including single-table inheritance, share an owner. Cross-module mappings of the same table are rejected. Joined-table inheritance resolves each local table separately.
- Use schema-qualified identities. Explicit and implicit default schemas identify one physical table; duplicate declarations are rejected. Both reflection and metadata are filtered, including table-owned columns, indexes, constraints, and foreign keys.
- Recompute the ownership snapshot per operation; no database ledger, global registration hook, or ordinary model annotation is needed. An empty owned set never means all tables.
- Once a model is removed, do not infer its old table's owner or auto-drop it. Use `--no-autogenerate` for a reviewed explicit migration. Historical execution, manual revisions, and offline SQL do not require current ownership. Raw unmanaged Alembic calls retain the full metadata view and are outside the isolation guarantee.

### Squash rollout

Keep retired revision IDs reserved. `head` and `base` are command targets, not rename choices. Adopt ambiguous bare Alembic heads on the bridge release. Use synchronous non-generic functions for manual replacements.

Rehearse both paths with seeded data. No automatic net diff: preserve data backfills and review transaction boundaries. Partial ranges must reach the old endpoint on the bridge release; upgrade refuses partial or conflicting ranges. Deploy and run the ordinary upgrade (`bedrock db upgrade` / `ensure_schema`): complete approved ranges in the target's ancestry are reconciled before business migrations (fresh executes, current skips); read-only commands never mutate. For handover or recovery, pause writes, back up, preview with `bedrock db squash apply` and switch manually with `--apply`.

### Existing Migration Safety

Bedrock does not rewrite old scripts, and new filtering does not repair old contaminated ones. Installation now executes the recorded scripts, so uninstall runs the `downgrade()` of scripts whose `upgrade()` really ran; databases adopted from `alembic_version` keep protected baseline rows instead (see below).

Before installing, upgrading, rolling back, or uninstalling an affected module:

1. Pause operations, back up the database and scripts, and inspect each environment's `alembic_version` (or `bedrock_migrations`), actual schema, and data.
2. Review both upgrade and downgrade across core and plugin modules, including columns, indexes, constraints, and raw SQL. A script owned by the right module does not prove safe table operations.
3. Regenerate unpublished scripts only after confirming they were never applied anywhere. Coordinate repairs to published/applied history; do not casually delete revisions or change their names. Appending a new revision alone cannot prevent an older destructive downgrade from running.
4. Check residual DDL after failed upgrades even without an applied revision. Restore lost data from verified backups or database recovery, not create_all.
5. Verify the actual install/upgrade/uninstall paths on an isolated production copy before resuming.

### Adopting an Existing Database

A database created by an earlier Bedrock version records migrations in `alembic_version`; every migration command (`upgrade`, `downgrade`, `ensure_schema`, `uninstall`) raises `MigrationError` until `bedrock db adopt <app> ...` (`mgr.adopt()`) runs once with every module loaded. Native Alembic CLI must not be used on Bedrock-managed databases afterward.

1. During a maintenance window stop old migration entry points; back up the database, deployed package, and original scripts. Old heads are resolved through that deployment's own revision graph, not today's manifest edges. Suspected missed data migrations need manual reconciliation; adoption does not replay them.
2. Each old head must belong to exactly one loaded module. A missing, ambiguous, or unresolvable head writes nothing.
3. Adopt stages one `origin='baseline'` row per revision up to those heads, verifies it, and renames it atomically to `bedrock_migrations`. `alembic_version` stays as an audit snapshot. An interrupted run before publication leaves only staging state and can be retried; a published table is never dropped or re-imported.
4. Baseline rows are protected: no script runs for them, and any downgrade, uninstall, or cascade that must undo one fails before the first DDL statement. Rolling back to the baseline itself is allowed.
5. After the new version has written to the database, do not switch the old program back. Restore the matching database, program, and migration files together.

Before adoption, align the script metadata in the new deployment copy: preserve every deployed `revision`, local `down_revision` (including merge parents), and operation body. Resolve a legacy bare cross-module `depends_on = "a12f"` against the original deployment and spell it `depends_on = ("myapp.auth:a12f",)` only if the owner is unique and a direct manifest dependency. Never infer historical edges from today's heads. On a restored database use normal `bedrock db history` before `adopt`, then compare `current`/`check` with the old position. The ledger records *all* direct parents; editing either kind of edge after adoption or execution blocks writes.

### Adding a New Independent Registry After Adoption

For genuinely new independent modules with no prior schema or history, `bedrock db init-ledger <root> --adopted-ledger <source>` creates the configured target table empty. It verifies the prior adoption read-only; no script runs and no baseline rows are copied. Back up and rehearse on a restored copy first.

Preflight: pause migrations, take a verified backup (database, package, scripts), confirm the source is adopted and `alembic_version` still holds the old heads, load only the new registry's modules, rehearse on a restored copy. It refuses and writes nothing when: legacy table missing; source not a valid adopted ledger or lacks evidence for the old heads; source already has rows for a loaded module; a table owned by a loaded module's models already exists; the target or a source/target staging table exists. Never patch ledger rows to bypass a refusal.

### Custom Version Table and Independent Module Sets

Default ledger is `bedrock_migrations`; override with `MigrationsManager(..., version_table="...")` or host setting `DATABASE_MIGRATION_VERSION_TABLE`. Name: lowercase simple SQL identifier (`[a-z_][a-z0-9_]*`), at most 57 characters. The name and `<name>_adopt` (adopt staging) are reserved; a name must not end in `_adopt`.

```python
from bedrock.module.registry import ModuleRegistry

billing, search = ModuleRegistry(), ModuleRegistry()
billing.populate(["myproject.modules.billing"])
search.populate(["myproject.modules.search"])

MigrationsManager(billing, url, version_table="billing_migrations").ensure_schema("myproject.modules.billing")
MigrationsManager(search, url, version_table="search_migrations").ensure_schema("myproject.modules.search")
```

- Programmatic independent registries, one ledger each, only when module sets own disjoint model tables and no migration depends on a module in another ledger (not checked across ledgers). All managers share the same database lock.
- Host and CLI use one configured root set and one ledger via `DATABASE_MIGRATION_VERSION_TABLE` (no CLI flag, no manifest field): `DATABASE_MIGRATION_VERSION_TABLE=billing_migrations bedrock db upgrade myproject.modules.billing`.
- Never rename a table to switch ledgers; a new name is a different, possibly empty ledger and applied migrations look pending.
- `adopt` imports the *whole* legacy `alembic_version` baseline into the configured ledger; load every module first. A set owning only part of the legacy history cannot adopt it.

### Upgrade / Downgrade

```python
mgr.upgrade("myproject.modules.users")             # upgrade to head
mgr.upgrade("myproject.modules.users", target="+1") # one step forward
mgr.downgrade("myproject.modules.users", target="-1")
mgr.downgrade("myproject.modules.users", target="base")
```

Targets are resolved within the app: `head`, `base`, `+N`/`-N`, or a full local revision name such as `0002_add_email`. Anything else (`heads`, another app's revision, a numeric prefix) raises `MigrationError`. Upgrade runs the app's pending migrations together with the migrations they depend on and nothing else; downgrade undoes the app's applied descendants. `upgrade`, `downgrade` and `uninstall` return the executed `(app, revision)` steps in order. When applied revisions cannot be read, `ensure_schema` and `uninstall` raise `MigrationError` instead of treating the module as never applied; `check` reports `unknown`.

Rollback is dependency-aware: if applied migrations of other modules depend on the ones being undone, `downgrade`/`uninstall` raise `MigrationError` and change nothing. `cascade=True` (`--cascade`) rolls those dependents back first; dependents that are not loaded are always an error (add `--load <app>`), and baseline rows from `adopt` cannot be rolled back. `heads()` and `current()` return tuples of revision names. To remove a module from the CLI use `bedrock db downgrade <app> base`; `bedrock db uninstall` no longer exists (`MigrationsManager.uninstall()` remains in Python).

### CLI Commands

```bash
bedrock db revision <app> -m "description"
bedrock db upgrade <app> [target]
bedrock db downgrade <app> <target> [--cascade] [--load <other-app>]
bedrock db heads <app>
bedrock db current <app>
bedrock db check <app>
bedrock db history <app>
bedrock db adopt <app> [<app> ...]
bedrock db init-ledger <root> [<root> ...] --adopted-ledger <source-name>
```

The CLI populates the registry for `<app>` and builds its database URL from `DATABASE_` settings, so secret handling applies there too. Use `--` before a negative positional target, for example `bedrock db downgrade myproject.modules.users -- -1`.

`upgrade` and `downgrade` (including `ensure_schema` and `uninstall`) take a PostgreSQL advisory lock or MySQL/MariaDB `GET_LOCK`. SQLite runs unlocked; migrate it from one process. The wait is `DATABASE_MIGRATION_LOCK_TIMEOUT`. A timeout raises `MigrationError`. Each migration runs in its own transaction; scripts must not commit or use `autocommit_block()`, and MySQL/MariaDB cannot roll back failed DDL (compare the real schema before retrying).

`bedrock db check <app>` prints each loaded module's current and head revisions without changing the database. It exits non-zero when any module is `behind` or `unknown` (including a database that has never been migrated).

### Migration Directory Layout

Each app stores migrations alongside its code:

```
my_app/users/
├── migrations/
│   ├── 0001_initial.py
│   └── 0002_add_email_column.py
├── models.py
└── manifest.yaml
```

---

## Query Patterns

`bedrock.database` provides a JSON Schema (Draft 2020-12)-driven SQLAlchemy 2.0 query builder centered on `QuerySchema[M]` (`compile_query_schema`). All parsing, validation, JSON Schema export, and pure SQLAlchemy clause compilation live in `QuerySchema`; there is no `auto_join`, no `auto_loads`, and no entity-level `group_by`.

### Compiling `QuerySchema` & FastAPI `Annotated` Integration

```python
from typing import Annotated
from bedrock.database import QuerySpec, FilterNode, compile_query_schema

user_schema = compile_query_schema(UserModel)  # default policy cached per model class
json_schema = user_schema.json_schema()        # Draft 2020-12 JSON Schema
filter_schema = user_schema.filter_json_schema()

# FastAPI / Pydantic v2 native integration (zero HTTP coupling in bedrock-core):
type UserQuery = Annotated[QuerySpec, user_schema]
type UserFilter = Annotated[FilterNode, user_schema]
```

### Three Orthogonal Sync/Async Executor Pairs

| Scenario | Unpaginated | Paginated | Return type | Notes |
|---|---|---|---|---|
| **Models** | `search_models` / `asearch_models` | `paginate_models` / `apaginate_models` | `list[M]` / `PageResult[M]` | Supports `base_select`, `options` (data query only), optional server `limit`; no `group_by` |
| **Fields** | `search_fields` / `asearch_fields` | `paginate_fields` / `apaginate_fields` | `list[dict]` / `PageResult[dict]` | Single-table column projection and multi-column `group_by` |
| **Select** | `search_select` / `asearch_select` | `paginate_select` / `apaginate_select` | `list[dict]` / `PageResult[dict]` | Explicit cross-table `JOIN` / `LEFT JOIN` / `aliased()` / aggregation `Select` |

All six pairs are also available as methods on `QuerySchema[M]` (`search`, `paginate`, `search_fields`, `paginate_fields`, `search_select`, `paginate_select` and `a`-prefixed async twins). Non-paginated `search_*` calls validate `query` (including `pagination` if present) and silently ignore `query.pagination` without running `COUNT(*)`, so the same client JSON works directly for exports.

```python
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from bedrock.database import (
    FieldPolicy,
    QueryPolicy,
    apaginate_models,
    db,
    paginate_fields,
    paginate_models,
    paginate_select,
    search_models,
)

# 1. Paginated model search
page = paginate_models(
    db.session,
    UserModel,
    query={
        "filter": {"field": "is_active", "op": "eq", "value": True},
        "sort": [{"field": "name", "direction": "asc"}],
        "pagination": {"page": 1, "limit": 10},
    },
    options=[selectinload(UserModel.department)],
)
# Async: await apaginate_models(db.asession, UserModel, query=..., options=[selectinload(UserModel.department)])

# 2. Single-table projection / multi-column group_by
grouped = paginate_fields(
    db.session,
    UserModel,
    fields=[UserModel.is_active, func.count(UserModel.id).label("cnt")],
    group_by=[UserModel.is_active],
    query={"pagination": {"page": 1, "limit": 20}},
)

# 3. Cross-table explicit JOIN Select
stmt = select(UserModel.id, DepartmentModel.name.label("dept_name")).select_from(UserModel).join(UserModel.department)
rows = paginate_select(
    db.session,
    UserModel,
    stmt,
    query={"filter": {"field": "dept_name", "op": "starts_with", "value": "Eng"}},
    policy=QueryPolicy(extra_fields={"dept_name": FieldPolicy(column=DepartmentModel.name, sortable=True)}),
)
```

---

## Filter Syntax

Queries use a strict JSON Schema DSL (`extra="forbid"` on every object) with top-level keys `filter`, `sort`, `search`, and `pagination`.

- **Allowlist & policies**: Primary keys are always allowed. Other columns, hybrids (declare `FieldPolicy(value_type=...)`), and relationships must appear in `__filterable_fields__` or `QueryPolicy` / `__query_policy__`. `"__all__"` expands mapped columns on that model only, **never** relationships. Hidden and missing fields raise indistinguishable `BadFilterFormatError` by default (`reveal_allowed_fields=False`).
- **Safety budgets** (defaults in `QueryPolicy`): `max_depth=16`, `max_nodes=100`, `max_relation_depth=2`, `max_in_items=200`, `max_limit=1000`.
- **Logical combinators**: `{"logic": "and" | "or", "conditions": [...]}`, `{"logic": "not", "condition": ...}`.
- **Leaf predicates**: `{"field": "<name>", "op": "<op>", "value": ...}` (unary `is_null` / `is_not_null` forbid `value`).
  - Numeric/Temporal: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `in`, `between` (strict 2-item array); `DateTime` values align automatically with column `timezone=True|False`.
  - String: `eq`, `ne`, `in`, `contains`, `icontains`, `starts_with`, `ends_with` (auto-escapes `%`, `_`, `\`; raw `like`/`ilike` require `FieldPolicy(allow_raw_like=True)`).
  - Boolean: `eq`, `ne`; Enum/UUID: `eq`, `ne`, `in`.
- **Relationships (always compiled to `EXISTS`, never `JOIN`)**:
  - Explicit quantifier: `{"relation": "orders", "quantifier": "some" | "none" | "every", "filter": ...}` (collections; `every` requires `filter`) or `{"relation": "department", "quantifier": "has" | "missing", "filter": ...}` (to-one).
  - To-one dotted path syntax sugar: `{"field": "department.name", "op": "eq", "value": "Engineering"}` compiles to `UserModel.department.has(DepartmentModel.name == "Engineering")`.
- **Pluggable Pagers (`pager=` on `QuerySchema` / `compile_query_schema` / `paginate_*`)**:
  - `PagePager()` (default): `{"page", "limit"}` -> `PageInfo` (`total`, `page`, `limit`, `offset`, `has_more`).
  - `LimitOffsetPager(include_total=True)`: `{"limit", "offset"}` -> `LimitOffsetPageInfo` (`total`, `limit`, `offset`, `has_more`).
  - `CursorPager(include_total=True, unique_keys=None)`: `{"cursor", "limit"}` -> `CursorPageInfo` (`total`, `limit`, `has_more`, `next_cursor`); forward keyset pagination with opaque URL-safe Base64 JSON tokens, automatic PK tie-breakers on single-table queries (explicit `unique_keys` required for joined/multi-table selects), NULL-safe comparisons, and `InvalidCursorError` on tampered/sort-mismatched tokens.
  - Custom pagers subclass `PaginationParams` (inherits `limit`), `BasePageInfo`, and `BasePager`, returning `PageResult[T]` (`total: int | None`, `page_info: SerializeAsAny[BasePageInfo]`).

### Legacy `search_filter_sort_paginate` (Deprecated)

`bedrock.database.service.search_filter_sort_paginate`, `asearch_filter_sort_paginate`, `build_query`, and legacy `filter_specs` remain in `0.4.x` with a `DeprecationWarning` and are scheduled for removal in `0.5.0`. Migrate to `compile_query_schema` and `paginate_models` / `apaginate_models`.

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
    __filterable_fields__ = ["name"]

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))

    # One-to-many: department has many users
    users: Mapped[list["UserModel"]] = relationship(back_populates="department")


class UserModel(BedrockModel):
    __tablename__ = "users"
    __filterable_fields__ = ["name", "department"]  # "department.name" also needs "name" on DepartmentModel

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))

    # Many-to-one: user belongs to a department
    department: Mapped["DepartmentModel"] = relationship(back_populates="users")
```

### Querying Across Relationships

Both dotted to-one paths and explicit `relation` quantifier nodes compile to `EXISTS` subqueries without adding main-query joins:

```python
# To-one dotted syntax sugar -> UserModel.department.has(DepartmentModel.name == "Engineering")
{"field": "department.name", "op": "eq", "value": "Engineering"}

# Collection quantifier -> DepartmentModel.users.any(UserModel.is_active == True)
{"relation": "users", "quantifier": "some", "filter": {"field": "is_active", "op": "eq", "value": True}}
```

---

## Common Patterns

### CRUD in Service Layer

```python
from bedrock.database import PageResult, db, paginate_models


def get_user(user_id: int) -> UserModel | None:
    """Fetch a single user by primary key."""
    return db.session.get(UserModel, user_id)


def list_users(query: dict | None = None) -> PageResult[UserModel]:
    """List users with JSON Schema filtering, sorting, and pagination."""
    return paginate_models(db.session, UserModel, query=query)


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

### Unpaginated Export (Reusing Client Query JSON)

```python
from bedrock.database import db, search_models

# Validates query (including pagination if present) and ignores pagination without COUNT(*)
all_users = search_models(db.session, UserModel, query=client_query_json, limit=5000)
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
| `DATABASE_PASSWORD` | `SecretStr \| None` | `None` | Database password. Omitted from `repr` and validation errors. Use `get_secret_value()` only when opening a connection. |
| `DATABASE_ASYNC_DRIVER` | `str \| None` | `None` | Async DBAPI (`aiosqlite`, `asyncpg`, `psycopg`, `asyncmy`, `aiomysql`). Required for async sessions. |
| `DATABASE_MAX_CONNECTIONS` | `int` | `10` | Per-process ceiling. Non-SQLite engines set `max_overflow = MAX_CONNECTIONS - POOL_SIZE`. Must be `>= POOL_SIZE`. |
| `DATABASE_POOL_TIMEOUT` | `int` | `30` | Seconds to wait for a connection |
| `DATABASE_POOL_SIZE` | `int` | `10` | Steady-state pool size. Must be `>= 1` (SQLAlchemy treats 0 as unlimited). |
| `DATABASE_POOL_RECYCLE` | `int` | `1800` | Seconds before recycling a connection |
| `DATABASE_MIGRATION_LOCK_TIMEOUT` | `float` | `30` | Seconds to wait for the migration lock. `0` does not wait. MySQL rounds a positive value up to whole seconds. |
| `DATABASE_MIGRATION_VERSION_TABLE` | `str` | `"bedrock_migrations"` | Table that records applied migrations. Lowercase simple SQL identifier, at most 57 characters. |
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

## Model Events

Bedrock has no observer wrapper. Use SQLAlchemy events directly and choose the phase:

```python
from sqlalchemy import event
from sqlalchemy.orm import Session

@event.listens_for(Catalog, "after_insert", propagate=True)  # id assigned; use `connection`, not the session
def catalog_inserted(mapper, connection, target): ...

@event.listens_for(Session, "before_flush")  # objects added here join the same flush
def audit_catalog(session, flush_context, instances):
    for obj in session.new:
        if isinstance(obj, Catalog):
            session.add(AuditLog(action="catalog_created"))
```

`after_update` / `after_delete` and the `before_*` variants work the same way; `propagate=True` covers subclasses.

---

## DatabaseManager API

The `db` singleton is a `DatabaseManager` instance. It owns the engine, session factory, and settings. All access goes through this object.

| Method / Property | Description |
|-------------------|-------------|
| `db.init(url, settings)` | Initialize or replace the engine and session factory. |
| `db.engine` | The SQLAlchemy engine. Raises `DatabaseNotConfiguredError` if not initialized. |
| `db.session` | The session bound to the current context. Raises `SessionNotBoundError` when none is bound. |
| `db.session_factory` | The underlying session factory. |
| `db.settings` | The resolved database settings. |
| `db.set_session(session)` | Bind an existing session to the current execution context. |
| `db.clear_session()` | Close and unbind the current context-local session. |
| `db.session_scope()` | Context manager or decorator — transactional scope with auto commit/rollback. Binds to context. |
| `db.independent_session()` | Context manager — isolated transaction that does not bind to context. |
| `db.aengine` | Async engine for the URL resolved by `init()`, including an explicit `url=`. Only the driver is swapped for `DATABASE_ASYNC_DRIVER`. |
| `db.asession` | The async session bound to the current context. Separate context variable from `db.session`. Raises `SessionNotBoundError` when none is bound. |
| `db.asession_scope()` | Async context manager or decorator. Same commit, rollback, and nesting rules as `session_scope()`. |
| `db.aindependent_session()` | Async transaction that does not bind `db.asession`. |
| `db.close()` | Dispose the sync engine. A built async engine stays usable. Idempotent. `init()` works again afterwards. |
| `db.aclose()` | Dispose the async engine. The sync engine stays usable. Idempotent. Does not shut down the module registry. If both engines were built, call `close()` and `await aclose()`. `bedrock.shutdown()` / `await bedrock.ashutdown()` call whichever apply after all module hooks. |

Async drivers install as extras: `aiosqlite` → `bedrock-core[async-sqlite]`; `asyncpg` → `bedrock-core[async-postgresql]`; `asyncmy` → `bedrock-core[async-mysql]`. `psycopg` and `aiomysql` are not in an extra; install the package itself. A missing package raises `DatabaseConfigurationError` naming the extra or package.

### Error Handling

Accessing `db.engine`, `db.session`, `db.session_factory`, or `db.settings`, or using session management methods, before calling `db.init()` raises `DatabaseNotConfiguredError`. Always initialize the database before using them.

Reading `db.session` / `db.asession` with no session bound to the current context raises `SessionNotBoundError` (a `BedrockExc`). Bind one at the entry point.
