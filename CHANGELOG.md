# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking changes

- **Database query builder**: client-controlled field names are restricted to an allowlist.
  `filter_specs`, `sort_key`, and string `group_by` in `search_filter_sort_paginate()`,
  `asearch_filter_sort_paginate()`, and `build_query()` may only reference primary keys and the
  names a model lists in the new `__filterable_fields__` class attribute (columns, hybrids,
  association proxies, relationships). Each hop of a dotted or colon path is checked against its
  own model. `count_pk` must be a primary key. Any other name raises `BadFilterFormatError`
  ("Field `x` is not filterable on Model"), the same error for hidden and missing fields.
  Previously every column of every reachable model was filterable, so a client could read secret
  columns (for example `owner.password_hash`) by observing result counts or order.
  Migration: add `__filterable_fields__ = [...]` to each model queried with client-supplied
  filters, sorting, or grouping; for dotted paths, list the relationship on the parent model and
  the target field on the related model. Models whose callers already validate field names can
  set `__filterable_fields__ = "__all__"` to keep the previous behavior for that model; related
  models reached through it keep their own setting. Raw `sqla_filters` and
  `__searchable_columns__` for `q` are unaffected.
- **Migrations**: `MigrationsManager.upgrade()`/`downgrade()` and `bedrock db upgrade`/`downgrade`
  only move the requested app's branch. `head`, `base`, and relative steps (`+1`, `-1`) are
  qualified as `<app>@<target>`; a revision ID must belong to the app's branch. Targets outside
  it, such as `heads` or another app's revision ID, now raise `MigrationError`. Previously
  `bedrock db downgrade <app> base` downgraded every installed app's branch and dropped their
  tables. Migration: pass a target of the app you name, and migrate each app separately.
- **Database sessions**: `db.session` and `db.asession` no longer create a session on first access.
  With no session bound to the current context they raise the new `SessionNotBoundError` (a
  `BedrockExc`, exported from `bedrock.database`). `SessionFactory` is no longer a context manager
  (`with db.session_factory:` is removed). Previously a session touched in a long-lived context
  (module import, a sync `setup()` hook, a reused worker thread) was created there, never closed, and
  inherited by every later request or task, which leaked uncommitted writes between requests and
  made every FastAPI request fail with `RuntimeError` once the middleware tried to open its scope.
  Migration: bind a session at each entry point. Scripts, CLI commands, custom workers, WebSocket
  handlers, and startup hooks use `with db.session_scope():` or decorate the entry function with
  `@db.session_scope()` (async: `@db.asession_scope()`; each call opens its own scope). FastAPI
  requests use `DbSessionMiddleware` / `AsyncDbSessionMiddleware`; tests call such services inside
  `with db.session_scope():` (the `db_session` fixture still rolls it back); sessions owned by another
  framework use `db.set_session()` with `db.clear_session()` in `finally`. Services that read
  `db.session` need no change.
- **Database observer removed**: `bedrock.database.observer` (`observes_model`, `ModelObserver`,
  `observer`) is deleted. It re-implemented SQLAlchemy's ORM events: registering on an ordinary
  model raised `AttributeError`, callbacks ran once per `ModelObserver` instance, collected
  callbacks failed the next flush, and `on_insert` ran before the primary key was assigned.
  Migration: use SQLAlchemy events directly, `@event.listens_for(Model, "after_insert",
  propagate=True)` (likewise `after_update` / `after_delete`) when the callback needs the id, or
  `@event.listens_for(Session, "before_flush")` filtering `session.new` / `session.dirty` /
  `session.deleted` by model when it adds objects to the same flush.

### Added

- **Testing**: runtime-level pytest fixtures in `bedrock.testing` (enabled with
  `pytest_plugins = ["bedrock.testing"]`):
  - `bedrock_runtime` gives each test a fresh module registry that `bedrock.setup()`, `asetup()`,
    `shutdown()` and `ashutdown()` act on, records lifecycle events as `LifecycleEvent(kind, module)`,
    and on teardown shuts the registry's modules down, removes lifecycle-signal receivers added
    during the test, resets the DI container and hooks, restores `os.environ`, and drops cached
    `SettingsProxy` values. Backed by the `isolated_runtime()` context manager.
  - `module_factory(name, *, manifest=None, bootstrap=None, models=None, files=None)` writes
    importable temporary Bedrock modules and removes them after the test, including their mapped
    models and association tables, so the next test can reuse the name. With `bedrock_runtime`,
    the cleanup waits until the runtime has shut the modules down.
  - `db_session` runs the test's database work on one connection inside an outer transaction that
    is rolled back afterwards; `commit()` in service code (including `db.session_scope()`) commits a
    savepoint. `rollback_session()` and `arollback_session()` provide the same isolation as context
    managers, the async one for use in an async fixture. SQLite is supported.
