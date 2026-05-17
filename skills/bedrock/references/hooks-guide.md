# Hook System Guide

Reference for Bedrock's structured hook system in `bedrock.hooks`.

## Table of Contents

1. [Core API](#core-api)
2. [Key Concepts](#key-concepts)
3. [Declaring Hook Specs](#declaring-hook-specs)
4. [Registering Implementations](#registering-implementations)
5. [Calling Hooks](#calling-hooks)
6. [Scanning Classes and Modules](#scanning-classes-and-modules)
7. [Using Hooks in Modules](#using-hooks-in-modules)
8. [Validation and Introspection](#validation-and-introspection)
9. [Signals vs Hooks](#signals-vs-hooks)
10. [Testing Utilities](#testing-utilities)
11. [Anti-Patterns](#anti-patterns)

---

## Core API

**Module**: `bedrock.hooks`

### Public exports

```python
from bedrock.hooks import HookRegistry, HookNamespace, hooks, hookspec, hookimpl
```

### Global singleton

| Name | Type | Import |
|------|------|--------|
| `hooks` | `HookRegistry` | `from bedrock.hooks import hooks` |

---

## Key Concepts

### Hook registry

`HookRegistry` stores hook specs and implementations by fully-qualified name.

### Hook namespace

`HookNamespace("auth")` scopes hook names under a module-owned namespace:

- `auth.authenticate`
- `auth.get_permissions`

### Hookspec vs hookimpl

| Concept | Purpose |
|---------|---------|
| `@hookspec` | Declares an extension point |
| `@hookimpl` | Provides one implementation of that extension point |

### Priority and firstresult

- Lower `priority` values run first
- `firstresult=True` stops dispatch after the first non-`None` result

---

## Declaring Hook Specs

```python
from bedrock.hooks import HookNamespace

auth_hooks = HookNamespace("auth")


@auth_hooks.spec(firstresult=True)
def authenticate(token: str) -> str | None:
    ...


@auth_hooks.spec
def get_permissions(user_id: str) -> list[str]:
    ...
```

### Rules

- The spec owner defines the namespace
- Specs document the extension contract
- Use `firstresult=True` only when the caller wants exactly one winning implementation

---

## Registering Implementations

### Direct registration with `@namespace.impl`

```python
from bedrock.hooks import HookNamespace

payment_hooks = HookNamespace("payment")


@payment_hooks.impl(priority=0)
def process(method: str, amount: int) -> str | None:
    if method == "card":
        return "charged"
    return None
```

### Marker-based registration with `@hookimpl`

```python
from bedrock.hooks import hookimpl


class JwtHooks:
    @hookimpl(priority=0)
    def authenticate(self, token: str) -> str | None:
        if token == "jwt-token":
            return "user-1"
        return None
```

---

## Calling Hooks

### `namespace.call(name, **kwargs)`

```python
results = auth_hooks.call("get_permissions", user_id="user-1")
```

### `await namespace.acall(name, **kwargs)`

```python
results = await auth_hooks.acall("get_permissions", user_id="user-1")
```

### `namespace.call_robust(name, **kwargs)`

```python
results = auth_hooks.call_robust("get_permissions", user_id="user-1")
```

### Return behavior

- normal hook: returns a list of all results in priority order
- `firstresult=True`: returns a list containing the first non-`None` result only
- robust calls: return `(callable, result_or_exception)` tuples

---

## Scanning Classes and Modules

### `add_specs_from(obj)`

```python
from bedrock.hooks import HookNamespace, hookspec

search_hooks = HookNamespace("search")


class SearchSpecs:
    @hookspec
    def providers(self) -> list[str]:
        ...


search_hooks.add_specs_from(SearchSpecs)
```

### `add_impls_from(obj, module=...)`

```python
from bedrock.hooks import hookimpl


class ElasticHooks:
    @hookimpl(priority=0)
    def providers(self) -> list[str]:
        return ["elastic"]


search_hooks.add_impls_from(ElasticHooks(), module="elastic")
```

Use instances for implementation scanning so methods are bound correctly.

---

## Using Hooks in Modules

Hooks are usually wired in `bootstrap.py`.

```python
from modules.auth.hookspecs import auth_hooks


class ApiKeyHooks:
    def authenticate(self, token: str) -> str | None:
        if token == "api-key":
            return "user-2"
        return None


def on_load(*, app) -> None:
    auth_hooks.add_impls_from(ApiKeyHooks(), module=app.name)
```

### Bootstrap injection

Bootstrap hooks may request:

- `registry`
- `app`
- `container`
- `hooks`

Declare only the parameters you need — the registry injects only what it finds in the signature.

---

## Validation and Introspection

### Validation

```python
warnings = hooks.validate()
```

Warnings are emitted for:

- implementations without a matching spec
- specs with zero implementations

### Introspection

```python
auth_hooks.has_spec("authenticate")
auth_hooks.get_impls("authenticate")
auth_hooks.specs()
hooks.namespaces()
```

### Reset

```python
auth_hooks.reset()   # one namespace
hooks.reset()        # all namespaces
```

---

## Signals vs Hooks

| Need | Use |
|------|-----|
| Fire-and-forget notification | Signals |
| Ordered call/response extension point | Hooks |
| Receiver return values | Hooks |
| Decoupled event broadcast | Signals |

### Rule of thumb

- Use **signals** when the sender should not care about return values
- Use **hooks** when the caller needs results, ordering, or first-match behavior

---

## Testing Utilities

**Package**: `bedrock.testing`

Available hook fixtures:

- `clean_hooks`
- `hook_registry`
- `hook_namespace`

---

## Anti-Patterns

- Do not use hooks when a normal function call is simpler
- Do not use signals when the caller needs ordered return values
- Do not let unrelated modules own another module's namespace
- Do not register implementations into a namespace that has no meaningful owning spec module

---

## See Also

- `references/module-guide.md`
- `references/signals-guide.md`
- `references/architecture.md`
