# Module Hierarchy Reference

Bedrock organizes code into three levels: Project, Submodule, Domain Module. This is a **development convention**, not a runtime enforcement mechanism.

## Three Module Types

| Type | Has manifest.yaml | Participates in lifecycle | Purpose |
|------|-------------------|---------------------------|---------|
| Project | Optional | No (unless it has a manifest) | Top-level organizational container |
| Submodule | Yes | Yes | Business-level module with full Bedrock integration |
| Domain Module | No | No | Code organization convention within a submodule |

## Project

Top-level directory for your application. Serves as an organizational container.

A project can be a plain Python package (no `manifest.yaml`) or a Bedrock module (with `manifest.yaml`). It contains submodules as subdirectories.

```python
# my_app/__init__.py — just a package marker, no lifecycle hooks
```

## Submodule

Business-level module that participates in Bedrock's module lifecycle.

- MUST have `manifest.yaml` at its root
- Loaded by `ModuleRegistry.install()` or `ModuleRegistry.populate()`
- Can declare dependencies on other submodules via `depends_on`

```yaml
# oms/manifest.yaml
title: Order Management System
description: Handles orders, fulfillment, and returns
version: 0.1.0
depends_on:
  - my_app.auth
commands: "commands:app"
```

## Domain Module

Code organization convention WITHIN a submodule. No `manifest.yaml`. Invisible to Bedrock's module system.

Recommended files (all optional):

| File | Purpose |
|------|---------|
| `entities.py` | Pydantic models for validation |
| `service.py` | Business logic |
| `exc.py` | Module exceptions |

Domain modules are just Python packages. Import them normally:

```python
from my_app.oms.sales_order.service import create_order
```

## Directory Structure

```text
my_app/                          # Project
├── __init__.py
├── settings.py
├── oms/                         # Submodule
│   ├── __init__.py
│   ├── manifest.yaml
│   ├── bootstrap.py
│   ├── sales_order/             # Domain Module
│   │   ├── __init__.py
│   │   ├── entities.py
│   │   └── service.py
│   └── purchase_order/          # Domain Module
│       ├── __init__.py
│       ├── entities.py
│       └── service.py
└── auth/                        # Submodule
    ├── __init__.py
    ├── manifest.yaml
    └── ...
```

## Cross-Project Dependencies

A separate runtime (e.g., a worker) can depend on submodules from another project via `depends_on`. When both packages are on the Python path, `depends_on` resolves import paths normally.

```text
my_app_worker/                   # Another project (e.g., a worker runtime)
├── __init__.py
├── manifest.yaml               # depends_on: ["my_app.oms", "my_app.auth"]
└── ...
```

```yaml
# my_app_worker/manifest.yaml
title: Background Worker
version: 0.1.0
depends_on:
  - my_app.oms
  - my_app.auth
```

## Convention, Not Enforcement

- `ModuleRegistry` does not distinguish between Project, Submodule, or Domain Module.
- All modules with a `manifest.yaml` are treated identically at runtime.
- The hierarchy helps teams organize code predictably. It does not affect loading, dependency resolution, or lifecycle behavior.