- **Lifecycle**: `bedrock.run_async(awaitable)` runs async code from synchronous code (Celery tasks, CLI
  commands, scripts) on Bedrock's process-wide runtime loop, the loop synchronous `bedrock.setup()` runs async
  hooks on. Clients opened by `on_load` hooks and the async database engine stay usable across calls, unlike
  with `asyncio.run()`. The calling thread drives the loop and blocks; calls from several threads run one at a
  time. It works without `setup()`, and raises `RuntimeError` inside a running event loop, after `asetup()` on
  a loop that is still open, and from a thread that a coroutine on the runtime loop is waiting for.
  `bedrock.shutdown()` and `ashutdown()` raise `RuntimeError` before shutting anything down when called from
  work running on the runtime loop, which cannot close itself; call `shutdown()` after `run_async()` returns.
- **Rate limit**: new `bedrock.contrib.rate_limit` module, a framework-agnostic token bucket (GCRA).
  `RateLimitPolicy(name, limit, period)` lets `limit` units be spent at once and returns one every
  `period / limit`. `rate_limit.hit(policy, key, cost=1)` / `ahit()` return
  `RateLimitDecision(allowed, limit, remaining, retry_after, reset_after)` with durations in seconds; denied hits
  consume nothing. `reset()` / `areset()` refill a bucket. Keys are hashed before storage. The built-in Redis
  backend (extra `bedrock-core[rate-limit-redis]`, Redis 5+) evaluates each hit atomically in one Lua script
  on the server clock; it is selected by `RATE_LIMIT_BACKEND` (default `redis`) and configured by
  `RATE_LIMIT_REDIS_*`, independent of `CACHE_REDIS_*`. `register_backend()` / `register_backend_lazy()` add
  replacement backends implementing `RateLimitBackend`. Errors: `InvalidRateLimitPolicyError`,
  `InvalidRateLimitArgumentError`, `RateLimitPolicyConflictError`, `RateLimitBackendError` (no connection
  details in the message), `RateLimitBackendNotConfiguredError`, `RateLimitCloseError`, all under
  `RateLimitError`.

### Changed

- **Lifecycle**: the event loop that synchronous registry entrypoints run async hooks on is now process-wide
  instead of one per `ModuleRegistry`. A registry no longer closes it when its last module shuts down;
  `bedrock.shutdown()` closes it after every module and the database are released (and `ashutdown()` closes it
  if synchronous code used it). Code that calls `registry.shutdown()` without `bedrock.shutdown()` leaves the
  loop open until the next `bedrock.shutdown()` or interpreter exit; call `bedrock.shutdown()` to release it.

- **Database**: `asearch_filter_sort_paginate()` in `bedrock.database.service`, the `AsyncSession`
  variant of `search_filter_sort_paginate()`. It takes the same arguments and returns the same
  `items`/`total`/`page_info` dictionary, so `async def` services can list with `db.asession`.
- **Database**: `SessionAlreadyBoundError` (exported from `bedrock.database`) is raised when
  `session_scope()` / `asession_scope()` opens while a session is already bound. It is a
  `BedrockExc` and still a `RuntimeError`, so existing `except RuntimeError` handlers keep working.
- **Migrations**: `MigrationsManager.get_history()` returns `RevisionHistoryEntry` typed dicts;
  `is_head` is a `bool`, as the CLI already used it.

### Fixed

- **Migrations**: a database URL containing percent-encoded characters, such as a password with
  `@` or `#`, no longer makes every migration command fail with `ValueError: invalid interpolation
  syntax` that printed the full URL, password included.
- **Migrations**: when the applied revisions cannot be read (connection, permission, or script
  errors), `ensure_schema()` and `uninstall()` raise `MigrationError` instead of treating the
  branch as never applied, which stamped head over pending migrations or reported an uninstall
  that did nothing. `check()` reports such branches as `unknown`.
- **Database query builder**: with `group_by`, `total` in `search_filter_sort_paginate()` /
  `asearch_filter_sort_paginate()` is the number of groups. It used to be the row count of the
  first group, and an empty result made `has_more` raise `TypeError`.
- **Migrations**: online migrations open one connection instead of two (the first was never
  closed), and offline (`--sql`) runs configure the Alembic context before starting the
  transaction instead of failing with "No context has been configured yet".
- **Database**: `BedrockModel.verbose_name` returns the class name instead of raising
  `AttributeError`.
