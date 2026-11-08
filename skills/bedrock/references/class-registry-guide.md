# Class Registry Guide

Use `ClassRegistry[T]` when multiple named implementation classes coexist and callers select one by name. It returns classes; use DI for dependency instances and lifetimes. When callers need validated provider instances, factory functions, structural implementations, or install hints for optional dependencies, use `bedrock.provider.ProviderCatalog` instead (`references/provider-guide.md`).

## Registration and lookup

```python
from abc import ABC, abstractmethod
from bedrock.common.registry import ClassRegistry

class Codec(ABC):
    @abstractmethod
    def encode(self, value: str) -> bytes: ...

class Utf8Codec(Codec):
    def encode(self, value: str) -> bytes:
        return value.encode("utf-8")

codecs = ClassRegistry[Codec](base_class=Codec)
codecs.register("utf8", Utf8Codec)
codec_class: type[Codec] = codecs.get("utf8")
encoded = codec_class().encode("hello")
```

For optional implementations, register a trusted path in the provider module's `bootstrap.py` rather than importing its implementation at startup:

```python
# bootstrap.py
from myapp.encoding.service import codecs

def on_load(*, app) -> None:
    codecs.register_lazy("other", "myapp.encoding.providers.other:OtherCodec")
```

Third-party providers should depend on the owning module in `manifest.yaml`. Do not register extensions using import-time decorators or `__init_subclass__`; keep parent `__init__.py` files free of optional provider imports. Paths use absolute `module:qualified.attribute` syntax, for example `vendor.codecs:Container.CustomCodec`. Registration validates syntax; `get()` imports and validates the selected class. `has()` and `keys()` do not import it. Initial inert declarations can instead be passed as `lazy_classes={"other": "myapp.encoding.providers.other:OtherCodec"}`; the mapping is copied.

| Interface | Behavior |
| --- | --- |
| `ClassRegistry[T](base_class=Base, lazy_classes=None)` | Independent registry for a base class |
| `register(name, cls)` | Validate and retain a class without constructing it |
| `register_lazy(name, path)` | Store a path without importing it |
| `get(name)` | Return a validated `type[T]` or raise |
| `has(name)` | Check registration, not importability |
| `keys()` | Snapshot of names in registration order |

Names are case-sensitive. Valid duplicate registrations fail even if the target is unchanged. Entries cannot be replaced or removed; create a fresh registry for isolation. Callers own construction, configuration, reuse, and cleanup; no constructor signature is imposed.

## Type and runtime guarantees

Direct classes are type-checked as `type[T]` and validated immediately. Lazy targets are validated on lookup. Implementations must explicitly inherit the base, directly or indirectly, and be concrete classes; the base itself, abstract classes, Protocol definitions, virtual ABC subclasses, and structural-only matches are rejected. Explicit Protocol subclasses work without `runtime_checkable`. Inheritance does not validate method signatures or behavior; use static checks and conformance tests. Inherited Protocol stubs are not abstract enforcement.

Registration and successful class-cache publication are thread-safe and process-local. Imports happen outside the lock; concurrent lookups can repeat validation. Successful classes, not instances, are cached; failures are not cached and a later lookup tries again. Imports are synchronous and Python's import cache is not cleared. Resolve configured implementations in `ready()` if startup should fail early or avoid first-use blocking in async traffic.

All errors below are exported from `bedrock.common.registry` and inherit `ClassRegistryError` (`BedrockExc`):

| Error | Meaning |
| --- | --- |
| `DuplicateRegistrationError` | Name already registered |
| `ClassNotRegisteredError` | Name unknown |
| `InvalidClassPathError` | Malformed path |
| `ClassLoadError` | Import or attribute lookup failed; `__cause__` retains the original exception |
| `InvalidRegisteredClassError` | Invalid base or implementation class |

Import paths execute code: accept them only from trusted code or deployment configuration, never directly from requests. Requests may select authorized, already-registered names; inheritance validation is not a sandbox.

## Migration

Replace positional path mappings with `ClassRegistry[Base](base_class=Base, lazy_classes=paths)` and register paths using `register_lazy(name, path)`. Replace missing-result checks and `KeyError` handling with the typed errors above. Replace mutable-registry operations with explicit registration and `keys()`; duplicate names now fail. Migrate legacy unvalidated class loaders to this registry. Custom Cache and Storage backends must explicitly inherit `CacheBackend` or `StorageBackend`; their `register_backend()` helpers reject duplicate names, including built-ins.
