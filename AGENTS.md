# AGENTS.md

Agent guidance for the Bedrock monorepo.

## Language Policy

- **Code**: English only (comments, docstrings, variables, docs)
- **Interactions**: Match user's language

## Overview

Python monorepo for Bedrock modular framework. `uv` workspaces, `src/` layout.

- `packages/bedrock` — Core runtime (active)
- `packages/bedrock-cli` — Scaffolding CLI (planned, gitignored)
- `packages/bedrock-example` — Example app (gitignored)
- `docs-web/` — Derived fumadocs Web Doc site (don't hand-edit)
- `docs-web/content/docs` — Docs source-of-truth (MDX)
- `xboc/` — Reference prototype (gitignored, not for production)

## Commands

```bash
# Setup
uv sync --all-packages

# Build
uv build --all-packages
uv build --package bedrock

# Run
bedrock -v

# Lint (ruff configured, line-length=120)
uv run ruff check .
uv run ruff format .

# Tests (pytest, 4 test files in packages/bedrock/tests/)
uv run pytest packages/bedrock/tests/

# Docs
cd docs-web && npm run dev    # Dev server with live sync
cd docs-web && npm run build  # Production build
```

## Architecture

### Module Anatomy (Standard)

```
<name>/
├── __init__.py
├── manifest.yaml    # name, package, version, depends_on
├── entities.py      # Business domain entities (internal data flow)
├── schemas.py       # Optional: API input/output contracts (request/response DTOs)
├── service.py       # Business logic (no HTTP)
├── exc.py           # Module exceptions
└── bootstrap.py     # Optional: lifecycle hooks
```

### Key Singletons

| Instance | Class | Module |
|----------|-------|--------|
| `apps` | `ModuleRegistry` | `bedrock.module` |
| `db` | `DatabaseManager` | `bedrock.database` |

## Coding Standards

- **Line length**: 120 (ruff configured)
- **Docstrings**: Google-style mandatory
- **Type hints**: Strict, always include
- **Async prefix**: `a` (e.g., `aget()`, `aset()`, `adelete()`)
- **Exceptions**: `BedrockExc` hierarchy with `detail` attribute
- **Settings**: `Pydantic BaseSettings` with optional `SettingsProxy` for lazy singletons
- **Imports**: Relative within-package, absolute cross-package

## Exceptions
- All custom exceptions must inherit from `BedrockExc`
- Always raise exceptions which are subclasses of `BedrockExc` for Business Error.
- Always Write Human-Readable Messages in `detail` attribute of BedrockExc.

## Documentation
- Hand-write under `docs-web/content/docs/`


## Anti-Patterns (This Project)

- Never treat `bedrock-cli` commands as stable contracts
- Never use `from __future__ import annotations` in database/ modules
- Never import HTTP frameworks in core runtime
- Never use import-time side effects for module registration

## Notes

- Root `pyproject.toml` is workspace coordinator only, not application package
- `bedrock-cli` is planned/future work—current CLI lives in `packages/bedrock/src/bedrock/cli/`
- Ruff config: `[tool.ruff]` in root `pyproject.toml`
- Pytest: dev dependency, 4 test files, no conftest.py
- No CI/CD pipelines, no Docker, no deployment scripts

