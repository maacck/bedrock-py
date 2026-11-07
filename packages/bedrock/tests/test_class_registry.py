"""Behavioral tests for typed, lazy class registration."""

import sys
from abc import ABC, abstractmethod
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
from threading import Barrier, Event
from types import ModuleType
from typing import Protocol

import pytest
from bedrock.common.registry import (
    ClassLoadError,
    ClassNotRegisteredError,
    ClassRegistry,
    DuplicateRegistrationError,
    InvalidClassPathError,
    InvalidRegisteredClassError,
)


@pytest.fixture
def plugin_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Provide real importable extensions, isolated from other tests."""
    package = tmp_path / "registry_test_extensions"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield package
    for name in tuple(sys.modules):
        if name == package.name or name.startswith(f"{package.name}."):
            del sys.modules[name]


def test_direct_registration_keeps_local_class_without_constructing() -> None:
    """Local classes remain usable without import paths or instance creation."""

    class Backend(dict):
        def __init__(self) -> None:
            raise AssertionError("The registry must not construct instances.")

    registry = ClassRegistry[dict](base_class=dict)
    registry.register("local", Backend)

    assert registry.get("local") is Backend


def test_lazy_lookup_imports_only_selected_extension_and_caches_class(plugin_package: Path) -> None:
    """Listing must not load SDKs, and a resolved class remains stable."""
    (plugin_package / "selected.py").write_text(
        "class Container:\n    class Backend(dict):\n        pass\n", encoding="utf-8"
    )
    (plugin_package / "unused.py").write_text("raise RuntimeError('unused SDK')\n", encoding="utf-8")
    registry = ClassRegistry[dict](base_class=dict)
    registry.register_lazy("selected", "registry_test_extensions.selected:Container.Backend")
    registry.register_lazy("unused", "registry_test_extensions.unused:Backend")

    assert set(registry.keys()) == {"selected", "unused"}
    assert registry.has("selected")
    assert "registry_test_extensions" not in sys.modules

    resolved = registry.get("selected")
    assert resolved(answer=42) == {"answer": 42}
    module = import_module("registry_test_extensions.selected")
    del module.Container.Backend
    assert registry.get("selected") is resolved
    assert "registry_test_extensions.unused" not in sys.modules


def test_initial_paths_are_lazy_and_independent_of_callers_mapping(plugin_package: Path) -> None:
    """A caller cannot bypass validation by mutating initial declarations."""
    (plugin_package / "backend.py").write_text("class Backend(dict):\n    pass\n", encoding="utf-8")
    paths = {"backend": "registry_test_extensions.backend:Backend"}
    registry = ClassRegistry[dict](base_class=dict, lazy_classes=paths)
    paths["backend"] = "registry_test_extensions.backend:Missing"
    assert "registry_test_extensions.backend" not in sys.modules
    assert registry.get("backend")(value=3) == {"value": 3}


class AbstractBackend(dict, ABC):
    """Abstract implementation that cannot be instantiated."""

    @abstractmethod
    def execute(self) -> None:
        """Require concrete implementations to define behavior."""


@pytest.mark.parametrize("candidate", [dict, list, {}, 42, AbstractBackend])
def test_direct_registration_rejects_invalid_classes(candidate: object) -> None:
    """Bad registrations must not occupy a name or reach consumers."""
    registry = ClassRegistry[dict](base_class=dict)
    with pytest.raises(InvalidRegisteredClassError):
        registry.register("invalid", candidate)  # type: ignore[arg-type]
    assert not registry.has("invalid")


@pytest.mark.parametrize(
    "source",
    [
        "Backend = 42\n",
        "class Backend(list):\n    pass\n",
        "Backend = dict\n",
        "from abc import ABC, abstractmethod\nclass Backend(dict, ABC):\n    @abstractmethod\n    def run(self): pass\n",
    ],
)
def test_lazy_registration_validates_after_import(plugin_package: Path, source: str) -> None:
    """String paths are not trusted merely because import succeeded."""
    (plugin_package / "invalid.py").write_text(source, encoding="utf-8")
    registry = ClassRegistry[dict](base_class=dict)
    registry.register_lazy("invalid", "registry_test_extensions.invalid:Backend")
    assert "registry_test_extensions.invalid" not in sys.modules
    with pytest.raises(InvalidRegisteredClassError) as error:
        registry.get("invalid")
    assert "invalid" in error.value.detail
    assert "registry_test_extensions.invalid:Backend" in error.value.detail


def test_nominal_contract_supports_explicit_protocol_children() -> None:
    """Protocol bases need not be runtime_checkable for explicit inheritance."""

    class Backend(Protocol):
        def run(self) -> int: ...

    class Implementation(Backend):
        def run(self) -> int:
            return 7

    class StructuralOnly:
        def run(self) -> int:
            return 8

    class ChildProtocol(Backend, Protocol):
        pass

    registry = ClassRegistry[Backend](base_class=Backend)
    registry.register("explicit", Implementation)
    assert registry.get("explicit")().run() == 7
    for invalid in (StructuralOnly, ChildProtocol):
        with pytest.raises(InvalidRegisteredClassError):
            registry.register("invalid", invalid)


def test_virtual_subclass_is_not_an_explicit_child() -> None:
    """ABC virtual registration must not bypass the inheritance contract."""

    class Backend(ABC):
        @abstractmethod
        def execute(self) -> None:
            """Require behavior even for an ABC with virtual children."""

    class Virtual:
        pass

    Backend.register(Virtual)
    registry = ClassRegistry[Backend](base_class=Backend)
    with pytest.raises(InvalidRegisteredClassError):
        registry.register("virtual", Virtual)  # type: ignore[arg-type]


@pytest.mark.parametrize("lazy_first", [False, True])
@pytest.mark.parametrize("lazy_second", [False, True])
def test_duplicates_preserve_original_entry(lazy_first: bool, lazy_second: bool, plugin_package: Path) -> None:
    """Neither registration route may overwrite a previously registered name."""
    (plugin_package / "backend.py").write_text("class Backend(dict):\n    pass\n", encoding="utf-8")

    class Original(dict):
        pass

    registry = ClassRegistry[dict](base_class=dict)
    if lazy_first:
        registry.register_lazy("backend", "registry_test_extensions.backend:Backend")
    else:
        registry.register("backend", Original)
    with pytest.raises(DuplicateRegistrationError):
        if lazy_second:
            registry.register_lazy("backend", "registry_test_extensions.missing:Backend")
        else:
            registry.register("backend", Original)
    resolved = registry.get("backend")
    assert resolved.__module__ == ("registry_test_extensions.backend" if lazy_first else __name__)
    assert "registry_test_extensions.missing" not in sys.modules


@pytest.mark.parametrize(
    "path", ["", "module.Class", ":Class", "module:", ".module:Class", "module:Outer..Inner", "a:b:c"]
)
def test_malformed_paths_fail_before_registration(path: str) -> None:
    """Malformed paths should fail at registration, not during a request."""
    registry = ClassRegistry[dict](base_class=dict)
    with pytest.raises(InvalidClassPathError):
        registry.register_lazy("broken", path)
    assert not registry.has("broken")


def test_unknown_name_has_distinct_error() -> None:
    """An unknown name must not be confused with a failed import."""
    registry = ClassRegistry[dict](base_class=dict)
    with pytest.raises(ClassNotRegisteredError):
        registry.get("unknown")


def test_failed_import_preserves_cause_and_can_be_requested_again(plugin_package: Path) -> None:
    """A failed import must not poison later explicit lookups or other entries."""
    (plugin_package / "backend.py").write_text("from .dependency import Backend\n", encoding="utf-8")
    registry = ClassRegistry[dict](base_class=dict)
    registry.register_lazy("backend", "registry_test_extensions.backend:Backend")
    with pytest.raises(ClassLoadError) as error:
        registry.get("backend")
    assert isinstance(error.value.__cause__, ModuleNotFoundError)
    assert error.value.__cause__.name == "registry_test_extensions.dependency"
    assert "backend" in error.value.detail

    dependency = ModuleType("registry_test_extensions.dependency")
    dependency.Backend = type("Backend", (dict,), {})
    sys.modules[dependency.__name__] = dependency
    assert registry.get("backend")(restored=True) == {"restored": True}


@pytest.mark.parametrize(
    "source,cause", [("value = 1\n", AttributeError), ("raise ValueError('extension bug')\n", ValueError)]
)
def test_loading_errors_keep_original_exception(plugin_package: Path, source: str, cause: type[Exception]) -> None:
    """Missing attributes and extension bugs remain distinguishable by cause."""
    (plugin_package / "broken.py").write_text(source, encoding="utf-8")
    registry = ClassRegistry[dict](base_class=dict)
    registry.register_lazy("broken", "registry_test_extensions.broken:Backend")
    with pytest.raises(ClassLoadError) as error:
        registry.get("broken")
    assert isinstance(error.value.__cause__, cause)


def test_concurrent_registration_has_one_winner() -> None:
    """Concurrent registration cannot silently overwrite the winning class."""
    registry = ClassRegistry[dict](base_class=dict)
    barrier = Barrier(2)

    def register(index: int) -> type[dict] | None:
        candidate = type(f"Backend{index}", (dict,), {})
        barrier.wait(timeout=5)
        try:
            registry.register("backend", candidate)
        except DuplicateRegistrationError:
            return None
        return candidate

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, (0, 1)))
    (winner,) = [candidate for candidate in results if candidate is not None]
    assert registry.get("backend") is winner


def test_slow_import_does_not_block_registry_operations(plugin_package: Path) -> None:
    """Never hold registry locks while executing extension module code."""
    signals = ModuleType("registry_test_extensions.signals")
    signals.started = Event()
    signals.release = Event()
    sys.modules[signals.__name__] = signals
    (plugin_package / "slow.py").write_text(
        "from .signals import started, release\nstarted.set()\n"
        "if not release.wait(10):\n    raise RuntimeError('import release timed out')\n"
        "class Backend(dict):\n    pass\n",
        encoding="utf-8",
    )
    registry = ClassRegistry[dict](base_class=dict)
    registry.register_lazy("slow", "registry_test_extensions.slow:Backend")

    class Immediate(dict):
        pass

    def register_other() -> type[dict]:
        registry.register("immediate", Immediate)
        return registry.get("immediate")

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(registry.get, "slow")
        try:
            assert signals.started.wait(5)
            second = pool.submit(registry.get, "slow")
            assert pool.submit(register_other).result(timeout=5) is Immediate
        finally:
            signals.release.set()
        assert first.result(timeout=5) is second.result(timeout=5)


def test_registry_instances_do_not_share_names() -> None:
    """Independent users of the same base class cannot pollute each other."""

    class Backend(dict):
        pass

    first = ClassRegistry[dict](base_class=dict)
    second = ClassRegistry[dict](base_class=dict)
    first.register("backend", Backend)
    names = first.keys()
    names.clear()
    assert first.get("backend") is Backend
    assert not second.has("backend")