- **Database query builder**: type annotations match accepted inputs. `sort_key` takes a column
  name or a list of `(column, direction)` pairs (`SortKey`), and `group_by` takes a column name
  or a SQL expression (`GroupBy`); type checkers no longer reject these documented forms.
- **Migrations**: the `revision()` docstring no longer claims Bedrock writes dependency heads into
  `depends_on`; it never did.

## [0.3.1] - 2026-11-08

### Breaking changes

- **Logging**: importing `bedrock` or `bedrock.logging` no longer configures loguru.
  `configure_logging()` is not called at import, and `bedrock.setup()` does not install a sink.
  Sinks registered before the import stay in place. Non-CLI processes that relied on implicit
  stderr output must call `configure_logging()` from their process entry point. That call leaves
  existing handlers in place, including Loguru's preconfigured stderr handler. The `bedrock` CLI
  calls `configure_cli_logging()` before dispatching commands: it removes that preconfigured stderr
  handler when it is still installed, then installs Bedrock's sink, so `LOG_*` controls CLI stderr
  and log lines are not duplicated. Handlers added by the host are kept. Repeated calls replace only
  the sink Bedrock installed; configuration never calls `logger.remove()` without a handler id.
  The module constants `bedrock.logging.LOG_LEVEL`, `LOG_FORMAT`, `LOG_FILE`, `LOG_ROTATION`,
  `LOG_RETENTION`, `LOG_COMPRESSION`, and `LOG_SERIALIZE` are removed; read the same values from
  `bedrock.logging.LoggingSettings()`.
- **Database connection ceiling.** `DATABASE_MAX_CONNECTIONS` is the per-process cap.
  Non-SQLite engines set SQLAlchemy `max_overflow` to `MAX_CONNECTIONS - POOL_SIZE`.
  The defaults (`POOL_SIZE=10`, `MAX_CONNECTIONS=10`) now allow 10 connections.
  They previously allowed SQLAlchemy's implicit overflow and capped at 20.
  Raise `DATABASE_MAX_CONNECTIONS` when a deployment needs that extra capacity.
  `MAX_CONNECTIONS < POOL_SIZE` raises `DatabaseConfigurationError` and names both settings, and so does
  `POOL_SIZE < 1`, which SQLAlchemy would treat as an unlimited pool.
  SQLite engines still ignore pool settings.
- **Database passwords are secrets.** `DbSettings.PASSWORD` is a Pydantic `SecretStr`.
  Settings `repr` and validation errors do not include the password. Call
  `get_secret_value()` only at the edge that opens a connection. The URL is built
  with `sqlalchemy.engine.URL.create`, so reserved characters in each component are escaped.

### Added

- **Async lifecycle.** `ModuleRegistry` gains `ainstall()`, `apopulate()`, `amark_ready()` and `ashutdown()`,
  and `bedrock` gains `asetup()`, `shutdown()` and `ashutdown()` next to `setup()`. Existing hook names
  (`on_load`, `ready`, `on_shutdown`) and signal receivers accept either sync or async implementations; a hook
  that returns an awaitable is awaited too. Async entrypoints run async hooks on the running loop and plain
  hooks in a worker thread, in dependency order and reverse order on shutdown. Sync entrypoints keep working
  and adapt async hooks on a private loop that stays open until every module has shut down, so a client opened
  by `on_load` is closed on the same loop. Called inside a running event loop, a sync entrypoint raises the new
  `ModuleAsyncRequiredError` (a `ModuleLifecycleError`) that names the async entrypoint, instead of nesting a
  loop or leaving a coroutine unawaited; `mark_ready()` and `shutdown()` check coroutine hooks before running
  anything. Both modes share one state machine, so rollback, retry, idempotency and aggregate errors are
  identical, and cancelling an async operation leaves every module in a valid state. A cancelled async operation
  waits for synchronous hooks and receivers already running in a worker thread before it rolls back or releases
  the registry, because a blocking function cannot be forced to stop; repeated cancellation does not skip that
  wait, and an awaitable returned by a cancelled plain hook is released instead of left unawaited. Installation
  rollback cannot be interrupted by a caller cancellation: every introduced module is still cleaned in reverse
  order and the operation stays held until then, after which `CancelledError` is raised (chained to the install
  error that started the rollback, with cleanup failures logged and attached as notes). A cleanup hook that
  itself raises `CancelledError` or `KeyboardInterrupt` leaves that module and its dependencies registered so
  `shutdown()` can finish them. Installs started from a hook must run one at a time: an `ainstall()` started while
  a sibling install from the same operation is still running (for example via `asyncio.gather` in `on_load`)
  raises `ModuleLifecycleError` before loading anything, because a failing sibling's rollback would remove it.
