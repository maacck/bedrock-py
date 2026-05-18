# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
