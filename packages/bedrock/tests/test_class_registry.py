"""Unit tests for the ClassRegistry helper."""

from __future__ import annotations

import sys
import textwrap
import types

import pytest
from bedrock.common.registry import ClassRegistry


def _install_module(monkeypatch: pytest.MonkeyPatch, module_name: str, source: str) -> types.ModuleType:
    """Install a temporary module in sys.modules for dynamic import tests."""
    module = types.ModuleType(module_name)
    exec(textwrap.dedent(source), module.__dict__)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


class TestClassRegistry:
    """Tests for class registration and lookup behavior."""

    def test_register_derives_nested_import_path_and_get_resolves_class(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module = _install_module(
            monkeypatch,
            "bedrock_test_class_registry_nested",
            """
            class Container:
                class Example:
                    pass
            """,
        )
        registry = ClassRegistry()

        registry.register("example", module.Container.Example)

        assert registry.instances["example"] == f"{module.__name__}:Container.Example"
        assert registry.get("example") is module.Container.Example

    def test_register_preserves_first_registration_for_duplicate_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module = _install_module(
            monkeypatch,
            "bedrock_test_class_registry_duplicate",
            """
            class First:
                pass

            class Second:
                pass
            """,
        )
        registry = ClassRegistry()

        registry.register("example", module.First)
        registry.register("example", module.Second)

        assert registry.instances["example"] == f"{module.__name__}:First"
        assert registry.get("example") is module.First