- `bedrock.shutdown()` / `bedrock.ashutdown()` stop every module, then release the initialized database: `db.close()`
  for the sync engine and `db.aclose()` for the async engine, each only if it exists, even when a module hook
  failed. A database failure is reported with the module failures as `ModuleFailure("bedrock.database", ...)`.
  `db.close()` and `db.aclose()` keep their sync-only and async-only contracts.
  `DatabaseManager.has_async_engine` reports whether an async engine exists without building one.
- `CacheService.aclose()` closes the async client of every backend the service created (including replaced
  ones) and `close()` the sync client, so the Redis async client is no longer left open at shutdown.
  `CacheService.has_async_resources()`, `CacheBackend.holds_async_resources` and `CacheCloseError` support it.
- The cache module's `on_shutdown` hook and a new metrics `on_shutdown` hook close sync resources and return
  the async close to the registry, which awaits it before the module counts as shut down. Metric providers may
  define optional `flush()`, `aflush()` and `aclose()`; `MetricsManager.aclose()` and `has_async_resources()`
  are new, and providers are now flushed and closed at registry shutdown (failures are still only logged).
- `bedrock.logging.LoggingSettings` (prefix `LOG_`) is read when `configure_logging()` runs, so
  `LOG_LEVEL`, `LOG_FORMAT`, `LOG_FILE`, `LOG_ROTATION`, `LOG_RETENTION`, `LOG_COMPRESSION`, and
  `LOG_SERIALIZE` set after import are honored.
- `configure_cli_logging()` is the process-owned CLI initializer. It retires Loguru's preconfigured
  stderr handler, then calls `configure_logging()`.
- **Async database sessions.** Set `DATABASE_ASYNC_DRIVER` and install
  `bedrock-core[async-sqlite]`, `bedrock-core[async-postgresql]`, or
  `bedrock-core[async-mysql]`. `db.aengine`, `db.asession`, `db.asession_scope()`,
  and `db.aindependent_session()` follow the sync session rules and use a separate
  context variable. A missing driver raises `DatabaseConfigurationError` naming the extra (`psycopg` and
  `aiomysql` are not in an extra, so the message names the package to install). The session is unbound
  even when leaving `asession_scope()` or `aclear_session()` is cancelled while closing it.
  The SQLAlchemy asyncio extra (`greenlet`) is now a core dependency. `AsyncSessionFactory` is exported
  from `bedrock.database` next to `SessionFactory`.
- **`db.close()` and `db.aclose()`** dispose one side each. `close()` disposes the
  synchronous engine and leaves an already built async engine usable. `await aclose()`
  disposes the async engine and leaves the synchronous engine usable. If both were
  built, call both. Each call is safe to repeat. They do not shut down the module
  registry. `init()` disposes the previous synchronous engine. It does not
  synchronously dispose an async engine that has already opened a connection;
  `await aclose()` before `init()` when that engine should be closed.
- **Migration lock.** `upgrade` and `downgrade`, including uninstall and `ensure_schema`,
  take a PostgreSQL advisory lock or a MySQL/MariaDB `GET_LOCK`. SQLite stays unlocked
  and must be migrated from one process. `DATABASE_MIGRATION_LOCK_TIMEOUT` (default 30
  seconds; `0` does not wait) bounds the wait. MySQL rounds a positive timeout up to
  whole seconds. A timeout raises `MigrationError`. `MigrationsManager(..., lock_timeout=30)` sets the
  same bound in code.
- **`bedrock db check <app>`** prints each loaded branch's current and head revisions
  without changing the database. It exits with status 1 when any branch is behind head
  or the revision is unknown, including a database that has never been migrated.
  `MigrationsManager.check(app)` returns the same data as `BranchRevisionStatus` records
  (`branch`, `current`, `head`, `state`), exported from `bedrock.database`.
- `DbSettings.sqlalchemy_url(*, driver=None)` returns the connection `URL` with every component quoted;
  `DbSettings.database_uri(*, driver=None)` renders it as a string.
- **`bedrock.provider`** — `ProviderCatalog[P]` maps names to providers of one capability. Register an
  imported class or factory with `register()`, or a `module:attribute` path with `register_lazy()` (or
  `ProviderSpec` entries passed as `specs=`); lazy paths are imported only when selected. `create(name,
  **kwargs)` calls the factory and validates the result with `isinstance` against a class or
  `@runtime_checkable` Protocol, so implementations need not inherit it. `resolve(name)` returns the class or
  factory for application-side construction, without validation. Re-registering the same target is a no-op;
  a different target under a used name raises `ProviderConflictError`. Load failures raise `ProviderLoadError`
  naming the spec's `install_requirement` and keeping the original exception as `__cause__`. Provider guide
  (en/zh) and the `provider-guide.md` skill reference describe when to use `ClassRegistry` instead.
