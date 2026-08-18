# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
