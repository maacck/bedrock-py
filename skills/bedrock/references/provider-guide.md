# Provider Guide

Use `ProviderCatalog[P]` from `bedrock.provider` when callers select a provider by name and need an **instance** validated against a protocol, or want the class or factory to construct themselves. Use `ClassRegistry` (`references/class-registry-guide.md`) when callers need the class itself and every implementation must inherit a base.

## Registration and creation

```python
from typing import Protocol, runtime_checkable
from bedrock.provider import ProviderCatalog

@runtime_checkable
class Notifier(Protocol):
    def send(self, to: str, text: str) -> str: ...

class ConsoleNotifier:  # structural: no inheritance required
    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix

    def send(self, to: str, text: str) -> str:
        return f"{self.prefix}{to}: {text}"

notifiers = ProviderCatalog[Notifier](protocol=Notifier)
notifiers.register("console", ConsoleNotifier)              # explicit: object already imported
notifier = notifiers.create("console", prefix="[dev] ")     # validated instance
```

Lazy providers record a path and import it only when selected. Declare built-ins with `ProviderSpec`, add others from the owning or third-party module's `bootstrap.py:on_load`:

```python
# service.py
notifiers = ProviderCatalog[Notifier](
    protocol=Notifier,
    specs=(ProviderSpec("sms", "myapp.notify.providers.sms:SmsNotifier", install_requirement="myapp[sms]"),),
)

# bootstrap.py
def on_load() -> None:
    notifiers.register_lazy("email", "myapp.notify.providers.email:create_email_notifier")
```

Pass application-owned configuration to `create()`; the catalog never reads settings. Use `resolve()` to get the class or factory without calling it, for classmethods, positional arguments, or custom construction. `resolve()` does not validate the result.

| Interface | Behavior |
| --- | --- |
| `ProviderCatalog[P](protocol=P, specs=())` | Catalog bound to a class or `@runtime_checkable` Protocol |
| `register(name, factory)` | Explicit mode: record an imported class or factory |
| `register_lazy(name, path, *, install_requirement=None)` | Lazy mode: record a path without importing it |
| `has(name)` / `names()` | Check / list names (sorted) without importing |
| `resolve(name)` | Return the class or factory; lazy paths import on first use |
| `create(name, **kwargs)` | Call the factory and return an instance checked with `isinstance` |

Re-registering the same object (explicit) or an equal path and requirement (lazy) is a no-op. Anything else under an existing name, including mixing explicit and lazy registration for one name, raises `ProviderConflictError`. Entries cannot be replaced or removed; use a fresh catalog for isolation. Do not register at import time.

## Guarantees and errors

`isinstance` against a Protocol proves members exist, not signatures or async-ness; use static checks and contract tests. Resolved lazy factories are cached, instances never are; failed imports are retried on the next call. Imports and factory calls run outside the lock. The catalog is process-local and does not own created instances.

All errors are exported from `bedrock.provider` and inherit `ProviderError` (`BedrockExc`):

| Error | Meaning |
| --- | --- |
| `InvalidProviderSpecError` | Empty name, malformed path or requirement, non-callable factory; also an `InvalidRegisteredClassError` |
| `ProviderConflictError` | Different provider under the same name; also a `DuplicateRegistrationError` |
| `ProviderNotFoundError` | Unknown name; message lists available names |
| `ProviderLoadError` | Lazy import or lookup failed, or the factory raised `ImportError`; names `install_requirement`; `__cause__` keeps the original |
| `InvalidProviderError` | `create()` result is not an instance of `P` |

Other factory exceptions propagate unchanged. Import paths execute code: accept them only from trusted code or deployment configuration.