- `register_backend_lazy(name, "module:attribute", install_requirement=None)` in `bedrock.contrib.cache` and
  `bedrock.contrib.storage` registers a backend by path; the module is imported only when `configure()`
  selects it. `StorageCloseError` is new.
- **`CACHE_BACKEND`** selects the cache backend by registered name (default `memory`). `cache.configure()`
  without a name, the cache module's `ready` hook, and the lazy configure on first use all read it, so
  `CACHE_BACKEND=redis` with `CACHE_REDIS_*` switches an app to Redis without code. `configure()`'s
  `backend_name` default changes from `"memory"` to `None`; an explicit name ignores the variable. An unknown
  name raises `BackendNotConfiguredError` saying it came from `CACHE_BACKEND`.

### Changed

- **Cache and storage backends** are registered in a `bedrock.provider.ProviderCatalog` instead of a
  `ClassRegistry`. `register_backend()` accepts a class or a factory taking `settings=`; inheriting
  `CacheBackend` / `StorageBackend` is optional, and `configure()` rejects an object that lacks a protocol
  member. Registering the same object again is a no-op; a different backend under a taken name still raises
  `DuplicateRegistrationError`. A missing optional SDK now raises `BackendNotConfiguredError` /
  `StorageBackendNotConfiguredError` naming the extra (`bedrock-core[cache-redis]`,
  `bedrock-core[storage-s3]`); before, S3 raised a bare `ModuleNotFoundError`. The configuration error's
  `__cause__` is now a `bedrock.provider` error instead of a `ClassRegistryError`. Importing
  `bedrock.contrib.cache.backends.redis` without `redis` no longer fails; constructing `RedisBackend` does.
- **Storage**: `storage.close()` also closes backends replaced by earlier `configure()` calls. A backend
  whose `close()` fails stays tracked and is retried by the next call; failures raise `StorageCloseError`
  after every backend was attempted, instead of the backend's own exception. A failed `configure()` keeps the
  previous backend. Importing `bedrock.contrib.storage` no longer imports backend modules; `LocalBackend`,
  `S3Backend` and their settings are loaded on first access.

### Fixed

- Async engines use the URL already resolved by `db.init()`, including an explicit
  `url=` argument. Only the DBAPI driver is swapped for `DATABASE_ASYNC_DRIVER`; the query string is kept,
  including for SQLite `:memory:`. An explicit SQLite path no longer falls back to the default `bedrock.db`.
- `db.close()` no longer disposes the async engine, and `db.aclose()` no longer
  disposes the sync engine. `init()` no longer sync-disposes an async engine that
  has already opened a connection, which failed with `MissingGreenlet`.

### Documentation

- Docs site: new Changelog page (en/zh) rendered from `CHANGELOG.md`; Chinese Class Registry guide.
- Agent skills: new `class-registry-guide.md` reference and ClassRegistry section in the `bedrock` skill;
  skills, guides, and getting-started pages realigned with the v0.3.0 lifecycle, registry, cache, storage,
  hooks, database, and CLI APIs (e.g. `bedrock run --app <app> <module> <command>`, `BEDROCK_APP`,
  `bedrock --help` instead of the unsupported `bedrock -v`).
- Agent skills: one "Choosing a Mechanism" table (direct call, DI, `ClassRegistry`, hooks, signals) with
  failure/async semantics replaces four diverging comparisons; the hooks guide documents extension points
  where every implementation must succeed (fail-fast with DB rollback, collect-all, external side effects).
- Contributing: public signature/behavior changes must update `CHANGELOG.md`, Web Doc (en + zh), and agent
  skills in the same change (`AGENTS.md`, `CONTRIBUTING.md`, PR template); `CONTRIBUTING.md` gains a
  Releasing section with a pre-release alignment check and bump steps.
- Guides and skills: `on_load` examples in the cache, storage, Class Registry, and Provider guides declare an
  injected parameter (`def on_load(*, app)`); the zero-argument form they showed is rejected by the registry.
  The Signals guide describes lifecycle signal dispatch for both entrypoint families instead of sync-only
  `send()`.

## [0.3.0] - 2026-11-07

### Breaking changes

