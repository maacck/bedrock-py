"""Unit tests for function introspection helpers."""

from __future__ import annotations

from bedrock.utils.inspect_func import (
    func_accepts_kwargs,
    func_accepts_var_args,
    func_supports_parameter,
    get_func_args,
    get_func_full_args,
    method_has_no_args,
)


class TestGetFuncArgs:
    """Tests for get_func_args positional-or-keyword extraction."""

    def test_empty_function(self) -> None:
        def empty() -> None:
            pass

        assert get_func_args(empty) == []

    def test_simple_args(self) -> None:
        def simple(a: int, b: str) -> None:
            pass

        assert get_func_args(simple) == ["a", "b"]

    def test_ignores_varargs_and_kwargs(self) -> None:
        def mixed(a: int, *args: object, **kwargs: object) -> None:
            pass

        assert get_func_args(mixed) == ["a"]

    def test_ignores_keyword_only(self) -> None:
        def kw_only(*, a: int) -> None:
            pass

        assert get_func_args(kw_only) == []


class TestGetFuncFullArgs:
    """Tests for get_func_full_args with defaults and special forms."""

    def test_no_args(self) -> None:
        def empty() -> None:
            pass

        assert get_func_full_args(empty) == []

    def test_simple_no_defaults(self) -> None:
        def simple(a: int, b: str) -> None:
            pass

        assert get_func_full_args(simple) == [("a",), ("b",)]

    def test_with_defaults(self) -> None:
        def defaults(a: int, b: str = "hello", c: int = 42) -> None:
            pass

        assert get_func_full_args(defaults) == [("a",), ("b", "hello"), ("c", 42)]

    def test_includes_varargs(self) -> None:
        def with_args(a: int, *rest: object) -> None:
            pass

        assert get_func_full_args(with_args) == [("a",), ("*rest",)]

    def test_includes_kwargs(self) -> None:
        def with_kwargs(a: int, **opts: object) -> None:
            pass

        assert get_func_full_args(with_kwargs) == [("a",), ("**opts",)]

    def test_ignores_self(self) -> None:
        class Foo:
            def method(self, a: int) -> None:
                pass

        assert get_func_full_args(Foo().method) == [("a",)]


class TestFuncAcceptsKwargs:
    """Tests for func_accepts_kwargs detection."""

    def test_no_kwargs(self) -> None:
        def plain(a: int) -> None:
            pass

        assert func_accepts_kwargs(plain) is False

    def test_has_kwargs(self) -> None:
        def with_kwargs(a: int, **kwargs: object) -> None:
            pass

        assert func_accepts_kwargs(with_kwargs) is True

    def test_only_kwargs(self) -> None:
        def only_kwargs(**kwargs: object) -> None:
            pass

        assert func_accepts_kwargs(only_kwargs) is True


class TestFuncAcceptsVarArgs:
    """Tests for func_accepts_var_args detection."""

    def test_no_varargs(self) -> None:
        def plain(a: int) -> None:
            pass

        assert func_accepts_var_args(plain) is False

    def test_has_varargs(self) -> None:
        def with_args(a: int, *args: object) -> None:
            pass

        assert func_accepts_var_args(with_args) is True

    def test_only_varargs(self) -> None:
        def only_args(*args: object) -> None:
            pass

        assert func_accepts_var_args(only_args) is True


class TestMethodHasNoArgs:
    """Tests for method_has_no_args self-only detection."""

    def test_bound_method_no_args(self) -> None:
        class Foo:
            def no_args(self) -> None:
                pass

        assert method_has_no_args(Foo().no_args) is True

    def test_bound_method_with_args(self) -> None:
        class Foo:
            def with_args(self, a: int) -> None:
                pass

        assert method_has_no_args(Foo().with_args) is False

    def test_unbound_function_no_args(self) -> None:
        def no_args() -> None:
            pass

        # unbound functions are expected to have a 'self'-like first arg
        assert method_has_no_args(no_args) is False

    def test_unbound_function_one_arg(self) -> None:
        def one_arg(a: int) -> None:
            pass

        # one positional arg is treated as the self-like arg
        assert method_has_no_args(one_arg) is True

    def test_unbound_function_self_like_arg(self) -> None:
        def self_arg(self: object) -> None:
            pass

        assert method_has_no_args(self_arg) is True


class TestFuncSupportsParameter:
    """Tests for func_supports_parameter named parameter check."""

    def test_supports_existing_param(self) -> None:
        def func(a: int, b: str) -> None:
            pass

        assert func_supports_parameter(func, "a") is True
        assert func_supports_parameter(func, "b") is True

    def test_missing_param(self) -> None:
        def func(a: int) -> None:
            pass

        assert func_supports_parameter(func, "b") is False

    def test_supports_kwargs_param(self) -> None:
        def func(**kwargs: object) -> None:
            pass

        # func_supports_parameter checks actual parameter names, not **kwargs catch-all
        assert func_supports_parameter(func, "kwargs") is True
        assert func_supports_parameter(func, "anything") is False

    def test_method_supports_param(self) -> None:
        class Foo:
            def method(self, a: int) -> None:
                pass

        assert func_supports_parameter(Foo().method, "a") is True
        assert func_supports_parameter(Foo().method, "self") is False
