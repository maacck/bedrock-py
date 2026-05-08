# AGENTS.md — bedrock core runtime

## OVERVIEW

Framework-agnostic modular runtime for Python applications. Handles module discovery, dependency resolution, lifecycle orchestration, and provides first-party contrib modules. Not a web framework. Usable from ASGI apps, Celery workers, CLI scripts, and anything else.

Entry point: `bedrock.setup()` bootstraps the `ModuleRegistry` singleton (`apps`). Call it once at process start.

## STRUCTURE

```
src/bedrock/
├── cli/           # Typer commands: run, app, db, manage
├── common/        # ClassRegistry, DictManager (dynamic class lookup)
├── contrib/       # First-party modules: cache (active), celery_tasks (skeletal)
├── database/      # SQLAlchemy 2.0 layer: base model, manager, session factory, migrations
├── module/        # Core runtime: registry, manifest parser, entities, lifecycle signals
├── signal/        # Custom blinker-derived signal system (sync + async)
├── utils/         # lazyload, proxy_obj, string_helpers, inspect_func
├── conf.py        # LazySettings proxy (defers BaseSettings init until first access)
├── constants.py   # StrEnum base class
├── entities.py    # BedrockEntity (Pydantic BaseModel base)
├── exc.py         # BedrockExc hierarchy (detail attribute pattern)
├── logging.py     # Loguru-based logger factory
└── settings.py    # BedrockSettings, BedrockRuntimeSettings (env-prefixed)
```

## WHERE TO LOOK

- Adding a new module lifecycle hook: `module/registry.py` (`_call_hook`), `module/signals.py`
- Changing manifest schema: `module/entities.py` (`ModuleManifest`)
- Database model base: `database/base.py` (`BedrockModel`, `CrudMixin`)
- Database session management: `database/manager.py` (`DatabaseManager`, `db` singleton)
- Cache backend registration: `contrib/cache/service.py` (`_BACKEND_REGISTRY`)
- Settings pattern: `conf.py` (`LazySettings`, `_LazySettingsProxy`)
- Dynamic imports: `utils/lazyload.py` (`load_string`, `load_callable`, `cached_import`)
- Signal system: `signal/base.py` (`Signal`, `Namespace`, `ANY`)
- Exception hierarchy: `exc.py` (base), `module/exc.py` (module-specific)

## CONVENTIONS

- **Singletons**: `apps` (ModuleRegistry), `db` (DatabaseManager), `cache` (CacheService). Import from their modules, not from `__init__`.
- **Async prefix**: async methods use `a` prefix: `aget`, `aset`, `adelete`, `aclear`, `asend`.
- **Exception pattern**: subclass `BedrockExc`, set a `detail` class attribute, accept optional `msg` override in `__init__`.
- **Settings**: extend `LazySettings` (not `BaseSettings` directly). Use `env_prefix` in `model_config`.
- **Manifests**: YAML files at package root. Parsed by `ModuleManifest` (Pydantic model). Required fields: `title`, `version`.
- **Bootstrap hooks**: optional `bootstrap.py` in module packages. Hook names: `on_load`, `ready`, `on_shutdown`. Called by registry, not imported manually.
- **Contrib modules**: live under `contrib/`, own their `manifest.yaml`, follow same lifecycle as business modules.
- **Entities**: extend `BedrockEntity` (Pydantic `BaseModel` with `arbitrary_types_allowed`).
- **Database models**: extend `BedrockModel` (SQLAlchemy `DeclarativeBase` + `CrudMixin`).
- **Line length**: 120 characters. Google-style docstrings. Strict type hints on all public functions.

## ANTI-PATTERNS

- Do not import `settings` at module level if it triggers early env reads. The lazy proxy exists for this reason.
- Do not call `db.init()` or `apps.populate()` more than once per process. Both are not idempotent in all paths.
- Do not bypass `ModuleRegistry.install()` to load modules manually. Dependency resolution and lifecycle hooks will be skipped.
- Do not put HTTP-specific types (Request, Response) in service or entity layers. Keep them at adapter boundaries.
- Do not subclass `BedrockExc` without setting a `detail` string. Error messages depend on it.
- Do not use `BaseSettings` directly for new settings classes. Always extend `LazySettings` to preserve deferred init.
- Do not hardcode cache backend names. Use `register_backend()` and the `ClassRegistry` pattern.
- Do not import `contrib` modules from core code. Core must not depend on contrib.
