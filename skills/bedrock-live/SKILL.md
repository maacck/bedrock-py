---
name: bedrock-live
description: >
  Use when developing applications with Python bedrock-core and consulting the live Bedrock
  documentation for module design, lifecycle hooks, database sessions, pytest fixtures,
  contrib packages, or integrations.
---

# Bedrock application development

## Feature index

| Application task | Guide |
| --- | --- |
| Create a module, write its manifest, or wire startup and shutdown | [Module Anatomy](https://bedrock-py.com/llms.mdx/docs/guides/module-anatomy) |
| Decide whether a domain package needs its own manifest | [Module Hierarchy](https://bedrock-py.com/llms.mdx/docs/guides/module-hierarchy) |
| Test a module's lifecycle, replace a dependency, or roll back database writes | [Testing](https://bedrock-py.com/llms.mdx/docs/guides/testing) · fixture choices below |
| Manage service lifetimes or select an implementation by name | [Dependency Injection](https://bedrock-py.com/llms.mdx/docs/guides/di) · [Class Registry](https://bedrock-py.com/llms.mdx/docs/guides/class-registry) · [Providers](https://bedrock-py.com/llms.mdx/docs/guides/provider) |
| Collect results from extensions or notify event listeners | [Hooks](https://bedrock-py.com/llms.mdx/docs/guides/hooks) · [Signals](https://bedrock-py.com/llms.mdx/docs/guides/signals) |
| Define models, bind sessions, query data, or migrate a module | [Database](https://bedrock-py.com/llms.mdx/docs/guides/database) |
| Run module commands or document a reusable module | [Runtime CLI](https://bedrock-py.com/llms.mdx/docs/guides/cli) · [Playbooks](https://bedrock-py.com/llms.mdx/docs/guides/playbooks) |
| Use first-party contrib packages | [Cache and locks](https://bedrock-py.com/llms.mdx/docs/guides/cache) · [Metrics](https://bedrock-py.com/llms.mdx/docs/guides/metric) · [Storage](https://bedrock-py.com/llms.mdx/docs/guides/storage) · [Rate limiting](https://bedrock-py.com/llms.mdx/docs/guides/rate-limit) |
| Serve a module over HTTP or run it in a worker | [FastAPI](https://bedrock-py.com/llms.mdx/docs/integrations/fastapi) · [Celery](https://bedrock-py.com/llms.mdx/docs/integrations/celery) |

## Testing your application

Pure business logic needs no Bedrock runtime. When a test depends on framework state, enable the pytest plugin once in your application's root `conftest.py`: `pytest_plugins = ["bedrock.testing"]`. Choose fixtures by what needs isolation:

| Test needs | Use |
| --- | --- |
| Exercise real startup and assert load order | `bedrock_runtime` gives each test a fresh registry and recorded lifecycle events. Add `module_factory` when you need temporary importable modules with specific dependencies. |
| Substitute a dependency or exercise hook registration | Use `override_service` for a DI replacement, `di_container` for isolated wiring, or `hook_namespace` for an isolated extension point. |
| Check writes even when a service commits | Initialize a test database first, then request `db_session`. Bedrock-managed sessions share the rollback transaction; raw engine connections bypass it. If the service reads `db.session`, call it inside `with db.session_scope():` — the fixture does not bind it for you. |
| Exercise async database or lifecycle code | Wrap `arollback_session()` in an async fixture for database writes. If the test starts the runtime on its event loop, `await bedrock.ashutdown()` before the test returns. |

## Where to look

- **For a topic not listed above**, open the [documentation index](https://bedrock-py.com/llms.txt) and read the relevant guide.
- **For a symbol or command**, search `https://bedrock-py.com/api/search?query=<URL-encoded term>&locale=en`. Resolve relative result URLs against `https://bedrock-py.com` and read the matching section, not just the snippet.
- **For an installed contrib module:** `bedrock app playbook <module-import-path>` gives guidance shipped with the package. For project scaffolding or code generation, use the `bedrock-cli` skill if installed.
- **For exhaustive cross-page searches only:** save [llms-full.txt](https://bedrock-py.com/llms-full.txt) locally and search it; read the matches, not the entire export. If offline, use the bundled `bedrock` skill if available, otherwise inspect the installed package and CLI help.

## Which mechanism to choose

| What does the caller need? | Start with |
| --- | --- |
| One known service in a declared dependency | Call the service directly. |
| An instance with a managed lifetime or a test override | DI container. |
| A named implementation **class** | `ClassRegistry`. |
| A named provider that creates an **instance** | `ProviderCatalog`. |
| An extension point with ordered implementations or a result | Hooks. Dispatch does not roll back earlier side effects. |
| A notification whose sender does not need listeners' results | Signals. Do not depend on receiver order. |

## When not to copy an example

- Compare the `bedrock-core` version in [llms.txt](https://bedrock-py.com/llms.txt) with `python -c "from importlib.metadata import version; print(version('bedrock-core'))"` in the application's environment. If they differ, read the [changelog](https://bedrock-py.com/llms.mdx/docs/changelog) entries between the installed and target versions for breaking changes and migration steps. Do not treat `Unreleased` as shipped or upgrade the dependency just to match the docs. When unsure, verify the installed signature or CLI `--help`.
- A guide's full directory tree is not a list of required files. Check Module Anatomy and Module Hierarchy before introducing a manifest or a new layer. Keep HTTP and worker types at the adapter boundary.
- Before reusing lifecycle, session, or hook examples, check who owns setup, commit/rollback and shutdown, whether the entry point is async, and what happens on failure. A sync CLI example is not a transaction or lifecycle contract for a web request or worker.
