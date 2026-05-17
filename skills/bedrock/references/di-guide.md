# Dependency Injection Guide

Reference for Bedrock's dependency injection container in `bedrock.di`.

## Table of Contents

1. [Core API](#core-api)
2. [Lifetimes](#lifetimes)
3. [Registering Services](#registering-services)
4. [Resolving Services](#resolving-services)
5. [Scoped Services](#scoped-services)
6. [Overrides for Tests](#overrides-for-tests)
7. [Decorators](#decorators)
8. [Using DI in Modules](#using-di-in-modules)
9. [Testing Utilities](#testing-utilities)
10. [Anti-Patterns](#anti-patterns)

---

## Core API

**Module**: `bedrock.di`

### Public exports

```python
from bedrock.di import Container, Lifetime, container, provider, inject
```

### Global singleton

| Name | Type | Import |
|------|------|--------|
| `container` | `Container` | `from bedrock.di import container` |

### Default vs custom container

- Prefer the default `container` for normal module/application wiring
- Create `Container()` manually only when you need an isolated service graph
- Good custom-container scenarios: tests, sandboxes, plugin isolation, short-lived worker flows
- If you do not need isolation, stay on the default container for consistency with Bedrock bootstrap injection

---

## Lifetimes

| Lifetime | Behavior | Use Case |
|----------|----------|----------|
| `SINGLETON` | Create once, cache forever | shared managers, clients, service singletons |
| `TRANSIENT` | Create on every `resolve()` | stateless helpers |
| `SCOPED` | Reuse within active `container.scope()` | request-local sessions, unit-of-work objects |

### Rules

- Use `SINGLETON` for long-lived shared services
- Use `TRANSIENT` when each caller should get a fresh instance
- Use `SCOPED` only when an explicit scope boundary exists
- Scoped services raise `ScopeError` if resolved without an active scope

---

## Registering Services

### `container.register(key, *, factory, lifetime=Lifetime.SINGLETON)`

Register a factory under a type key or string key.

```python
from bedrock.di import Lifetime, container


class InventoryService:
    def check(self, sku: str) -> int:
        return 10


container.register(
    InventoryService,
    factory=InventoryService,
    lifetime=Lifetime.SINGLETON,
)
```

### `container.register_instance(key, instance)`

Register a pre-built object as a singleton.

```python
config = {"region": "ap-southeast-1"}
container.register_instance("app.config", config)
```

### Registering into a custom container

```python
from bedrock.di import Container, Lifetime

custom = Container()
custom.register(InventoryService, factory=InventoryService, lifetime=Lifetime.SINGLETON)
custom.register_instance("app.config", {"region": "sandbox"})
```

### Duplicate registration

Both `register()` and `register_instance()` raise `DuplicateServiceError` if the key already exists.

---

## Resolving Services

### `container.resolve(key)`

```python
service = container.resolve(InventoryService)
config = container.resolve("app.config")
```

### `container.is_registered(key)`

```python
if container.is_registered(InventoryService):
    service = container.resolve(InventoryService)
```

### Failure mode

Resolving an unknown key raises `ServiceNotFoundError`.

---

## Scoped Services

### `container.scope(name="default")`

Use a context manager to create a scope boundary.

```python
from bedrock.di import Lifetime, container


class Session:
    def close(self) -> None:
        print("closed")


container.register(Session, factory=Session, lifetime=Lifetime.SCOPED)

with container.scope("request"):
    first = container.resolve(Session)
    second = container.resolve(Session)
    assert first is second
```

### Cleanup

When the scope exits, Bedrock calls `.close()` on scoped objects that provide it.

---

## Overrides for Tests

### `container.override(key, instance)`

Temporarily replace a registration.

```python
from bedrock.di import container


class Mailer:
    def send(self, to: str, subject: str, body: str) -> None:
        raise NotImplementedError


class FakeMailer(Mailer):
    def send(self, to: str, subject: str, body: str) -> None:
        print(to)


with container.override(Mailer, FakeMailer()):
    mailer = container.resolve(Mailer)
```

### Notes

- Overrides restore the previous registration on exit
- Overrides also work for keys that were not previously registered
- Nested overrides are supported

---

## Decorators

### `@provider`

Register a class in the default global container.

```python
from bedrock.di import Lifetime, provider


@provider(lifetime=Lifetime.SINGLETON)
class AuditService:
    def write(self, message: str) -> None:
        print(message)
```

### `custom.provider`

Bind decorator-based registration to a specific custom container.

```python
from bedrock.di import Container, Lifetime

custom = Container()


class AuditService:
    def write(self, message: str) -> None:
        print(message)


@custom.provider(lifetime=Lifetime.SINGLETON)
class CustomAuditService(AuditService):
    pass


@custom.provider("audit.name")
class AuditName:
    def __str__(self) -> str:
        return "sandbox"
```

### `@inject(**mappings)`

Resolve named keyword-only dependencies from the default global container.

```python
from bedrock.di import inject


class Mailer:
    def send(self, to: str, subject: str, body: str) -> None:
        print(to)


@inject(mailer=Mailer)
def send_welcome(user_email: str, *, mailer: Mailer) -> None:
    mailer.send(user_email, "Welcome", "Hello")
```

Explicit kwargs override injected values.

### `custom.inject(**mappings)`

Resolve dependencies from a specific custom container only.

```python
from bedrock.di import Container

custom = Container()
custom.register_instance(Mailer, Mailer())


@custom.inject(mailer=Mailer)
def send_preview(user_email: str, *, mailer: Mailer) -> None:
    mailer.send(user_email, "Preview", "Hello")
```

The top-level `provider` and `inject` exports remain aliases for the default
global container.

---

## Using DI in Modules

The most common registration point is `bootstrap.py`.

```python
from bedrock.di import Lifetime


class InventoryService:
    def check(self, sku: str) -> int:
        return 10


def on_load(*, container) -> None:
    container.register(
        InventoryService,
        factory=InventoryService,
        lifetime=Lifetime.SINGLETON,
    )
```

### Bootstrap injection

Bootstrap hooks may request `container` by name:

- `registry`
- `app`
- `container`
- `hooks`

Declare only the parameters you need — the registry injects only what it finds in the signature.

---

## Testing Utilities

**Package**: `bedrock.testing`

Available DI fixtures:

- `clean_container`
- `di_container`
- `override_service`

---

## Anti-Patterns

- Do not turn the container into a hidden service locator for everything
- Do not register unrelated services eagerly at import time
- Do not use DI where direct local construction is clearer
- Do not use `SCOPED` without a real scope boundary
- Do not introduce a custom `Container()` unless isolation is part of the requirement

---

## See Also

- `references/module-guide.md`
- `references/signals-guide.md`
- `references/architecture.md`
