# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
