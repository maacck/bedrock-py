# Bedrock Anatomy

This document is the canonical structure and layering specification for Bedrock applications and modules. It turns the direction in [`VISION.md`](../VISION.md) into file-level rules for people and coding agents.

Use it when creating or changing a Bedrock project, module, contrib package, scaffold, or adapter. Other guides and skills may explain these rules, but they must not define a competing anatomy.

## 1. Authority and terminology

Bedrock distinguishes runtime contracts from development conventions:

- **Runtime-required**: enforced by `bedrock-core`. Violating it prevents loading or breaks a runtime feature.
- **Convention-required**: required for a predictable Bedrock codebase even when Python could execute without it.
- **Conditional**: required only when the module owns the corresponding concern.
- **Optional**: useful for some modules and ignored by the runtime when absent.

When this document and current runtime behavior disagree, runtime behavior describes what executes today. This document remains the canonical target for scaffolds, documentation, and agent-generated code; the drift must be fixed rather than documented as a second convention.

Canonical terms:

- **Project**: the host Python application. It owns process configuration and chooses which Bedrock modules to load.
- **Bedrock module**: an importable Python package with a root `manifest.yaml`. It participates in dependency resolution and lifecycle.
- **Domain package**: an ordinary Python package inside a Bedrock module. It organizes a larger domain but has no `manifest.yaml` and no independent Bedrock lifecycle.
- **Adapter**: transport- or provider-specific code at an application seam, such as HTTP, CLI, worker, Redis, or S3 integration.
- **Contrib module**: first-party reusable infrastructure implemented as a Bedrock module. It follows the same manifest and lifecycle rules as application modules.

Avoid using *submodule* as a Bedrock classification: in Python it already means any importable module below another module. Say **Bedrock module** or **domain package**.

## 2. Project anatomy

A project is an organizational and process boundary, not automatically a Bedrock module.

```text
my_project/
├── pyproject.toml
├── src/
│   └── my_project/
│       ├── __init__.py
│       ├── settings.py
│       ├── app.py
│       ├── inventory/             # Bedrock module
│       │   └── ...
│       ├── billing/               # Bedrock module
│       │   └── ...
│       └── adapters/              # Process/transport adapters
│           ├── http/
│           ├── worker/
│           └── cli/
└── tests/
```

Project responsibilities:

- `settings.py` owns host-application settings and environment bindings.
- `app.py` (or an equivalent process entry point) calls `bedrock.setup()` with the root module(s).
- Bedrock modules own business capabilities.
- `adapters/` translates transport/provider input into module entities and service calls.
- Project-level tests exercise cross-module behavior and adapter integration.

A project package may also be a Bedrock module by adding `manifest.yaml`, but do so only when the project itself owns lifecycle or dependency behavior. Do not add a manifest merely to mark the project root.

## 3. Bedrock module anatomy

### 3.1 Minimal loadable module

```text
inventory/
├── __init__.py
└── manifest.yaml
```

| Path | Level | Contract |
| --- | --- | --- |
| `__init__.py` | Runtime-required | Makes the directory an importable package. Keep it free of registration and resource initialization side effects. |
| `manifest.yaml` | Runtime-required | Declares module metadata, dependencies, and optional commands. It must sit beside `__init__.py`. |

This is the minimum that `ModuleRegistry` can load. It is not the recommended shape for a business module.

### 3.2 Recommended business module

```text
inventory/
├── __init__.py
├── manifest.yaml
├── entities.py
├── service.py
├── exc.py
├── settings.py                # conditional: module owns configuration
├── models.py                  # conditional: module owns SQLAlchemy tables
├── bootstrap.py               # conditional: module owns runtime lifecycle work
├── installation.py            # conditional: module owns one-time install work
├── commands.py                # conditional: manifest declares commands
├── domains/                   # optional: split a large module by domain
│   └── stock_item/
│       ├── __init__.py
│       ├── entities.py
│       ├── service.py
│       └── exc.py
└── playbook/                  # conditional: reusable module distributed without source context
    └── PLAYBOOK.md
```

Do not generate every optional file. Create a file only when its responsibility exists. Empty hooks and placeholder layers reduce predictability rather than improving it.

### 3.3 File responsibilities

#### `__init__.py`

The module's deliberate public import surface.

- May re-export stable entities, exceptions, and service entry points.
- Must not register the module, initialize clients, connect signals, read environment eagerly, or call `bedrock.setup()`.
- Must not import optional provider SDKs merely to expose names.

#### `manifest.yaml`

