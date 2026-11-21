---
name: bedrock-cli
description: >
  Use when scaffolding or generating Bedrock application code with the exploratory `bedrock-cli` package,
  especially for `bedrock-cli init`, `bedrock-cli add`, `bedrock-cli gen`, `_bedrock_gen` custom templates,
  or when a task risks confusing the scaffolding CLI with the runtime `bedrock` command.
metadata:
  bedrock-cli: "0.4.0"
---

# Bedrock CLI Scaffolding

`bedrock-cli` is the exploratory scaffolding companion for Bedrock. It is **not** the runtime `bedrock` CLI, and its command surface is not stable.

This skill describes `bedrock-cli` at the version in `metadata.bedrock-cli` above. If the installed version differs (`uv run python -c "import importlib.metadata as m; print(m.version('bedrock-cli'))"`), tell the user and trust the installed `bedrock_cli` source and `bedrock-cli --help` over this skill.

## When to Use

- Scaffolding a new project with `bedrock-cli init`
- Adding a module or domain package with `bedrock-cli add`
- Generating code from SQLAlchemy models with `bedrock-cli gen`
- Working with `_bedrock_gen/<template>.py.j2` overrides
- Preventing confusion between `bedrock-cli` and runtime `bedrock`

Do **not** use this skill for runtime migrations, module inspection, or lifecycle management after scaffolding; use `bedrock` for that.

## Quick Reference

| Command | Purpose | Caveat |
|---|---|---|
| `bedrock-cli init <name>` | Scaffold a new project | Minimal starter |
| `bedrock-cli add module <name> <path>` | Add a Bedrock module | Prefer explicit flags |
| `bedrock-cli add domain <name> <path>` | Add an internal domain package | No `manifest.yaml` |
| `bedrock-cli gen <template> <model_ref> <path>` | Generate from a SQLAlchemy model | Run from project root |
| `_bedrock_gen/<template>.py.j2` | Override/add generation templates | Checked before built-ins |

## Core Workflow

- `bedrock-cli` = scaffolding and generation
- `bedrock` = runtime management (`app`, `db`, `run`)

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
    ├── bootstrap.py
    ├── installation.py
    └── exc.py
```

This is a starter scaffold, not a list of required files. Use the [Module Hierarchy guide](https://bedrock-py.com/llms.mdx/docs/guides/module-hierarchy) to decide which packages need a manifest. Add host `settings.py` and an `app.py` entry point as needed; keep a root manifest only if the project owns lifecycle or dependency behavior.

### 2. Add modules explicitly

Prefer explicit flags:

```bash
bedrock-cli add module users src/my_saas_app --bootstrap --installation
bedrock-cli add domain profile src/my_saas_app/users
```

- `add module` creates `__init__.py`, `manifest.yaml`, `entities.py`, `exc.py`, plus optional `bootstrap.py` and `installation.py`; omitted flags prompt interactively with a default of no. It creates no `models.py`: add one only when the module owns tables
- `add domain` creates `__init__.py`, `entities.py`, `service.py`, and `exc.py`; it is an internal domain package, not a Bedrock module
- Generated files are starters: add a module `service.py` for business logic and remove optional placeholders for concerns the module does not own.

### 3. Generate code from models

```bash
bedrock-cli gen entity my_saas_app.users.models:User src/my_saas_app/users/profile --overwrite
bedrock-cli gen service my_saas_app.users.models:User src/my_saas_app/users/profile --overwrite
```

- Define `User` in `my_saas_app.users.models` first; the scaffold creates no `models.py`.
- Run from project root so `.` and `./src` are importable; the model's dependencies must also be installed.
- `model_ref` must be `<module.path>:<ClassName>`
- Built-ins are `entity` and `service`
- Output file is always `<template>.py`; `entity` therefore writes `entity.py`, not the canonical `entities.py`. Move its contents into `entities.py` and remove `entity.py`.
- `--overwrite` is needed here because `add domain` made the destination non-empty; it also replaces the domain's existing `service.py`. The generated service returns ORM objects; adapt it to the entity-based service interface in the [Module Anatomy guide](https://bedrock-py.com/llms.mdx/docs/guides/module-anatomy).

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
| Assuming a command or flag is stable | Check `bedrock-cli <command> --help` in the application's environment |
| Omitting explicit add flags in scripted examples | Use `--bootstrap/--no-bootstrap` and `--installation/--no-installation` |
| Running `gen` outside project root | Run it where `_bedrock_gen/`, `src/`, and your model package are visible |
| Forgetting overwrite semantics | Without `--overwrite`, any non-empty destination fails, even if the target file does not exist |
