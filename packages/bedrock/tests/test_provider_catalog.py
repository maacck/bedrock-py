"""Behavior tests for the bedrock.provider catalog."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
from bedrock.common.registry import DuplicateRegistrationError, InvalidRegisteredClassError
from bedrock.provider import (
    InvalidProviderError,
    InvalidProviderSpecError,
    ProviderCatalog,
    ProviderConflictError,
    ProviderError,
    ProviderLoadError,
    ProviderNotFoundError,
    ProviderSpec,
)


@runtime_checkable
class Greeter(Protocol):
    def greet(self) -> str: ...


class NotRuntimeGreeter(Protocol):
    def greet(self) -> str: ...


class Hello:
    """Implements Greeter structurally, without inheriting it."""

    def __init__(self, name: str = "world") -> None:
        self.name = name

    def greet(self) -> str:
        return f"hello {self.name}"


class Silent:
    """Has no greet() method."""


GREETER_SOURCE = """
created = 0


class Greeter:
    def __init__(self, name="world"):
        global created
        created += 1
        self.name = name

    def greet(self):
        return "hi " + self.name
"""


def write_module(root: Path, name: str, source: str = GREETER_SOURCE) -> None:
    (root / f"{name}.py").write_text(source)


def greeters(*specs: ProviderSpec) -> ProviderCatalog[Greeter]:
    return ProviderCatalog(protocol=Greeter, specs=specs)


class TestLazyRegistration:
    def test_registering_and_listing_import_nothing_until_selected(self, fake_package: Path) -> None:
        for suffix in "abc":
            write_module(fake_package, f"provider_probe_{suffix}")
        catalog = greeters(ProviderSpec("a", "provider_probe_a:Greeter"))
        catalog.register_lazy("b", "provider_probe_b:Greeter")
        catalog.register_lazy("c", "provider_probe_c:Greeter", install_requirement="probe[c]")

        assert catalog.names() == ["a", "b", "c"]
        assert catalog.has("b")
        assert not any(f"provider_probe_{suffix}" in sys.modules for suffix in "abc")

        assert catalog.create("b", name="bob").greet() == "hi bob"
        assert "provider_probe_b" in sys.modules
        assert "provider_probe_a" not in sys.modules
        assert "provider_probe_c" not in sys.modules

    def test_resolved_factory_is_cached_but_every_create_builds_a_new_instance(self, fake_package: Path) -> None:
        write_module(fake_package, "provider_probe_cached")
        catalog = greeters(ProviderSpec("p", "provider_probe_cached:Greeter"))

        first = catalog.create("p")
        del sys.modules["provider_probe_cached"]
        second = catalog.create("p")

        assert "provider_probe_cached" not in sys.modules
        assert first is not second

    def test_missing_module_names_install_requirement_and_keeps_cause(self) -> None:
        catalog = greeters()
        catalog.register_lazy("x", "provider_no_such_module:Greeter", install_requirement="acme[x]")

        with pytest.raises(ProviderLoadError, match=r"Install 'acme\[x\]'") as excinfo:
            catalog.create("x")

        assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)

    def test_missing_attribute_is_a_load_error(self, fake_package: Path) -> None:
        write_module(fake_package, "provider_probe_noattr", "VALUE = 1\n")
        catalog = greeters(ProviderSpec("x", "provider_probe_noattr:Missing"))

        with pytest.raises(ProviderLoadError) as excinfo:
            catalog.create("x")

        assert isinstance(excinfo.value.__cause__, AttributeError)

    def test_path_to_non_callable_is_a_load_error(self, fake_package: Path) -> None:
        write_module(fake_package, "provider_probe_value", "VALUE = 1\n")
        catalog = greeters(ProviderSpec("x", "provider_probe_value:VALUE"))

        with pytest.raises(ProviderLoadError, match="does not name a callable"):
            catalog.create("x")

    def test_import_error_raised_by_factory_is_wrapped_with_install_requirement(self, fake_package: Path) -> None:
        write_module(fake_package, "provider_probe_sdk", "def create(**kwargs):\n    import provider_absent_sdk\n")
        catalog = greeters(ProviderSpec("x", "provider_probe_sdk:create", "acme-sdk"))

        with pytest.raises(ProviderLoadError, match="Install 'acme-sdk'") as excinfo:
            catalog.create("x")

        assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)


class TestExplicitRegistration:
    def test_locally_defined_class_and_function_factory_can_be_created(self) -> None:
        class Local:
            def greet(self) -> str:
                return "local"

        def make(name: str = "world") -> Hello:
            return Hello(name)

        catalog = greeters()
        catalog.register("local", Local)
        catalog.register("made", make)

        assert catalog.create("local").greet() == "local"
        assert catalog.create("made", name="ann").greet() == "hello ann"

    def test_import_error_from_explicit_factory_names_the_factory_without_install_hint(self) -> None:
        def needs_sdk() -> Hello:
            raise ImportError("no sdk")

        catalog = greeters()
        catalog.register("sdk", needs_sdk)

        with pytest.raises(ProviderLoadError, match="needs_sdk") as excinfo:
            catalog.create("sdk")

        assert "Install" not in excinfo.value.detail
        assert str(excinfo.value.__cause__) == "no sdk"

    def test_other_factory_errors_propagate_unchanged(self) -> None:
        def broken() -> Hello:
            raise ValueError("bad settings")

        catalog = greeters()
        catalog.register("broken", broken)

        with pytest.raises(ValueError, match="bad settings"):
            catalog.create("broken")


class TestDuplicates:
    def test_identical_registrations_are_no_ops(self) -> None:
        catalog = greeters(ProviderSpec("lazy", "m:Greeter", "m[x]"), ProviderSpec("lazy", "m:Greeter", "m[x]"))
        catalog.register_lazy("lazy", "m:Greeter", install_requirement="m[x]")
        catalog.register("hello", Hello)
        catalog.register("hello", Hello)

        assert catalog.names() == ["hello", "lazy"]

    @pytest.mark.parametrize(
        "conflict",
        [
            lambda c: c.register("hello", Silent),
            lambda c: c.register_lazy("lazy", "m:Other"),
            lambda c: c.register_lazy("lazy", "m:Greeter", install_requirement="m[y]"),
            lambda c: c.register_lazy("hello", "tests:Hello"),
            lambda c: c.register("lazy", Hello),
        ],
        ids=["explicit-other", "lazy-other-path", "lazy-other-requirement", "lazy-over-explicit", "explicit-over-lazy"],
    )
    def test_different_target_under_same_name_conflicts(self, conflict) -> None:
        catalog = greeters(ProviderSpec("lazy", "m:Greeter", "m[x]"))
        catalog.register("hello", Hello)

        with pytest.raises(ProviderConflictError) as excinfo:
            conflict(catalog)

        assert isinstance(excinfo.value, DuplicateRegistrationError)
        assert catalog.resolve("hello") is Hello


class TestInvalidSpecs:
    @pytest.mark.parametrize(
        "register",
        [
            lambda c: c.register("", Hello),
            lambda c: c.register("x", 42),
            lambda c: c.register_lazy("x", "no_colon"),
            lambda c: c.register_lazy("x", "1bad:Thing"),
            lambda c: c.register_lazy("x", "m:Thing", install_requirement=" "),
            lambda c: ProviderSpec("", "m:Thing"),
        ],
        ids=["empty-name", "non-callable", "no-colon", "bad-module", "blank-requirement", "spec-empty-name"],
    )
    def test_malformed_registration_is_rejected_and_not_recorded(self, register) -> None:
        catalog = greeters()

        with pytest.raises(InvalidProviderSpecError) as excinfo:
            register(catalog)

        assert isinstance(excinfo.value, InvalidRegisteredClassError)
        assert catalog.names() == []

    def test_protocol_must_support_isinstance(self) -> None:
        with pytest.raises(ProviderError, match="runtime_checkable"):
            ProviderCatalog(protocol=NotRuntimeGreeter)


class TestCreate:
    def test_unknown_name_lists_available_providers(self) -> None:
        catalog = greeters()
        catalog.register("hello", Hello)

        with pytest.raises(ProviderNotFoundError, match="Available: hello"):
            catalog.create("missing")

    def test_result_that_does_not_implement_protocol_is_rejected(self) -> None:
        catalog = greeters()
        catalog.register("silent", Silent)

        with pytest.raises(InvalidProviderError, match="Silent"):
            catalog.create("silent")


class TestResolve:
    def test_explicit_entry_is_returned_as_registered(self) -> None:
        catalog = greeters()
        catalog.register("hello", Hello)

        assert catalog.resolve("hello") is Hello

    def test_lazy_entry_is_imported_but_not_called(self, fake_package: Path) -> None:
        write_module(fake_package, "provider_probe_resolve")
        catalog = greeters(ProviderSpec("p", "provider_probe_resolve:Greeter"))

        factory = catalog.resolve("p")
        module = sys.modules["provider_probe_resolve"]

        assert factory is module.Greeter
        assert module.created == 0
        assert factory(name="app").greet() == "hi app"

    def test_resolve_does_not_validate_against_protocol(self) -> None:
        catalog = greeters()
        catalog.register("silent", Silent)

        assert catalog.resolve("silent") is Silent

    def test_resolve_errors_match_create(self) -> None:
        catalog = greeters()
        catalog.register_lazy("x", "provider_no_such_module:Greeter", install_requirement="acme[x]")

        with pytest.raises(ProviderNotFoundError):
            catalog.resolve("missing")
        with pytest.raises(ProviderLoadError, match=r"Install 'acme\[x\]'"):
            catalog.resolve("x")
