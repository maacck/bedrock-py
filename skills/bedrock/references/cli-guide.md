# Bedrock CLI Reference

CLI commands for building and managing Bedrock applications.

## Invocation

```bash
uv run bedrock <command> [options]
```

The `bedrock` entry point calls `configure_cli_logging()` before it dispatches a command. That removes Loguru's preconfigured stderr handler and installs Bedrock's sink, so CLI log lines are not duplicated and `LOG_*` controls CLI stderr. Host sinks registered earlier stay. Importing Bedrock from a library process does not do this. Call `configure_logging()` from that process when you want Bedrock's sink beside the handlers you already installed.

---

## bedrock run

Run commands declared by your Bedrock modules.

### Usage

```bash
bedrock run --app <module_import_path> <module_import_path> <subcommand> [args...]
bedrock run -a <module_import_path> <module_import_path> <subcommand> [args...]
```

The app option selects the root module to load; commands are grouped by each loaded module's full import path, including dependencies. `BEDROCK_APP` supplies the root when the option is omitted.

### Example

Given a module `myapp.users` with `commands: commands:app` in its manifest:

```bash
bedrock run --app myapp.users myapp.users create-user Alice alice@example.com
bedrock run --app myapp.users myapp.users list-users
```

---

## bedrock manage install

Load an application and install all loaded modules, or select one loaded module.

### Usage

```bash
bedrock manage [-A <module>] install [--app <module>] [--skip-migrations]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `-A <module>` | `BEDROCK_APP` env var | Root module to load; belongs before `install` |
| `--app <module>` | all loaded modules | Install only this loaded module |
| `--skip-migrations` | false | Skip schema management |

### What Happens

For each selected module:
1. If it has models and migrations are not skipped, ensure its schema is current (create tables and stamp head on first install, or upgrade if behind).
2. If `installation.py` defines `install()`, run optional `pre_install()`, then `install()`, then optional `post_install()`. Without `install()`, all three hooks are skipped.

### Examples

```bash
bedrock manage install
bedrock manage -A myapp.users install
bedrock manage -A myapp.users install --app myapp.users --skip-migrations
```

---
## bedrock app install

Load the specified module and its dependencies, but install only the specified module. Schema and hook behavior matches `manage install` above.

```bash
bedrock app install myapp.users [--skip-migrations]
```

---


## bedrock app info

Display module metadata.

```bash
bedrock app info <import_path>
```

**Example**: `bedrock app info myapp.users`

Shows: title, description, version, dependencies, bootstrap/models status, declared commands.

---

## bedrock app inspect

Validate a module's structure.

```bash
bedrock app inspect <import_path>
```

Checks the manifest, module loading, bootstrap and models availability, and optional installation-hook signatures without executing hooks. Declared dependencies also receive basic checks; failures cause a nonzero exit.

---

## bedrock app playbook

Read documentation from a module's playbook.

```bash
bedrock app playbook <module> [path]
```

Without a path, prints `playbook/PLAYBOOK.md`. With a path, prints the specified file from the `playbook/` directory. Absolute paths and paths containing `..` are rejected.

**Examples**:
```bash
bedrock app playbook myapp.auth
bedrock app playbook myapp.auth references/api-reference.md
bedrock app playbook myapp.auth examples/login-flow.py
```

Use this when your module `depends_on` an external module and you need to understand its API.

---

## bedrock db revision

Create a new database migration.

```bash
bedrock db revision <app> -m "description" [--autogenerate | --no-autogenerate]
```

Autogeneration defaults to enabled. The first revision creates the app's branch root; subsequent revisions append to that branch.

**Examples**:
```bash
bedrock db revision myapp.users -m "add users table"
bedrock db revision myapp.users -m "seed data" --no-autogenerate
```

---

## bedrock db upgrade

Apply migrations.

```bash
bedrock db upgrade <app> [target]
```

Only the specified app's migration branch is upgraded. Upgrade and downgrade take a PostgreSQL advisory lock or MySQL/MariaDB `GET_LOCK` and wait up to `DATABASE_MIGRATION_LOCK_TIMEOUT` seconds (default 30). SQLite migrations are not locked; run them from one process.

**Targets**: `head` (default), `+N` (upgrade N steps), `<revision_id>`

**Examples**:
```bash
bedrock db upgrade myapp.users
bedrock db upgrade myapp.users +1
```

---

## bedrock db downgrade

Roll back migrations.

```bash
bedrock db downgrade <app> <target>
```

**Targets**: `-N` (downgrade N steps), `base` (empty), `<revision_id>`

**Examples**:
```bash
bedrock db downgrade myapp.users -- -1
bedrock db downgrade myapp.users base
```

---

## bedrock db heads

Show latest revision.

```bash
bedrock db heads myapp.users
```

---

## bedrock db current

Show current database revision.

```bash
bedrock db current myapp.users
```

---

## bedrock db check

Report each loaded branch's current and head revisions without changing the database. Exits non-zero when any branch is `behind` or `unknown`.

```bash
bedrock db check myapp.users
```

States: `at-head` (applied revisions match head), `behind` (an older revision is applied), `unknown` (nothing applied, no scripts, or the applied revision is not in the branch).

---

## bedrock db history

Show migration history.

```bash
bedrock db history myapp.users
```

---

## bedrock db uninstall

Downgrade the app's migration branch to base before removing the module.

```bash
bedrock db uninstall myapp.users
```

---

## Module CLI Commands

How to create module-level CLI commands that integrate with `bedrock run`.

### Steps

1. **Create `commands.py`** in your module package with a Typer app:

```python
# myapp/users/commands.py
import typer

app = typer.Typer()


@app.command()
def create_user(name: str, email: str):
    """Create a new user."""
    print(f"Creating user: {name} <{email}>")


@app.command()
def list_users():
    """List all users."""
    print("Listing users...")
```

2. **Register in `manifest.yaml`**:

```yaml
title: users
description: User management
version: 0.1.0
commands: "commands:app"
```

3. **Invoke with `bedrock run`**:

```bash
bedrock run --app myapp.users myapp.users create-user Alice alice@example.com
bedrock run --app myapp.users myapp.users list-users
```
