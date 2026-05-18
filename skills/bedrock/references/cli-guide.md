# Bedrock CLI Reference

CLI commands for building and managing Bedrock applications.

## Invocation

```bash
uv run bedrock <command> [options]
```

---

## bedrock run

Run commands declared by your Bedrock modules.

### Usage

```bash
bedrock run --app <module_import_path> <subcommand> [args...]
bedrock run -a <module_import_path> <subcommand> [args...]
```

### Example

Given a module `myapp.users` with `commands: commands:app` in its manifest:

```bash
bedrock run --app myapp.users create-user --name Alice --email alice@example.com
bedrock run --app myapp.users list-users
```

---

## bedrock manage install

Install a module: run database migrations, then execute installation hooks.

### Usage

```bash
bedrock manage install [options]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `-A <module>` | `BEDROCK_APP` env var | Module import path to install |
| `--skip-migrations` | false | Skip Alembic migration step |

### What Happens

For each module (and its dependencies):
1. Run Alembic migrations (create/upgrade schema)
2. Execute `installation.py` hooks: `pre_install()` → `install()` → `post_install()`

### Examples

```bash
bedrock manage install
bedrock manage install -A myapp.users
bedrock manage install -A myapp.users --skip-migrations
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

Checks: manifest.yaml validity, bootstrap.py importability, models.py importability, installation hooks.

---

## bedrock app playbook

Read documentation from a module's playbook.

```bash
bedrock app playbook <module> [path]
```

Without a path, prints `playbook/PLAYBOOK.md`. With a path, prints the specified file from the `playbook/` directory. Path traversal is blocked for security.

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
bedrock db downgrade myapp.users -1
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

## bedrock db history

Show migration history.

```bash
bedrock db history myapp.users
```

---

## bedrock db uninstall

Downgrade to base and clean up. Use when removing a module.

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
bedrock run --app myapp.users create-user --name Alice --email alice@example.com
bedrock run --app myapp.users list-users
```
