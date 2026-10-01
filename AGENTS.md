# AGENTS.md

Agent guidance for the Bedrock monorepo.

## Language Policy

- **Code**: English only (comments, docstrings, variables, docs)
- **Interactions**: Match user's language

## Vision

**MUST read** [`VISION.md`](VISION.md) before any of: feature design, implementation planning, new contrib, public API change, architectural change.

Completion: the proposal names the Vision section it serves and passes the Feature Planning Gate below. If it fails the gate, stop and say so.

### Feature Planning Gate

Every feature design or implementation plan must answer all of the following. If any answer is no, the work is out of vision: stop and say so.

| # | Check | Pass |
| --- | --- | --- |
| 1 | **Runtime or shared contrib.** It strengthens the modular runtime, conventions, or a first-party capability other modules reuse — not a single application's domain. | |
| 2 | **Adapter at the edge.** HTTP, worker, and CLI types stay out of core, `service.py`, and `entities.py`. Web-related work ships as a separate package, not `bedrock-core`. | |
| 3 | **Convention.** It follows [`docs/bedrock-anatomy.md`](docs/bedrock-anatomy.md). A new parallel pattern needs an explicit reason in the design. | |
| 4 | **Deepen before widen.** It tightens an existing contract (lifecycle, provider/adapter, cache, storage, metrics, database, testing) before adding a sibling API or contrib. | |
| 5 | **Real caller.** A new contrib has a caller that needs it, or it is infrastructure several applications would otherwise rewrite. Speculative contrib is out of vision. | |
| 6 | **Core stays domain-agnostic.** Shipping it does not require Bedrock to know about a particular application's domain. | |

Record the check results in the design or plan (a short table is enough). A plan that skips `VISION.md` is incomplete.

## Anatomy

**MUST read** [`docs/bedrock-anatomy.md`](docs/bedrock-anatomy.md) before creating or changing a Bedrock project, module, contrib package, scaffold, adapter, manifest, or lifecycle hook.

Completion: every affected path follows the document's runtime contract, file responsibilities, dependency direction, and review checklist. Other guides and skills may explain the anatomy; they must not define a competing one.

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
