---
name: bedrock-cli
description: >
  Use when scaffolding or generating Bedrock application code with the exploratory `bedrock-cli` package,
  especially for `bedrock-cli init`, `bedrock-cli add`, `bedrock-cli gen`, `_bedrock_gen` custom templates,
  or when a task risks confusing the scaffolding CLI with the runtime `bedrock` command.
---

# Bedrock CLI Scaffolding

`bedrock-cli` is the exploratory scaffolding companion for Bedrock. It is **not** the runtime `bedrock` CLI, and its command surface is not stable. Trust `packages/bedrock-cli/src/bedrock_cli/` over higher-level prose.

## When to Use

- Scaffolding a new project with `bedrock-cli init`
- Adding a submodule or domain module with `bedrock-cli add`
- Generating code from SQLAlchemy models with `bedrock-cli gen`
- Working with `_bedrock_gen/<template>.py.j2` overrides
- Preventing confusion between `bedrock-cli` and runtime `bedrock`

Do **not** use this skill for runtime migrations, module inspection, or lifecycle management after scaffolding; use `bedrock` for that.

## Quick Reference

| Command | Purpose | Caveat |
|---|---|---|
| `bedrock-cli init <name>` | Scaffold a new project | Minimal starter |
| `bedrock-cli add submodule <name> <path>` | Add a lifecycle module | Prefer explicit flags |
| `bedrock-cli add domain <name> <path>` | Add an internal domain module | No `manifest.yaml` |
| `bedrock-cli gen <template> <model_ref> <path>` | Generate from a SQLAlchemy model | Run from project root |
| `_bedrock_gen/<template>.py.j2` | Override/add generation templates | Checked before built-ins |

## Core Workflow

- `bedrock-cli` = scaffolding and generation
- `bedrock` = runtime management (`app`, `manage`, `db`, `run`)

### 1. Scaffold a project

```bash
bedrock-cli init my-saas-app
cd my-saas-app
uv sync
```

`init` creates:

```text
my-saas-app/
├── pyproject.toml
├── .python-version
├── README.md
└── src/my_saas_app/
    ├── __init__.py
    ├── manifest.yaml
    ├── models.py
    ├── bootstrap.py
    ├── installation.py
    └── exc.py
```

Treat this as a starter scaffold, not a complete app.

### 2. Add modules explicitly

Prefer explicit flags:

```bash
bedrock-cli add submodule users src/my_saas_app --bootstrap --installation
bedrock-cli add domain profile src/my_saas_app/users
```

- `add submodule` creates `__init__.py`, `manifest.yaml`, `models.py`, `entities.py`, `exc.py`, plus optional `bootstrap.py` and `installation.py`
- `add domain` creates `__init__.py`, `entities.py`, `service.py`, and `exc.py`

### 3. Generate code from models

```bash
bedrock-cli gen entity my_saas_app.users.models:User src/my_saas_app/users/profile
bedrock-cli gen service my_saas_app.users.models:User src/my_saas_app/users/profile
```

- Run from project root so `.` and `./src` are importable
- `model_ref` must be `<module.path>:<ClassName>`
- Built-ins are `entity` and `service`
- Output file is always `<template>.py`

Template resolution order:

1. `_bedrock_gen/<template>.py.j2`
2. built-in mapping (`entity` → `crud/entities.py.j2`, `service` → `crud/service.py.j2`)

### 4. Hand off to runtime CLI

After scaffolding, switch to `bedrock`:

```bash
bedrock app inspect my_saas_app.users
bedrock app info my_saas_app.users
```

## Common Mistakes

| Mistake | Fix |
|---|---|
| Treating `bedrock-cli` as the same thing as `bedrock` | State the distinction first |
| Presenting `bedrock-cli` as stable | Mention that `packages/bedrock-cli/README.md` calls it exploratory/planned |
| Using docs as authority when they disagree with implementation | Prefer `packages/bedrock-cli/src/bedrock_cli/commands/*.py` and templates |
| Omitting explicit add flags in scripted examples | Use `--bootstrap/--no-bootstrap` and `--installation/--no-installation` |
| Running `gen` outside project root | Run it where `_bedrock_gen/`, `src/`, and your model package are visible |
| Forgetting overwrite semantics | Without `--overwrite`, non-empty destinations and existing files fail |
