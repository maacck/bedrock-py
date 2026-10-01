# Vision

Bedrock is a **modular runtime** plus a small set of first-party contrib modules. Together they are a foundation for building Python applications.

Business logic lives in modules that declare themselves with `manifest.yaml`. Web, workers, and CLIs are adapters around the same modules. The core package stays decoupled from HTTP. Web-related capabilities, if they exist later, ship as separate packages — not inside `bedrock-core`.

## Mission

Give people and coding agents one shared convention: how modules are cut, how dependencies are declared, when lifecycle runs, and where infrastructure belongs. A person can read a project and extend it quickly. An agent generates code to the same places instead of inventing a new structure in every repository.

## Why it exists

Without a shared convention, structure drifts, layers couple, and infrastructure such as cache, storage, and metrics ends up inside business code. Each repository then maintains its own agent instructions.

Bedrock replaces that with one module anatomy, one explicit dependency graph, and one lifecycle. The result is predictable extension and thinner business modules.

The shape is borrowed from Django and Odoo: **applications are modules**, and a few batteries are first-party. It does not copy web-centric design, framework-owned business domains, or implicit registration.

## What it is for

- Boot a framework-agnostic module runtime (`bedrock.setup()`).
- Organize business modules by convention instead of inventing directories and load order.
- Reuse cross-application infrastructure through first-party contrib (cache, storage, metrics; further contrib is abstracted from real applications, not pre-built as a full batteries suite).
- Run the same business module behind a web adapter, a worker, or a CLI without changing its service layer.

## What it is not

- A web framework. HTTP types do not belong in the core package.
- A domain application. Product features belong in application modules, not in Bedrock.
- A second module system, registry, or settings stack beside the ones it already has.

## Who it is for

People who build Python applications, and the coding agents that work with them. The same conventions serve both: they are how a team reads a module, and how an agent writes one.

## Tradeoffs

Structure and file placement are unique on purpose. People and agents get one correct path. Prefer less duplicated infrastructure and thin business modules over implicit shortcuts.