The runtime-readable module declaration. See [Manifest contract](#4-manifest-contract).

#### `entities.py`

Transport-independent data contracts and value objects used by business logic.

- Extend `BedrockEntity` unless a narrower existing Bedrock type applies.
- May contain validation intrinsic to the domain value.
- Must not import HTTP request/response types, Typer contexts, worker messages, SQLAlchemy sessions, or provider SDK clients.
- Must not perform I/O or orchestration.

A root `schemas.py` is **not** part of canonical Bedrock module anatomy. Transport DTOs belong with their adapter, for example `adapters/http/schemas.py`. If a shape is shared by business logic across transports, it is an entity, not an HTTP schema.

#### `service.py`

The module's business interface and orchestration.

- Accepts primitive values or entities; returns entities, primitives, or explicit results.
- Owns business rules, transactions, and coordination of models or contrib interfaces.
- May import local `entities`, `models`, and `exc` modules.
- Must not accept or return HTTP, CLI, or worker framework objects.
- Must not construct provider-specific clients when a contrib interface exists.
- Async functions and methods use the `a` prefix when sync and async variants coexist.

A large module may split service implementation into domain packages. Keep one clear public interface rather than exposing every internal helper.

#### `exc.py`

The module's exception hierarchy.

- Business exceptions inherit from `BedrockExc`.
- Every concrete exception defines a human-readable `detail`.
- Transport adapters translate these exceptions into transport-specific errors; business code does not raise HTTP or CLI exceptions.

#### `settings.py`

Configuration owned by this module.

- Extend `BaseSettings` and use a module-specific `env_prefix`.
- Wrap module-level settings singletons with `SettingsProxy` when imports may happen before the environment is ready.
- Keep host-process configuration in the project `settings.py`; keep capability-specific configuration with its owning module.
- Do not read environment variables or construct provider clients during package import.

#### `models.py`

SQLAlchemy persistence owned by this module.

- Models extend `BedrockModel`.
- This is the canonical location for SQLAlchemy imports in a Bedrock module.
- Importing the module causes Bedrock to import `models.py` so SQLAlchemy mappings are registered.
- Models represent persistence, not request/response contracts or business orchestration.
- A module without tables must not create an empty `models.py`.

#### `bootstrap.py`

Process lifecycle integration. The runtime recognizes these synchronous hooks:

```python
def on_load(*, registry, app) -> None: ...
def ready(*, registry, app) -> None: ...
def on_shutdown(*, registry, app) -> None: ...
```

Supported injected names are `registry`, `app`, `container`, and `hooks`. A hook may declare any supported subset, or `**kwargs` to receive all injected values. Parameters must be keyword-compatible.

Hook responsibilities:

- `on_load`: register this module's providers, hook implementations, or lightweight configuration after dependencies load.
- `ready`: start work that requires the complete dependency graph.
- `on_shutdown`: release owned resources; shutdown runs in reverse install order.

Do not put request handling, seed data, migrations, or ordinary business workflows in bootstrap hooks. Current runtime hooks are synchronous; do not declare `async def` hooks until the runtime contract explicitly supports awaiting them.

#### `installation.py`

One-time installation behavior invoked by the runtime CLI, separate from process lifecycle.

Current command order is:

1. Load and ready the module dependency graph.
2. Ensure this module's database schema when `models.py` exists.
3. Call `pre_install()` when present.
4. Call required `install()`.
5. Call `post_install()` when present.

Installation hooks must accept `**kwargs` to pass module inspection, although the current installer invokes them without defined arguments. Treat `**kwargs` as forward compatibility, not as a source of current inputs.

Use installation hooks for idempotent seed or setup work that cannot be expressed as a migration. Do not rely on `installation.py:uninstall`; the current module installation contract does not invoke it.

#### `commands.py`

A Typer adapter owned by the module.

- Exposes a `typer.Typer` instance referenced by `manifest.yaml`, normally `commands:app`.
- Parses CLI input, creates entities, calls the service interface, and renders output.
- Contains no business rules or direct persistence orchestration.

#### `playbook/`

Agent-readable usage documentation for a reusable module whose source may not be present in the consuming repository.

- `PLAYBOOK.md` is the entry point.
- Add it for distributed/reused modules, not every local domain module.
- Document public imports, configuration, lifecycle requirements, errors, and concise examples.
- A playbook explains the module interface; it does not redefine Bedrock anatomy.

## 4. Manifest contract

Canonical manifest:

```yaml
title: Inventory
version: "0.2.0"
description: Inventory and stock operations
depends_on:
  - my_project.catalog
commands: commands:app
```

| Field | Runtime | Convention |
| --- | --- | --- |
| `title` | Required string | Use a human-readable module name. |
| `version` | Optional; defaults to `"unknown"` | Required explicitly. Use a quoted version string. Do not rely on the fallback. |
| `description` | Optional string | Recommended for reusable modules. State capability, not implementation. |
| `depends_on` | Optional list; defaults to `[]` | Every value is a fully qualified Python import path. Order is declaration order; runtime installs dependencies recursively. |
| `commands` | Optional string | Prefer a module-relative `module:attribute` path such as `commands:app`. The attribute must be a `Typer` instance. |

The module's runtime identity is its Python import path. It is not a `name` field in the manifest. Do not invent manifest fields unless `ModuleManifest` supports them.

## 5. Dependency and import direction

```text
adapter -> service -> entities / models / contrib interfaces
             |
             -> exc
bootstrap -> registrations and owned resources
installation -> migrations already applied, then one-time setup
```

Rules:

1. Adapters depend on module interfaces; module business code does not depend on adapters.
2. `service.py` owns orchestration. Adapters and models do not contain business workflows.
3. `entities.py` stays transport- and persistence-independent.
4. `models.py` stays persistence-focused.
5. Cross-module dependencies must appear in `manifest.yaml`; a Python import alone does not declare lifecycle ordering.
6. Import from another module's deliberate public surface. Do not reach into its implementation helpers.
7. Registration occurs through lifecycle hooks, never as an import-time side effect.

## 6. Domain packages

Use domain packages when one Bedrock module is too large for flat files but the extracted code does not need independent installation or lifecycle.

```text
orders/
├── manifest.yaml
├── service.py                  # public module interface
├── domains/
│   ├── fulfillment/
│   │   ├── entities.py
│   │   ├── service.py
│   │   └── exc.py
│   └── returns/
│       ├── entities.py
│       ├── service.py
│       └── exc.py
└── ...
```

A domain package:

- has no `manifest.yaml`;
- is invisible to `ModuleRegistry`;
- has no Bedrock bootstrap or installation hooks;
- is internal unless deliberately re-exported by the parent module.

Promote it to a Bedrock module only when it needs an independent dependency graph, lifecycle, installation, or reusable public interface.

## 7. Adapter anatomy

Adapters sit outside the business interface they expose.

```text
adapters/
└── http/
    └── inventory/
        ├── routes.py
        ├── schemas.py
        └── errors.py
```

- `routes.py` translates transport input to entities and calls `inventory.service`.
- `schemas.py` contains transport-specific request/response DTOs.
- `errors.py` maps `inventory.exc` exceptions to transport errors.

Web integrations may be published as separate Bedrock ecosystem packages. HTTP dependencies and types do not enter `bedrock-core` or business modules.

## 8. Contrib anatomy

A first-party contrib is reusable infrastructure, not a privileged exception to module rules.

It must:

- be an ordinary Bedrock module with `__init__.py` and `manifest.yaml`;
- expose a small public interface from its package root;
- keep provider-specific implementations behind that interface;
- centralize configuration, lifecycle, serialization, and error translation;
- use lazy provider loading when an optional SDK would otherwise be imported eagerly;
- include conformance tests when two or more providers implement the same interface;
- keep its provider types out of consuming business services.

Canonical conceptual shape:

```text
contrib/<capability>/
├── __init__.py              # deliberate public interface
├── manifest.yaml
├── base.py                  # provider interface/protocol
├── service.py               # manager/facade and provider selection
├── settings.py              # capability-level configuration
├── entities.py              # provider-neutral values/results
├── exc.py                   # normalized capability errors
├── bootstrap.py             # registration and resource lifecycle
└── providers/               # concrete adapters
    ├── memory.py
    └── external.py
```

Names may be omitted when the responsibility does not exist, but must not be replaced by a second registry or settings pattern. A provider seam is justified by real variation: one implementation does not require a speculative provider abstraction; two implementations do.

## 9. Review checklist

Before accepting a module or scaffold, verify:

- [ ] Exactly one package root contains its `manifest.yaml`.
- [ ] Manifest dependencies are fully qualified import paths.
- [ ] `version` is explicit even though the runtime has a fallback.
- [ ] Business input/output uses entities; transport DTOs live with adapters.
- [ ] Business rules live behind the module's service interface.
- [ ] Module configuration lives in `settings.py`, uses an environment prefix, and is lazy when imported early.
- [ ] SQLAlchemy code is localized to `models.py` or persistence internals.
- [ ] HTTP, worker, and CLI framework objects do not enter entities or services.
- [ ] Bootstrap hooks use supported keyword injection and contain lifecycle work only.
- [ ] Installation hooks are idempotent and accept `**kwargs`.
- [ ] Cross-module imports match manifest dependencies.
- [ ] Importing the package does not register modules or initialize resources.
- [ ] Optional files exist because they have behavior, not to complete a template.
- [ ] Reusable modules document their public interface in a playbook.
- [ ] Contrib providers share one interface, lifecycle, settings, and error model.
