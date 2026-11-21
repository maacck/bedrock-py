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

## bedrock app install

Install one module. By default only TARGET is installed; its dependencies are loaded but not installed.

### Usage

```bash
bedrock app install TARGET [--root ROOT] [--with-dependencies] [--skip-migrations]
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--root ROOT` | TARGET | Root module to load (load context only; does not widen what is installed) |
| `--with-dependencies` | false | Also install TARGET's manifest dependency closure, each module once, in dependency order |
| `--skip-migrations` | false | Skip schema management only; hooks still run |

### What Happens

For each installed module:
1. If it has models and migrations are not skipped, run its migration scripts up to head (a module with models but no scripts fails; generate `0001_initial` first).
2. If `installation.py` defines `install()`, run optional `pre_install()`, then `install()`, then optional `post_install()`. Without `install()`, all three hooks are skipped.

### Examples

```bash
bedrock app install myapp.users
bedrock app install myapp.users --root myapp --skip-migrations
bedrock app install myapp --with-dependencies
```

`bedrock manage` is removed. Former `bedrock manage -A ROOT install --app TARGET` is `bedrock app install TARGET --root ROOT`; former `bedrock manage install` with `BEDROCK_APP` set is `bedrock app install "$BEDROCK_APP" --with-dependencies`.

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

Autogeneration defaults to enabled. Revisions are named `<number>_<slug>` (`0001_initial`, `0002_add_email`); the first creates the app's root, later ones follow its single head. The app must be at head, and dependencies on other modules are written automatically as qualified `app:revision` entries in `depends_on`.

The generated file is a reviewable draft; commit it after review. Runtime ordering uses only the committed `depends_on` graph, never foreign keys.

Autogeneration filters both database reflection and shared metadata to tables owned by the target module. Ordinary ORM models use their defining package, including loaded domain packages; importing a foreign model does not transfer ownership. Unmapped `Table` objects declare `info={"bedrock_module": "myapp.users"}`. Missing, conflicting, or unloaded owners raise `MigrationError` without writing a revision. See [Table Ownership](database-guide.md#table-ownership).

When a model has been removed, its old database table is not automatically claimed or dropped. Use `--no-autogenerate` to author a reviewed explicit drop migration. Historical scripts and manual revisions do not depend on a current ownership snapshot.

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

Only the specified app's migrations run, together with the migrations they depend on in other modules. Each executed step is printed as `app:revision` (or `Nothing to do.`). Upgrade and downgrade take a PostgreSQL advisory lock or MySQL/MariaDB `GET_LOCK` and wait up to `DATABASE_MIGRATION_LOCK_TIMEOUT` seconds (default 30). SQLite migrations are not locked; run them from one process.

The CLI uses one ledger (`bedrock_migrations` by default). `DATABASE_MIGRATION_VERSION_TABLE` selects another (lowercase simple SQL identifier, at most 57 characters); no CLI flag exists. Never rename the table to switch ledgers.

**Targets**: `head` (default), `+N` (upgrade N steps), or a full revision name of the app such as `0002_add_email`

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

**Targets**: `-N` (downgrade N steps), `base` (empty), or a full revision name of the app (undoes its applied descendants; the revision stays)

If applied migrations of other modules depend on what would be undone, the command fails and changes nothing. `--cascade` rolls those dependents back first; add `--load <app>` for each dependent module that is not already loaded (unloaded dependents are always an error). Baseline rows imported by `adopt` cannot be undone.

**Examples**:
```bash
bedrock db downgrade myapp.users -- -1
bedrock db downgrade myapp.users base
bedrock db downgrade myapp.users base --load myapp.orders --cascade
```

---

## bedrock db heads

Show the head revisions in the app's scripts, one per line.

```bash
bedrock db heads myapp.users
```

---

## bedrock db current

Show the applied revisions of the app, one per line (`-` when none).

```bash
bedrock db current myapp.users
```

---

## bedrock db check

Report each loaded module's current and head revisions without changing the database. Exits non-zero when any module is `behind` or `unknown`.

```bash
bedrock db check myapp.users
```

States: `at-head` (applied revisions match the heads), `behind` (an older revision is applied), `unknown` (nothing applied, no scripts, or an applied revision has no script).

---

## bedrock db history

Show migration history (revision, down revision, depends on, message).

```bash
bedrock db history myapp.users
```

---

## Removing a module (`bedrock db downgrade`)

`bedrock db uninstall` is removed. Roll the app's applied migrations back to base before removing the module:

```bash
bedrock db downgrade myapp.users base [--cascade] [--load myapp.orders]
```

Without `--cascade`, the rollback fails and changes nothing when applied migrations of other modules depend on the app's. `--cascade` rolls those dependents back first; add `--load <app>` for each dependent that is not already loaded. Unloaded dependents and baseline rows imported by `adopt` always block the command before the first DDL statement.

Review both directions of existing scripts before rolling back: it runs every applied `downgrade()`, and table operations are not sandboxed. New generation filters do not repair old destructive downgrades. Back up, check the applied revisions and actual schema state, and follow [Existing Migration Safety](database-guide.md#existing-migration-safety); adding a new revision alone cannot neutralize an older dangerous downgrade.

---

## bedrock db adopt

Import a database whose migrations are recorded in Alembic's `alembic_version` into `bedrock_migrations`. Until it runs once, every other migration command refuses to run on that database.

```bash
bedrock db adopt myapp.users myapp.orders
```

Load every module that has migrations; each old head must belong to exactly one. Writes protected `baseline` rows and publishes the table atomically; no script is executed and `alembic_version` is left untouched. Back up first and follow [Adopting an Existing Database](database-guide.md#adopting-an-existing-database).

---

## bedrock db init-ledger

```bash
bedrock db init-ledger <root> [<root> ...] --adopted-ledger <source-name>
```

For genuinely new independent modules with no prior schema or history, `bedrock db init-ledger <root> --adopted-ledger <source>` creates the configured target table empty. It verifies the prior adoption read-only; no script runs and no baseline rows are copied. Back up and rehearse on a restored copy first.

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