- `ClassRegistry` now requires `base_class` and uses the base instance type as its generic argument: `ClassRegistry[Base](base_class=Base, lazy_classes=paths)`. Only concrete, explicit subclasses are accepted. Register paths with `register_lazy()` instead of `add()`; `get()` raises typed `BedrockExc` subclasses instead of returning `None` or raising `KeyError`.
- Duplicate class/backend names now raise `DuplicateRegistrationError`, including repeated registrations of the same implementation. The mutable `instances`, `update`, `remove`, and `all` interfaces and the obsolete `DictManager` loader are removed.
- Custom Cache and Storage backends must explicitly inherit their respective backend contracts; structural-only implementations must migrate. Configuration errors preserve registry resolution failures in their exception chains.
- **Module lifecycle**: a failed `install()` / `populate()` no longer leaves modules registered.
  Every module introduced by the failed call is removed (modules installed earlier are kept), so
  code that relied on a module staying installed after its `on_load` raised must now retry
  the install instead.
- **Module lifecycle**: `shutdown()` no longer stops at the first failing hook. It attempts every
  module that has not completed shutdown and then raises one `ModuleLifecycleError` listing all
  failures; `registry_shutdown` is only emitted when every module shut down.
- **Module lifecycle**: `mark_ready()` skips modules that are already ready (their `ready` hook and
  `module_ready` signal are not repeated) and raises `ModuleLifecycleError` if a module was already
  shut down. `AppConfig.lifecycle_state` is now a `ModuleLifecycleState` string enum (values still compare
  equal to `"loaded"`, `"ready"`, `"shutdown"`) with a new initial value `"loading"`.

### Added

- Typed, thread-safe lazy class registration with successful-resolution caching, isolated registries, retained import error causes, and no instance or dependency-injection lifecycle. Direct registration retains class objects, including locally defined classes.
- Class Registry guide covering explicit extension registration, optional dependency loading, type guarantees, security boundaries, and migration from the previous registry.
- `ModuleLifecycleState`, `AppConfig.transition_to()` (validated forward transitions), `ModuleFailure`,
  and `ModuleStartupRollbackError`, exported from `bedrock.module`.
- `ModuleLifecycleError.failures` / `.failed_modules` expose every module failure with its original cause.
- Installed modules that had loaded before a later startup failure are unwound best-effort with
  `on_shutdown` (reverse order, `module_shutdown` emitted); if cleanup also fails,
  `ModuleStartupRollbackError` carries both the original failure and the cleanup failures.

### Changed

- `ModuleRegistry.install()`, `populate()`, `mark_ready()` and `shutdown()` run as one serialized operation
  (`populate()` covers both its install and ready phases), so other threads and tasks wait until the
  operation finishes and cannot run a hook twice. The guard no longer holds a thread lock, so it stays correct
  across `await` points; see the async lifecycle entry below.
- `mark_ready()` and `shutdown()` are not re-entrant: calling either from a bootstrap hook or a lifecycle
  signal receiver while one of them is running raises `ModuleLifecycleError` instead of re-running hooks
  (previously this recursed until `RecursionError`). The guard is registry state, not a thread lock.
- A failed `ready` hook now raises a `ModuleLifecycleError` whose `failures` name the module, like
  `shutdown()`. `failures` are always attributed to the module and step where they happened
  (`on_load`, `manifest`, `dependency`, `module_loaded`, `ready`, `on_shutdown`, ...), and the `__cause__` of
  every aggregated lifecycle error is an `ExceptionGroup` of all its failures.
- `AppConfig.lifecycle_state` is a read-only property. Assigning to it raises `AttributeError`, and passing it
  to `AppConfig(...)` or `AppConfig.model_validate(...)` raises `ValidationError`. A module starts at
  `loading` and changes state only through `AppConfig.transition_to()`, which validates the transition and
  accepts a `ModuleLifecycleState` or its string value. Every other `AppConfig` field keeps its behavior:
  unknown keyword arguments are still ignored, and assignments are still validated and coerced.
- `install()` is now a registry operation like `mark_ready()` and `shutdown()`. While it runs, an `on_load`
  hook, a `module_loaded` receiver, or a rollback hook that calls `shutdown()` or `mark_ready()` gets a
  `ModuleLifecycleError` ("Cannot call shutdown() while install() is running") instead of acting on a
  half-installed registry. To abort an installation from a hook, raise an error: the installation rolls back.
  Installing further modules from `on_load` or `module_loaded` stays supported.
- Installing a module that is not installed yet from a `ready` or `on_shutdown` hook, or from a
  `module_ready`, `registry_ready`, `module_shutdown`, or `registry_shutdown` receiver, raises
  `ModuleLifecycleError` before anything is loaded. Previously it left a `loaded` module in a registry that
  already reported `ready`, with no way to ready it from inside the hook. Install the module before
  `mark_ready()`, or call `install()` and then `mark_ready()` again, which readies only the new module.
  Installing an already installed module remains a no-op everywhere.
