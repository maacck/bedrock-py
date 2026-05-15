# Signal System Reference

Blinker-derived event system with sync/async support and lifecycle signals. Implementation: `bedrock.signal`.

> **Need call/response with return values?** Use the hook system (`bedrock.hooks`) instead. Signals are notification-only.

## Table of Contents

1. [Creating Signals](#creating-signals)
2. [Connecting Receivers](#connecting-receivers)
3. [Sending Signals](#sending-signal)
4. [Sender Filtering](#sender-filtering)
5. [Context Managers](#context-managers)
6. [Lifecycle Signals](#lifecycle-signals)
7. [Weak References](#weak-references)
8. [Critical Pitfall: Async Receivers with Sync Send](#critical-pitfall-async-receivers-with-sync-send)
9. [Signals vs Hooks](#signals-vs-hooks)
10. [Anti-Patterns](#anti-patterns)

---

## Creating Signals

### Signal

Base class. Optional docstring for documentation:

```python
from bedrock.signal import Signal

user_created = Signal("Emitted after a new user is persisted")
payment_failed = Signal()
```

### NamedSignal

Subclass with a `name` attribute for identification and debugging:

```python
from bedrock.signal import NamedSignal

audit_event = NamedSignal("app.audit", doc="Emitted for auditable actions")
print(audit_event.name)  # "app.audit"
```

### Namespace

Dictionary-like container mapping names to `NamedSignal` instances. Signals are created on first access:

```python
from bedrock.signal import Namespace

my_signals = Namespace()

# Creates NamedSignal on first call
login_signal = my_signals.signal("user.login", doc="User logged in")

# Second call returns the same instance
assert my_signals.signal("user.login") is login_signal
```

### Default Namespace

Module-level convenience for quick signal creation:

```python
from bedrock.signal import signal

# Creates or retrieves a NamedSignal in the default namespace
cache_miss = signal("cache.miss", doc="Cache key not found")
```

---

## Connecting Receivers

A receiver is any callable accepting `sender` as its first positional argument plus optional keyword arguments.

### `signal.connect(receiver, sender, weak)`

```python
from bedrock.signal import Signal

user_created = Signal()

def log_user(sender, **kwargs):
    print(f"User created by {sender}: {kwargs.get('username')}")

# Connect to all senders (sender defaults to Signal.ANY)
user_created.connect(log_user)

# Connect to a specific sender only
user_created.connect(log_user, sender=admin_module)

# Disable weak referencing (required for closures/lambdas)
user_created.connect(log_user, weak=False)
```

Parameters:
- `receiver`: Callable to invoke on signal send.
- `sender`: `Signal.ANY` (default) or a specific object. Receiver only fires for matching sender.
- `weak`: `True` (default). Uses `weakref` to track receiver. Set to `False` for closures, lambdas, or when you need a strong reference.

Returns the receiver (allows decorator-style usage).

### `@signal.connect_via(sender)` Decorator

Connects a receiver for a specific sender. Defaults to `weak=False`:

```python
from bedrock.signal import Signal

invoice_created = Signal()

@invoice_created.connect_via(billing_module)
def handle_invoice(sender, **kwargs):
    """Only called when billing_module sends invoice_created."""
    process_invoice(kwargs["invoice"])
```

### Disconnecting

```python
user_created.disconnect(log_user)

# Disconnect from a specific sender only
user_created.disconnect(log_user, sender=admin_module)
```

---

## Sending Signals

### `signal.send(sender, **kwargs)`

Synchronous dispatch. Returns `list[tuple[receiver, return_value]]`:

```python
results = order_placed.send(current_app, order_id="ORD-042")

for receiver, result in results:
    print(f"{receiver.__name__} returned {result}")
```

### `signal.send_robust(sender, **kwargs)`

Like `send()`, but catches `Exception` subclasses from receivers. Returns exceptions in the result list instead of propagating:

```python
results = order_placed.send_robust(current_app, order_id="ORD-042")

for receiver, result in results:
    if isinstance(result, Exception):
        log.error(f"{receiver.__name__} failed: {result}")
    else:
        log.info(f"{receiver.__name__} returned {result}")
```

Note: `BaseException` subclasses (e.g. `KeyboardInterrupt`) still propagate.

### `await signal.asend(sender, **kwargs)`

Async dispatch. Awaits async receivers natively. Sync receivers are wrapped via `asyncio.to_thread` by default:

```python
results = await order_placed.asend(current_app, order_id="ORD-042")
```

### `await signal.asend_robust(sender, **kwargs)`

Async equivalent of `send_robust()`. Captures exceptions from receivers:

```python
results = await order_placed.asend_robust(current_app, order_id="ORD-042")
```

### Custom Wrappers

Both sync and async methods accept optional wrapper arguments for adapting receiver execution:

- `send()` / `send_robust()`: `_async_wrapper` parameter to adapt async receivers to sync.
- `asend()` / `asend_robust()`: `_sync_wrapper` parameter to adapt sync receivers to async (defaults to `asyncio.to_thread`).

---

## Sender Filtering

### Signal.ANY

Sentinel meaning "any sender". This is the default for `connect()`:

```python
from bedrock.signal import Signal

data_changed = Signal()

# These are equivalent:
data_changed.connect(handler)
data_changed.connect(handler, sender=Signal.ANY)
```

### Specific Sender

Pass a concrete sender to restrict which sender triggers a receiver:

```python
from bedrock.signal import Signal

data_changed = Signal()

@data_changed.connect_via(inventory_module)
def on_inventory_change(sender, **kwargs):
    """Only fires when inventory_module sends data_changed."""
    refresh_stock(kwargs["item_id"])

# This triggers the receiver:
data_changed.send(inventory_module, item_id="SKU-99")

# This does NOT:
data_changed.send(billing_module, item_id="SKU-99")
```

### Checking for Receivers

Guard expensive operations with `has_receivers_for()`:

```python
if data_changed.has_receivers_for(inventory_module):
    payload = build_expensive_payload()
    data_changed.send(inventory_module, payload=payload)
```

---

## Context Managers

### `signal.connected_to(receiver, sender)`

Temporarily connects a receiver for the duration of a `with` block. Disconnects on exit. Useful for testing:

```python
from bedrock.signal import Signal

user_created = Signal()
received = []

def capture(sender, **kwargs):
    received.append(kwargs)

with user_created.connected_to(capture):
    user_created.send(app, username="alice")

assert len(received) == 1

# Receiver is disconnected after the block
user_created.send(app, username="bob")
assert len(received) == 1  # Still 1
```

### `signal.muted()`

Temporarily suppresses all signal dispatch. No receivers are called while muted:

```python
from bedrock.signal import Signal

audit = Signal()

@audit.connect_via(Signal.ANY, weak=False)
def log_audit(sender, **kwargs):
    write_audit_log(kwargs)

# Suppress during bulk import
with audit.muted():
    for record in bulk_data:
        import_record(record)
        audit.send(importer, action="import")  # Silently ignored

# Normal dispatch resumes here
audit.send(importer, action="complete")  # This fires
```

---

## Lifecycle Signals

Defined in `bedrock.module.signals`. Emitted by the `ModuleRegistry` during startup and shutdown.

### Available Signals

| Signal | Emitted When | Sender | Extra kwargs |
|--------|-------------|--------|--------------|
| `module_loaded` | After a module's `on_load` hook completes | `AppConfig` instance | `registry`, `module`, `config` |
| `module_ready` | After a module's `ready` hook completes | `AppConfig` instance | `registry`, `module`, `config` |
| `module_shutdown` | During graceful shutdown (reverse order) | `AppConfig` instance | `registry`, `module`, `config` |
| `registry_ready` | After all modules are marked ready | `ModuleRegistry` | `registry`, `config` |
| `registry_shutdown` | When the registry begins shutdown | `ModuleRegistry` | `registry`, `config` |

### Import

```python
from bedrock.module.signals import (
    module_loaded,
    module_ready,
    module_shutdown,
    registry_ready,
    registry_shutdown,
)
```

### Usage

```python
from bedrock.signal import Signal
from bedrock.module.signals import module_ready, registry_ready

@module_ready.connect_via(Signal.ANY, weak=False)
def on_module_ready(sender, **kwargs):
    module = kwargs["module"]
    print(f"Module ready: {module.name}")

@registry_ready.connect_via(Signal.ANY, weak=False)
def on_all_ready(sender, **kwargs):
    print("All modules initialized, safe to accept traffic")
```

### Lifecycle Order

During `registry.populate(modules)`:

1. Each module installed in dependency order, `module_loaded` per module
2. Each module's `ready` hook called, `module_ready` per module
3. Registry marked ready, `registry_ready` once

During `registry.shutdown()`:

4. Each module's `on_shutdown` hook called in **reverse** order, `module_shutdown` per module
5. Registry signals completion, `registry_shutdown` once

---

## Weak References

By default, receivers are stored as weak references (`weak=True`).

### Behavior by Receiver Type

| Receiver Type | Weak Behavior | Notes |
|---------------|---------------|-------|
| Module-level functions | Works fine | Persist for the lifetime of the module |
| Closures / lambdas | **Garbage collected immediately** | Must use `weak=False` |
| Bound methods | WeakMethod wrapper | Auto-disconnected when owning instance is garbage collected |

### When to Use `weak=False`

```python
# WRONG: closure is immediately garbage collected
def setup():
    def handler(sender, **kwargs):
        print("received")
    signal.connect(handler)  # handler has no strong reference!

# CORRECT: disable weak refs for closures
def setup():
    def handler(sender, **kwargs):
        print("received")
    signal.connect(handler, weak=False)
```

The `connect_via` decorator defaults to `weak=False`, making it safe for typical decorator usage.

### Checking Connected State

Use the `receivers` attribute for a quick boolean check:

```python
if order_placed.receivers:
    print("At least one receiver is connected")
```

---

## Critical Pitfall: Async Receivers with Sync Send

Calling `signal.send()` when an async receiver is connected raises `RuntimeError`:

```python
from bedrock.signal import Signal

order_placed = Signal()

async def async_handler(sender, **kwargs):
    await notify_external_service(kwargs["order_id"])

order_placed.connect(async_handler, weak=False)

# THIS RAISES RuntimeError!
order_placed.send(app, order_id="ORD-001")
# RuntimeError: Cannot send to an async receiver with send().
# Use await signal.asend(...) or provide _async_wrapper.

# CORRECT: use asend() in async context
await order_placed.asend(app, order_id="ORD-001")
```

**Rule**: In mixed sync/async codebases, prefer `asend()` as the default dispatch method. It handles both sync and async receivers seamlessly. Sync receivers are automatically wrapped via `asyncio.to_thread`.

---

## Signals vs Hooks

Bedrock offers two distinct mechanisms for cross-module communication:

| Aspect | Signals (`bedrock.signal`) | Hooks (`bedrock.hooks`) |
|--------|---------------------------|------------------------|
| **Pattern** | Notification (fire-and-forget) | Call/response (returns values) |
| **Return values** | Ignored by sender | Collected and returned to caller |
| **Ordering** | Unspecified (set-based) | Priority-sorted (lower runs first) |
| **Short-circuit** | No | `firstresult=True` stops after first non-None result |
| **Registration** | `signal.connect(receiver)` | `@hookimpl` decorator or `ns.impl()` |
| **Dispatch** | `signal.send()` / `signal.asend()` | `hooks.call(fqn)` / `hooks.acall(fqn)` |
| **Best for** | Lifecycle events, loose coupling | Extension points, middleware chains |

**Use signals when** you want to notify listeners about something that happened, and you don't care about return values. Example: "a user was created, update your cache."

**Use hooks when** you want to define an extension point where implementations contribute behavior or return values. Example: "authenticate this request, first valid result wins."

```python
# SIGNAL: notification only
from bedrock.signal import Signal
user_created = Signal("user_created")

@user_created.connect
def on_user_created(sender, **kwargs):
    send_welcome_email(kwargs["user"])  # Fire-and-forget

user_created.send(sender, user=new_user)


# HOOK: call/response with return values
from bedrock.hooks import HookNamespace
auth = HookNamespace("auth")

@auth.spec(firstresult=True)
def authenticate(request):
    """Return user if authenticated, None otherwise."""

@auth.impl(priority=10)
def check_token(request):
    if valid_token(request.token):
        return get_user_from_token(request.token)
    return None  # Let next impl try

results = auth.call("authenticate", request=req)
user = results[0] if results else None
```

---

## Anti-Patterns

**Don't mix `send()` and async receivers.** Raises `RuntimeError`. Use `asend()` in codebases with async receivers, or guard with `has_receivers_for()` plus type checks.

**Don't rely on receiver execution order.** The default `set_class` is Python's unordered `set`. If ordered dispatch is needed, provide an ordered set implementation via `Signal.set_class`.

**Don't connect receivers inside hot loops.** Each `connect()` call updates internal bookkeeping dicts. Connect once at module load time, not per-request.

**Don't forget `weak=False` for closures.** A closure or lambda connected without `weak=False` gets garbage collected before any signal is sent.

**Don't use signals for synchronous control flow.** Signals are notification (fire-and-forget). If you need a return value to drive logic, call the function directly.

**Don't send lifecycle signals manually.** The `ModuleRegistry` owns lifecycle signal emission. Sending `module_ready` or `registry_ready` yourself breaks invariants.
