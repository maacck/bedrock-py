"""Unit tests for proxy object utilities."""

from __future__ import annotations

from typing import Any

import pytest
from bedrock.utils.proxy_obj import Proxy, ProxyCallable


class _DummyTarget:
    """Simple target class for proxy tests."""

    def __init__(self) -> None:
        self.value: int = 42
        self.name: str = "dummy"

    def greet(self, greeting: str) -> str:
        return f"{greeting}, {self.name}!"


class TestProxy:
    """Tests for the lazy-loading Proxy class."""

    def test_lazy_instantiation_on_getattr(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)
        assert proxy._wrapped is None

        _ = proxy.value

        assert proxy._wrapped is not None
        assert isinstance(proxy._wrapped, _DummyTarget)

    def test_attribute_access_delegation(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        assert proxy.value == 42
        assert proxy.name == "dummy"

    def test_attribute_assignment_delegation(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        proxy.value = 100
        proxy.name = "updated"

        assert proxy.value == 100
        assert proxy.name == "updated"

    def test_dir_exposes_wrapped_attributes(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        attrs = dir(proxy)
        assert "value" in attrs
        assert "name" in attrs
        assert "greet" in attrs

    def test_method_call_delegation(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        result = proxy.greet("Hello")
        assert result == "Hello, dummy!"

    def test_factory_attribute_not_delegated(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        # _factory should be read from the proxy itself, not trigger setup
        factory = proxy._factory
        assert factory is _DummyTarget
        assert proxy._wrapped is None

    def test_wrapped_attribute_not_delegated(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        wrapped = proxy._wrapped
        assert wrapped is None

    def test_setup_attribute_not_delegated(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        setup_method = proxy._setup
        assert callable(setup_method)
        assert proxy._wrapped is None

    def test_setattr_on_proxy_internals(self) -> None:
        proxy: Proxy[_DummyTarget] = Proxy(_DummyTarget)

        # Setting _wrapped directly should not trigger _setup
        proxy._wrapped = None
        assert proxy._wrapped is None

    def test_callable_factory(self) -> None:
        def factory() -> dict[str, Any]:
            return {"key": "value"}

        proxy: Proxy[dict[str, Any]] = Proxy(factory)  # type: ignore[arg-type]
        assert proxy.get("key") == "value"


class TestProxyCallable:
    """Tests for ProxyCallable dynamic callable loader."""

    def test_delegates_call_to_loaded_callable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = __import__("types").ModuleType("bedrock_test_proxy_callable_mod")
        mod.my_func = lambda x, y: x + y  # type: ignore[attr-defined]
        monkeypatch.setitem(__import__("sys").modules, "bedrock_test_proxy_callable_mod", mod)

        proxy_callable = ProxyCallable("bedrock_test_proxy_callable_mod:my_func")
        result = proxy_callable(2, 3)
        assert result == 5

    def test_passes_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = __import__("types").ModuleType("bedrock_test_proxy_callable_kw_mod")
        mod.my_func = lambda **kwargs: kwargs  # type: ignore[attr-defined]
        monkeypatch.setitem(__import__("sys").modules, "bedrock_test_proxy_callable_kw_mod", mod)

        proxy_callable = ProxyCallable("bedrock_test_proxy_callable_kw_mod:my_func")
        result = proxy_callable(a=1, b=2)
        assert result == {"a": 1, "b": 2}

    def test_raises_for_non_callable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = __import__("types").ModuleType("bedrock_test_proxy_callable_non_mod")
        mod.not_callable = 42  # type: ignore[attr-defined]
        monkeypatch.setitem(__import__("sys").modules, "bedrock_test_proxy_callable_non_mod", mod)

        proxy_callable = ProxyCallable("bedrock_test_proxy_callable_non_mod:not_callable")
        with pytest.raises(ValueError, match="is not callable"):
            proxy_callable()