- `registry.ready` records that readiness was announced. It stays `True` when a new module is installed
  afterwards, and that module stays `loaded` until `mark_ready()` runs again. Check each module's
  `lifecycle_state` instead of reading `registry.ready` as "every installed module is ready".

### Fixed

- Calling `shutdown()` from an `on_load` hook no longer shuts down other modules in the middle of the
  installation and then fails on an invalid `loading` to `shutdown` transition that cut cleanup short.
  Calling it from a `module_loaded` receiver no longer shuts down the registry during installation.
  Both now fail with `ModuleLifecycleError`, and the failed installation still unwinds every module that
  had loaded.
- Failed `registry_ready` and `registry_shutdown` notifications can be retried without
  repeating completed module hooks. A failed ready notification resets `registry.ready`,
  so retrying `bedrock.setup()` also resumes notification delivery.
- Installation rollback rejects attempts to introduce new modules from cleanup hooks
  or signal receivers, preserving failure-atomic membership and install order.

## [0.2.2] - 2026-10-01

### Breaking changes

- **Storage backend protocol**: custom backends registered with
  `register_backend()` must implement `generate_presigned_url()` (returning
  `StoragePresignedUrl`) instead of `get_signed_url()`. The service-level
  `storage.get_signed_url()` keeps working through the new method.

### Added

- **`bedrock.contrib.storage`** — `storage.generate_presigned_url()` returns a
  `StoragePresignedUrl(method, endpoint, query_params, headers)` and can sign
  upload conditions for browser direct uploads: `mime_type`, `content_length`,
  `checksum_sha256` (base64 digest; S3 maps it to `x-amz-checksum-sha256`), and
  backend-specific `provider_headers` (e.g. `x-amz-checksum-type`). Invalid
  conditions and reserved headers raise `StorageError` before signing.
- **`bedrock.contrib.cache`** — `CacheSlot.get_or_load()` / `aget_or_load()`
  return the cached value or call the loader on a miss, store, and return it;
  `aget_or_load()` accepts sync or async loaders.
- `cache.configure()` logs the configured backend name and
  `metrics.register_provider()` logs each newly registered provider at INFO.

### Deprecated

- `storage.get_signed_url()` emits `DeprecationWarning`; use
  `storage.generate_presigned_url()`.

### Documentation

- Cache playbook expanded with slot, lock, coder, exception, and lifecycle references.
- Storage guide (en/zh) documents presigned requests, browser upload usage, and bucket CORS requirements.

## [0.2.1] - 2026-08-19

### Added

- **`bedrock.contrib.metric`** — application metrics instrumentation with a
  hardcoded-logging manager and pluggable providers:
  - `MetricsManager` singleton with `Counter` / `Gauge` / `Timer` handles
    (decorator, context-manager, and direct APIs).
  - `StatsDProvider` (zero-dependency UDP wire format).
  - `SentryProvider` (optional `metric-sentry` extra).
  - `PrometheusPushProvider` (optional `metric-prometheus` extra).
- **`bedrock.contrib.storage`** — unified storage service with pluggable backends:
  - `LocalBackend` (atomic writes, metadata sidecars, path-traversal containment).
  - `S3Backend` (canned-ACL validation, SigV4 presigned URLs, CDN host rewriting;
    optional `storage-s3` extra).
  - Key normalization rejecting `..` segments; signed/access/preview URL helpers.

### Fixes

- `bedrock app inspect` validates optional installation hooks without executing them (MCK-1).

### Security

- StatsD provider sanitizes metric names and tags against wire-format delimiter
  injection and rejects non-finite values.

## [0.2.0] - 2026-08-13

### Breaking changes

- **`/api/chat` contract** (docs-web): the endpoint now accepts
  `{ query, thread_id?, device_id, context? }` instead of a full `messages`
  array. Conversation history is managed server-side per thread; clients must
  send `device_id` (a client-generated ownership key — not an auth boundary)
  and store the server-issued `X-Thread-Id` response header for follow-ups.
  Body cap is **16 KiB** (was 64 KiB). `query` is at most 2,000 characters.
  `thread_id` must be a UUID when present; an unknown explicit id is `404`
  (`thread_not_found`) and does not mint a Durable Object. A concurrent
  request on the same thread is `409` (`thread_busy`).
