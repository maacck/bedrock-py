# Bedrock CLI Reference (For Application Developers)

CLI commands you'll use when building applications with Bedrock.

## Invocation

```bash
uv run bedrock <command> [options]
```

---

## bedrock run

Run commands declared by your Bedrock modules.

### Usage

```bash
bedrock run --app <module_import_path> <module_import_path> <subcommand> [args...]
```

The app option (`--app` or `-a`) selects the root module to load; commands are grouped by each loaded module's full import path, including dependencies. `BEDROCK_APP` supplies the root when the option is omitted.

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

Read documentation from a module's playbook/ directory.

```bash
bedrock app playbook <module> [path]
```

Without a path, prints `playbook/PLAYBOOK.md` to stdout. With a path, prints the specified file from the `playbook/` directory. Absolute paths and paths containing `..` are rejected.

**Examples**:
```bash
bedrock app playbook myapp.users
bedrock app playbook myapp.users references/architecture.md
bedrock app playbook myapp.users references/templates/example.py
```

Useful for AI agents learning how to use a module.

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

Only the specified app's migration branch is upgraded.

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
