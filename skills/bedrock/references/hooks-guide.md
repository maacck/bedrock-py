# Hook System Guide

Reference for Bedrock's structured hook system in `bedrock.hooks`.

## Table of Contents

1. [Core API](#core-api)
2. [Key Concepts](#key-concepts)
3. [Declaring Hook Specs](#declaring-hook-specs)
4. [Registering Implementations](#registering-implementations)
5. [Calling Hooks](#calling-hooks)
6. [Extension Points That Must All Succeed](#extension-points-that-must-all-succeed)
7. [Scanning Classes and Modules](#scanning-classes-and-modules)
8. [Using Hooks in Modules](#using-hooks-in-modules)
9. [Validation and Introspection](#validation-and-introspection)
10. [Signals vs Hooks](#signals-vs-hooks)
11. [Testing Utilities](#testing-utilities)
12. [Anti-Patterns](#anti-patterns)

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
- Equal priorities run in registration order
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
- Implementations are matched by function name, and receive all dispatch kwargs; their signatures must accept them
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

`@hookimpl` only marks a method; call `add_impls_from()` to register it.

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

Async implementations are awaited; synchronous implementations run via `asyncio.to_thread`. `call()` invokes implementations directly and does not await async implementations.

```python
results = await auth_hooks.acall("get_permissions", user_id="user-1")
```

### `namespace.call_robust(name, **kwargs)`

```python
results = auth_hooks.call_robust("get_permissions", user_id="user-1")
```

### Return behavior

- normal hook: returns a list of all results in priority order
- `firstresult=True`: returns a list containing the first non-`None` result, or an empty list
- `call_robust()` returns `(callable, result_or_exception)` tuples; with `firstresult=True`, exceptions are retained and dispatch continues until a non-`None` result
- Calling a hook without a registered spec raises `HookSpecNotFoundError`

---

## Extension Points That Must All Succeed

Hooks dispatch in priority order but provide no transaction and no error aggregation. Pick the guarantee you need:

- Keep `firstresult=False` (the default); `firstresult=True` stops at the first non-`None` result.
- Report failure by raising a `BedrockExc` subclass. Return values are collected, not interpreted: `return False` is not a failure.

### Abort and roll back database changes

```python
# orders/hooks.py: the owning module declares the spec
from bedrock.hooks import HookNamespace

order_hooks = HookNamespace("orders")


@order_hooks.spec
def before_order_placed(order): ...


# orders/service.py
from bedrock.database import db

from .hooks import order_hooks


def place_order(order):
    with db.session_scope():  # commits on success, rolls back on any exception
        order_hooks.call("before_order_placed", order=order)
        _save(order)
```

`call()` stops at the first exception, after earlier implementations have already run. Their writes are undone only if they joined the same transaction: write through `db.session`, never commit, and never use `independent_session()`. `session_scope()` raises `RuntimeError` when a session is already bound to the context; then call the hook inside the caller's existing transaction instead.

### Run every implementation, then report all failures

```python
from bedrock.exc import BedrockExc


class OrderValidationError(BedrockExc):
    detail = "Order validation failed."


results = order_hooks.call_robust("validate_order", order=order)
errors = [result for _, result in results if isinstance(result, Exception)]
if errors:
    raise OrderValidationError() from ExceptionGroup("validate_order failed", errors)
```

Use this for validation so callers see every problem at once. There is no async robust variant: in async code, `acall()` is fail-fast; to collect all failures, iterate `order_hooks.get_impls("validate_order")` (priority order) with your own `try`/`except`.

### External side effects (HTTP APIs, cache, messages)

Neither hooks nor database transactions can undo them. Split the work: a side-effect-free validation hook that must fully pass (either pattern above), then commit, then perform external effects after commit (for example via a signal or an outbox) and make them idempotent and retryable.

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
from bedrock.hooks import hookimpl


class ApiKeyHooks:
    @hookimpl
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

`validate()` returns warnings; registry startup also logs them during readiness.

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

Namespace reset removes implementations associated with registered specs; orphan implementations without specs remain until the entire registry is reset.

---

## Signals vs Hooks

See `SKILL.md` → "Choosing a Mechanism" for the decision table covering direct calls, DI, `ClassRegistry`, `ProviderCatalog`, hooks, and signals, including failure semantics.

---

## Testing Utilities

**Package**: `bedrock.testing`

Register fixtures with `pytest_plugins = ["bedrock.testing"]` in `conftest.py`, or import them from `bedrock.testing`:

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
