"""Unit tests for lazy loading and dynamic import utilities."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from bedrock.utils.lazyload import (
    _split_ref,
    cached_import,
    import_string,
    load_callable,
    load_optional_callable,
    load_single_subclass,
    load_string,
    module_dir,
    module_has_submodule,
)


class TestSplitRef:
    """Tests for the internal _split_ref helper."""

    def test_colon_separator(self) -> None:
        module, attr = _split_ref("package.module:ClassName")
        assert module == "package.module"
        assert attr == "ClassName"

    def test_dot_separator(self) -> None:
        module, attr = _split_ref("package.module.ClassName")
        assert module == "package.module"
        assert attr == "ClassName"

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _split_ref("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _split_ref("   ")

    def test_missing_separator_raises(self) -> None:
        with pytest.raises(ValueError, match="must use"):
            _split_ref("ClassName")

    def test_empty_module_raises(self) -> None:
        with pytest.raises(ValueError, match="both a module path"):
            _split_ref(":ClassName")

    def test_empty_attr_raises(self) -> None:
        with pytest.raises(ValueError, match="both a module path"):
            _split_ref("package.module:")


class TestCachedImport:
    """Tests for cached_import reusing or loading modules."""

    def test_imports_attribute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_cached_import_mod")
        mod.MyClass = type("MyClass", (), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_cached_import_mod", mod)

        result = cached_import("bedrock_test_cached_import_mod", "MyClass")
        assert result is mod.MyClass

    def test_reuses_cached_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_cached_reuse_mod")
        mod.Counter = 0  # type: ignore[attr-defined]
        spec = types.SimpleNamespace(_initializing=False)
        mod.__spec__ = spec  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_cached_reuse_mod", mod)

        result = cached_import("bedrock_test_cached_reuse_mod", "Counter")
        assert result == 0

    def test_missing_attribute_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_cached_attr_mod")
        monkeypatch.setitem(sys.modules, "bedrock_test_cached_attr_mod", mod)

        with pytest.raises(AttributeError):
            cached_import("bedrock_test_cached_attr_mod", "MissingAttr")


class TestImportString:
    """Tests for import_string dotted-path importer."""

    def test_imports_dotted_class(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_import_string_mod")
        mod.MyClass = type("MyClass", (), {})  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_import_string_mod", mod)

        result = import_string("bedrock_test_import_string_mod.MyClass")
        assert result is mod.MyClass

    def test_no_dot_raises_import_error(self) -> None:
        with pytest.raises(ImportError, match="doesn't look like a module path"):
            import_string("nodots")

    def test_missing_attribute_raises_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_import_string_missing")
        monkeypatch.setitem(sys.modules, "bedrock_test_import_string_missing", mod)

        with pytest.raises(ImportError, match="does not define"):
            import_string("bedrock_test_import_string_missing.MissingAttr")


class TestLoadString:
    """Tests for load_string colon-separated importer."""

    def test_imports_colon_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_load_string_mod")
        mod.MyFunc = lambda: "ok"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_load_string_mod", mod)

        result = load_string("bedrock_test_load_string_mod:MyFunc")
        assert result is mod.MyFunc

    def test_no_colon_raises_import_error(self) -> None:
        with pytest.raises(ImportError, match="doesn't look like a module path"):
            load_string("nocolon")

    def test_missing_attribute_raises_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_load_string_missing")
        monkeypatch.setitem(sys.modules, "bedrock_test_load_string_missing", mod)

        with pytest.raises(ImportError, match="does not define"):
            load_string("bedrock_test_load_string_missing:MissingAttr")


class TestLoadCallable:
    """Tests for load_callable resolving and validating callables."""

    def test_loads_callable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_load_callable_mod")
        mod.my_func = lambda: "ok"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_load_callable_mod", mod)

        result = load_callable("bedrock_test_load_callable_mod:my_func")
        assert result is mod.my_func
        assert result() == "ok"

    def test_non_callable_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_load_callable_non")
        mod.not_callable = 42  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_load_callable_non", mod)

        with pytest.raises(ValueError, match="is not callable"):
            load_callable("bedrock_test_load_callable_non:not_callable")


class TestLoadOptionalCallable:
    """Tests for load_optional_callable with graceful fallback."""

    def test_loads_existing_callable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_load_opt_mod")
        mod.my_func = lambda: "ok"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_load_opt_mod", mod)

        result = load_optional_callable("bedrock_test_load_opt_mod:my_func")
        assert result is mod.my_func

    def test_missing_module_returns_none(self) -> None:
        result = load_optional_callable("bedrock_test_missing_module_xyz:foo")
        assert result is None

    def test_missing_attribute_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_load_opt_missing")
        monkeypatch.setitem(sys.modules, "bedrock_test_load_opt_missing", mod)

        result = load_optional_callable("bedrock_test_load_opt_missing:missing")
        assert result is None

    def test_non_callable_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_load_opt_non")
        mod.not_callable = 42  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_load_opt_non", mod)

        with pytest.raises(ValueError, match="is not callable"):
            load_optional_callable("bedrock_test_load_opt_non:not_callable")


class TestModuleHasSubmodule:
    """Tests for module_has_submodule package introspection."""

    def test_detects_existing_submodule(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pkg = types.ModuleType("bedrock_test_pkg")
        pkg.__name__ = "bedrock_test_pkg"  # type: ignore[attr-defined]
        pkg.__path__ = ["/fake/path"]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_pkg", pkg)

        # Simulate that find_spec returns a spec for the submodule
        fake_spec = types.SimpleNamespace()
        monkeypatch.setattr(
            "bedrock.utils.lazyload.importlib_find",
            lambda name, path: fake_spec if name == "bedrock_test_pkg.sub" else None,
        )

        assert module_has_submodule(pkg, "sub") is True

    def test_non_package_returns_false(self) -> None:
        mod = types.ModuleType("bedrock_test_mod")
        assert module_has_submodule(mod, "sub") is False

    def test_missing_submodule_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pkg = types.ModuleType("bedrock_test_pkg2")
        pkg.__name__ = "bedrock_test_pkg2"  # type: ignore[attr-defined]
        pkg.__path__ = ["/fake/path"]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_pkg2", pkg)

        monkeypatch.setattr(
            "bedrock.utils.lazyload.importlib_find",
            lambda name, path: None,
        )

        assert module_has_submodule(pkg, "missing") is False

    def test_invalid_dotted_path_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pkg = types.ModuleType("bedrock_test_pkg3")
        pkg.__name__ = "bedrock_test_pkg3"  # type: ignore[attr-defined]
        pkg.__path__ = ["/fake/path"]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_pkg3", pkg)

        def raise_module_not_found(name: str, path: Any) -> None:
            raise ModuleNotFoundError(name)

        monkeypatch.setattr(
            "bedrock.utils.lazyload.importlib_find",
            raise_module_not_found,
        )

        assert module_has_submodule(pkg, "invalid.dotted") is False


class TestModuleDir:
    """Tests for module_dir filesystem resolution."""

    def test_returns_path_from_single_path(self) -> None:
        mod = types.ModuleType("bedrock_test_dir_mod")
        mod.__path__ = ["/some/package/path"]  # type: ignore[attr-defined]

        assert module_dir(mod) == "/some/package/path"

    def test_returns_dir_from_file(self) -> None:
        mod = types.ModuleType("bedrock_test_dir_file_mod")
        mod.__file__ = "/some/module/file.py"  # type: ignore[attr-defined]

        assert module_dir(mod) == "/some/module"

    def test_raises_when_no_path_or_file(self) -> None:
        mod = types.ModuleType("bedrock_test_dir_none")

        with pytest.raises(ValueError, match="Cannot determine directory"):
            module_dir(mod)

    def test_empty_path_list_with_file_fallback(self) -> None:
        mod = types.ModuleType("bedrock_test_dir_empty")
        mod.__path__ = []  # type: ignore[attr-defined]
        mod.__file__ = "/fallback/module.py"  # type: ignore[attr-defined]

        assert module_dir(mod) == "/fallback"


class TestLoadSingleSubclass:
    """Tests for load_single_subclass dynamic subclass discovery."""

    def test_finds_first_concrete_subclass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_subclass_mod")

        class Base:
            pass

        class FirstConcrete(Base):
            pass

        class SecondConcrete(Base):
            pass

        mod.Base = Base  # type: ignore[attr-defined]
        mod.FirstConcrete = FirstConcrete  # type: ignore[attr-defined]
        mod.SecondConcrete = SecondConcrete  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_subclass_mod", mod)

        result = load_single_subclass("bedrock_test_subclass_mod", Base)
        assert result is FirstConcrete

    def test_skips_abstract_classes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import abc

        mod = types.ModuleType("bedrock_test_abstract_mod")

        class Base(abc.ABC):
            @abc.abstractmethod
            def method(self) -> None:
                pass

        class Concrete(Base):
            def method(self) -> None:
                pass

        mod.Base = Base  # type: ignore[attr-defined]
        mod.Concrete = Concrete  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_abstract_mod", mod)

        result = load_single_subclass("bedrock_test_abstract_mod", Base)
        assert result is Concrete

    def test_skips_base_class_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_base_only_mod")

        class Base:
            pass

        mod.Base = Base  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_base_only_mod", mod)

        result = load_single_subclass("bedrock_test_base_only_mod", Base)
        assert result is None

    def test_tuple_of_bases(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("bedrock_test_tuple_mod")

        class A:
            pass

        class B:
            pass

        class C(A):
            pass

        mod.A = A  # type: ignore[attr-defined]
        mod.B = B  # type: ignore[attr-defined]
        mod.C = C  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "bedrock_test_tuple_mod", mod)

        result = load_single_subclass("bedrock_test_tuple_mod", (A, B))
        assert result is C

    def test_import_error_raises(self) -> None:
        with pytest.raises(ImportError):
            load_single_subclass("bedrock_test_nonexistent_module_12345", object)