- **Query limit** (`bedrock.database.service`): `build_query()` and
  `search_filter_sort_paginate()` now reject `limit=0` (previously the
  "unlimited" sentinel with `show_all=True`), booleans, and other non-integers
  with `InvalidQueryLimitError`. `limit` must be an integer in `1..1000` (M-6).
- **Redis cache `clear()`** (`bedrock.contrib.cache.backends.redis`):
  `clear()`/`aclear()` raise `CacheClearRequiresPrefixError` only when **both**
  the configured key prefix and the explicit `prefix` argument are empty
  (instead of calling `flushdb()`). An explicit `prefix` is honored even
  without a configured `CACHE_REDIS_KEY_PREFIX` (M-5).
- **Cache backend registration** (`bedrock.contrib.cache`): `register_backend`
  and `list_backends` are no longer exported from `bedrock.contrib.cache.base`.
  The package root (`bedrock.contrib.cache`) still exports `register_backend`
  only; list registered names via `cache.list_backends()` on the `CacheService`
  singleton (L-8).

### Fixes

- `DatabaseManager` no longer raises `AttributeError` on `_database_url` before
  `init()` (H-1).
- `bedrock-cli init` no longer crashes with a `ValueError` traceback when the
  output directory is a symlink, e.g. `/tmp`/`/var` on macOS (M-1).
- docs-web token budget now charges straddling reservations to the new window
  (L-1) and charges the full reservation when the provider omits usage (L-3).
- docs-web markdown sanitizer rejects protocol-relative URLs (L-5).

## [0.1.2] - 2026-05-23

### Fixed

- cli: populate apps on `bedrock app install` to ensure module state is ready
- database: allow calling `get_primary_keys` as a classmethod on `BedrockModel` classes
- database: correct type hint for `_registry` in `migrations/env.py` using quoted type annotations
- logging: update return type of `get_logger` to return `Logger` instead of `logger` module instance

## [0.1.1] - 2026-05-18

### Added

- CLI: `bedrock playbook` command for running module playbooks, with optional path argument
- Database: model-level full-text search support in `search_filter_sort_paginate`

### Fixed

- docs: `uv sync` corrected to `uv sync --all-packages` in `AGENTS.md`, `README.md`, and
  contributing guides — plain `uv sync` does not install workspace packages, breaking fresh
  contributor and CI setups
- database: removed `from __future__ import annotations` from `migrations_manager.py` and
  `migrations/env.py` per project policy; `TYPE_CHECKING`-guarded annotations quoted for
  runtime safety
- di: `_scope.py` now raises `ScopeError` instead of bare `RuntimeError` when writing to an
  inactive scope
- database: `DatabaseNotConfiguredError` now subclasses `ImproperlyConfigured` (BedrockExc
  hierarchy) with an explicit `detail` message
- database: `MigrationError` now subclasses `BedrockExc` with `detail` attribute;
  `BranchOwnershipError` inherits correctly
- database: six silent `except Exception` blocks in `MigrationsManager` now log at `DEBUG`
  level with `exc_info=True` instead of silently suppressing errors
- module: `populate()` re-entrancy guard now raises `ModuleLifecycleError` instead of bare
  `RuntimeError`
- module: invalid bootstrap hook signatures now raise `InvalidModuleCallableError` instead of
  bare `TypeError`
- database: `search_filter_sort_paginate` `model` parameter tightened from `Any` to
  `type[BedrockModel]`
- cli: `install` command implemented; `run` positional argument parsing fixed

### Changed

- database: filter and helper typing normalised across `service.py` and related modules
- hooks: `HookRegistry.namespace()` return type narrowed from `Any` to `HookNamespace`
- common: `DictManager.all()` return type corrected from `Iterable[str, str]` to
  `ItemsView[str, str]`
- utils: all functions in `inspect_func` fully annotated with strict type hints

### Documentation

- Aligned database README examples with the current API
- Updated all module structure references from `modules/` to `my_app/`
- Updated homepage and PyPI links in `pyproject.toml`

## [0.1.0] - 2026-05-08

### Added

- Module registry with manifest-driven dependency resolution and lifecycle hooks
- SQLAlchemy 2.0 database layer with Alembic migrations support
- Cache system with memory and Redis backends, distributed locks, and typed schemas
- Signal/event system (blinker-derived) with sync and async dispatch
- Typer-based CLI for module and database management
- Utility library: lazy loading, proxy objects, introspection, string helpers
- SettingsProxy for optional deferred environment reads on module-level singletons
- Exception hierarchy with `BedrockExc` base and `detail` attribute pattern
- Pydantic entity base class with `arbitrary_types_allowed`
- Fumadocs documentation site (`docs-web/`)
