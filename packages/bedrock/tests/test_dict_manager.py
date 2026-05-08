"""Unit tests for the DictManager registry helper."""

from __future__ import annotations

import importlib
import sys
import textwrap
import types

import pytest
from bedrock.common.registry import DictManager


def _install_module(monkeypatch: pytest.MonkeyPatch, module_name: str, source: str) -> types.ModuleType:
    """Install a temporary module in sys.modules for dynamic import tests."""
    module = types.ModuleType(module_name)
    exec(textwrap.dedent(source), module.__dict__)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


class TestDictManager:
    """Tests for dict-backed registry behavior."""

    def test_add_get_remove_and_has(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _install_module(
            monkeypatch,
            "bedrock_test_dict_manager_success",
            """
            class Example:
                pass
            """,
        )
        manager = DictManager()

        manager.add("example", f"{module.__name__}:Example")

        assert manager.has("example") is True
        assert manager.get("example") is module.Example

        manager.remove("example")

        assert manager.has("example") is False

    def test_add_does_not_overwrite_existing_registration(self) -> None:
        manager = DictManager({"example": "first.module:Example"})

        manager.add("example", "second.module:Replacement")

        assert manager.instances["example"] == "first.module:Example"

    def test_update_all_and_keys_reflect_current_state(self) -> None:
        manager = DictManager({"alpha": "pkg.alpha:One"})

        manager.update({"alpha": "pkg.alpha:Updated", "beta": "pkg.beta:Two"})

        assert dict(manager.all()) == {
            "alpha": "pkg.alpha:Updated",
            "beta": "pkg.beta:Two",
        }
        assert manager.keys() == ["alpha", "beta"]

    def test_get_returns_none_when_dynamic_import_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = DictManager({"broken": "broken.module:Example"})

        def _raise_import_error(module_name: str) -> types.ModuleType:
            raise ImportError(f"cannot import {module_name}")

        monkeypatch.setattr(importlib, "import_module", _raise_import_error)

        assert manager.get("broken") is None

    def test_get_missing_key_preserves_key_error_behavior(self) -> None:
        manager = DictManager()

        with pytest.raises(KeyError):
            manager.get("missing")
