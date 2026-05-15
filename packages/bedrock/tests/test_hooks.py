"""Unit tests for the Bedrock hook system."""

import pytest
from bedrock.hooks import HookNamespace, HookRegistry, hookimpl, hookspec
from bedrock.hooks.exc import HookSpecNotFoundError


class TestHookspecMarker:
    """@hookspec decorator sets metadata on functions."""

    def test_bare_hookspec_sets_metadata(self) -> None:
        @hookspec
        def my_hook(x: int) -> int:
            return x

        assert hasattr(my_hook, "_hookspec")
        assert my_hook._hookspec["firstresult"] is False

    def test_hookspec_with_firstresult(self) -> None:
        @hookspec(firstresult=True)
        def my_hook(x: int) -> int:
            return x

        assert my_hook._hookspec["firstresult"] is True

    def test_hookspec_returns_original_function(self) -> None:
        def original(x: int) -> int:
            return x

        decorated = hookspec(original)
        assert decorated is original


class TestHookimplMarker:
    """@hookimpl decorator sets metadata on functions."""

    def test_bare_hookimpl_sets_metadata(self) -> None:
        @hookimpl
        def my_impl(x: int) -> int:
            return x

        assert hasattr(my_impl, "_hookimpl")
        assert my_impl._hookimpl["priority"] == 0

    def test_hookimpl_with_priority(self) -> None:
        @hookimpl(priority=10)
        def my_impl(x: int) -> int:
            return x

        assert my_impl._hookimpl["priority"] == 10

    def test_hookimpl_returns_original_function(self) -> None:
        def original(x: int) -> int:
            return x

        decorated = hookimpl(original)
        assert decorated is original


class TestHookRegistry:
    """Core registry operations: spec registration, impl registration, dispatch."""

    def test_register_spec_and_call(self) -> None:
        reg = HookRegistry()
        reg.register_spec("auth.login", lambda **kw: None, firstresult=False, namespace="auth")

        @hookimpl
        def impl_a(**kwargs: object) -> str:
            return "ok"

        reg.register_impl("auth.login", impl_a, priority=0, module=None)

        results = reg.call("auth.login")
        assert results == ["ok"]

    def test_call_raises_without_spec(self) -> None:
        reg = HookRegistry()
        with pytest.raises(HookSpecNotFoundError):
            reg.call("nonexistent.hook")

    def test_has_spec(self) -> None:
        reg = HookRegistry()
        reg.register_spec("auth.check", lambda **kw: None, firstresult=False, namespace="auth")

        assert reg.has_spec("auth.check") is True
        assert reg.has_spec("auth.missing") is False

    def test_validate_reports_orphaned_impls(self) -> None:
        reg = HookRegistry()
        reg.register_impl("orphan.impl", lambda: None, priority=0, module=None)

        warnings = reg.validate()
        assert any("non-existent spec" in w for w in warnings)

    def test_validate_reports_specs_without_impls(self) -> None:
        reg = HookRegistry()
        reg.register_spec("empty.spec", lambda **kw: None, firstresult=False, namespace="empty")

        warnings = reg.validate()
        assert any("no implementations" in w for w in warnings)

    def test_validate_no_warnings_when_clean(self) -> None:
        reg = HookRegistry()
        reg.register_spec("ok.spec", lambda **kw: None, firstresult=False, namespace="ok")
        reg.register_impl("ok.spec", lambda: "ok", priority=0, module=None)

        warnings = reg.validate()
        assert warnings == []

    def test_namespaces_lists_registered_namespaces(self) -> None:
        reg = HookRegistry()
        reg.register_spec("auth.login", lambda **kw: None, firstresult=False, namespace="auth")
        reg.register_spec("cache.invalidate", lambda **kw: None, firstresult=False, namespace="cache")

        assert reg.namespaces() == ["auth", "cache"]

    def test_reset_all_clears_everything(self) -> None:
        reg = HookRegistry()
        reg.register_spec("auth.login", lambda **kw: None, firstresult=False, namespace="auth")
        reg.register_impl("auth.login", lambda: None, priority=0, module=None)

        reg.reset()

        assert reg.has_spec("auth.login") is False
        assert reg._impls == {}
        assert reg._namespaces == {}

    def test_reset_namespace_only_clears_that_namespace(self) -> None:
        reg = HookRegistry()
        reg.register_spec("auth.login", lambda **kw: None, firstresult=False, namespace="auth")
        reg.register_spec("cache.evict", lambda **kw: None, firstresult=False, namespace="cache")
        reg.register_impl("auth.login", lambda: None, priority=0, module=None)

        reg.reset(namespace="auth")

        assert reg.has_spec("auth.login") is False
        assert reg.has_spec("cache.evict") is True

    def test_impls_sorted_by_priority(self) -> None:
        reg = HookRegistry()
        reg.register_spec("hook.x", lambda **kw: None, firstresult=False, namespace="ns")

        reg.register_impl("hook.x", lambda: "low", priority=10, module=None)
        reg.register_impl("hook.x", lambda: "high", priority=1, module=None)
        reg.register_impl("hook.x", lambda: "mid", priority=5, module=None)

        results = reg.call("hook.x")
        assert results == ["high", "mid", "low"]

    def test_impls_same_priority_use_registration_order(self) -> None:
        reg = HookRegistry()
        reg.register_spec("hook.y", lambda **kw: None, firstresult=False, namespace="ns")

        reg.register_impl("hook.y", lambda: "first", priority=0, module=None)
        reg.register_impl("hook.y", lambda: "second", priority=0, module=None)

        results = reg.call("hook.y")
        assert results == ["first", "second"]

    def test_namespace_factory_returns_same_instance(self) -> None:
        reg = HookRegistry()
        ns1 = reg.namespace("auth")
        ns2 = reg.namespace("auth")
        assert ns1 is ns2


class TestHookNamespace:
    """HookNamespace spec/impl registration and dispatch."""

    def test_spec_and_impl_registration(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def authenticate(self, user: str) -> bool: ...

        class Impl:
            @hookimpl
            def authenticate(self, user: str) -> bool:
                return user == "admin"

        ns.add_specs_from(Spec)
        ns.add_impls_from(Impl())

        assert ns.has_spec("authenticate") is True
        assert reg.has_spec("auth.authenticate") is True

    def test_call_dispatches_to_impls(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def login(self, user: str) -> str: ...

        class Impl:
            @hookimpl
            def login(self, user: str) -> str:
                return f"welcome:{user}"

        ns.add_specs_from(Spec)
        ns.add_impls_from(Impl())

        results = ns.call("login", user="alice")
        assert results == ["welcome:alice"]

    @pytest.mark.asyncio
    async def test_acall_dispatches_async(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def verify(self, token: str) -> bool: ...

        class Impl:
            @hookimpl
            def verify(self, token: str) -> bool:
                return token == "valid"

        ns.add_specs_from(Spec)
        ns.add_impls_from(Impl())

        results = await ns.acall("verify", token="valid")
        assert results == [True]

    @pytest.mark.asyncio
    async def test_acall_with_native_async_impl(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def check(self, x: int) -> int: ...

        class Impl:
            @hookimpl
            async def check(self, x: int) -> int:
                return x * 2

        ns.add_specs_from(Spec)
        ns.add_impls_from(Impl())

        results = await ns.acall("check", x=5)
        assert results == [10]

    def test_call_robust_catches_exceptions(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("events", registry=reg)

        class Spec:
            @hookspec
            def on_event(self, data: str) -> str: ...

        class ImplOk:
            @hookimpl(priority=0)
            def on_event(self, data: str) -> str:
                return "ok"

        class ImplBroken:
            @hookimpl(priority=1)
            def on_event(self, data: str) -> str:
                raise ValueError("boom")

        ns.add_specs_from(Spec)
        ns.add_impls_from(ImplOk())
        ns.add_impls_from(ImplBroken())

        results = ns.call_robust("on_event", data="test")
        assert len(results) == 2
        assert results[0] == (results[0][0], "ok")
        assert isinstance(results[1][1], ValueError)

    def test_call_robust_with_firstresult(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec(firstresult=True)
            def find_user(self, name: str) -> str | None: ...

        class ImplNone:
            @hookimpl(priority=0)
            def find_user(self, name: str) -> str | None:
                return None

        class ImplHit:
            @hookimpl(priority=1)
            def find_user(self, name: str) -> str | None:
                return f"found:{name}"

        ns.add_specs_from(Spec)
        ns.add_impls_from(ImplNone())
        ns.add_impls_from(ImplHit())

        results = ns.call_robust("find_user", name="alice")
        assert len(results) == 1
        assert results[0][1] == "found:alice"

    def test_get_impls_returns_callables(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def auth(self, user: str) -> bool: ...

        class ImplHigh:
            @hookimpl(priority=2)
            def auth(self, user: str) -> bool:
                return True

        class ImplLow:
            @hookimpl(priority=1)
            def auth(self, user: str) -> bool:
                return True

        ns.add_specs_from(Spec)
        ns.add_impls_from(ImplHigh())
        ns.add_impls_from(ImplLow())

        impls = ns.get_impls("auth")
        assert len(impls) == 2

    def test_specs_lists_namespace_specs(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def login(self, user: str) -> bool: ...

            @hookspec
            def logout(self, user: str) -> bool: ...

        ns.add_specs_from(Spec)

        assert ns.specs() == ["login", "logout"]

    def test_reset_clears_only_this_namespace(self) -> None:
        reg = HookRegistry()
        ns_auth = HookNamespace("auth", registry=reg)
        ns_cache = HookNamespace("cache", registry=reg)

        class AuthSpec:
            @hookspec
            def login(self, user: str) -> bool: ...

        class CacheSpec:
            @hookspec
            def evict(self, key: str) -> None: ...

        ns_auth.add_specs_from(AuthSpec)
        ns_cache.add_specs_from(CacheSpec)

        ns_auth.reset()

        assert ns_auth.has_spec("login") is False
        assert ns_cache.has_spec("evict") is True

    def test_add_specs_from_class(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class AuthSpecs:
            @hookspec
            def authenticate(self, user: str) -> bool: ...

            @hookspec(firstresult=True)
            def authorize(self, user: str, resource: str) -> bool: ...

        ns.add_specs_from(AuthSpecs)

        assert ns.has_spec("authenticate") is True
        assert ns.has_spec("authorize") is True

    def test_add_impls_from_class(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def authenticate(self, user: str) -> bool: ...

        class AuthImpls:
            @hookimpl(priority=1)
            def authenticate(self, user: str) -> bool:
                return user == "admin"

        ns.add_specs_from(Spec)
        ns.add_impls_from(AuthImpls())

        results = ns.call("authenticate", user="admin")
        assert results == [True]

    def test_add_impls_from_skips_private_attrs(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def login(self, user: str) -> bool: ...

        class Impl:
            _private = "ignored"

            @hookimpl
            def login(self, user: str) -> bool:
                return True

        ns.add_specs_from(Spec)
        ns.add_impls_from(Impl())
        assert len(ns.get_impls("login")) == 1

    def test_namespace_spec_with_firstresult_kwarg(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec(firstresult=True)
            def find(self, name: str) -> str | None: ...

        class Impl:
            @hookimpl
            def find(self, name: str) -> str | None:
                return f"found:{name}"

        ns.add_specs_from(Spec)
        ns.add_impls_from(Impl())

        results = ns.call("find", name="test")
        assert results == ["found:test"]

    def test_namespace_impl_with_priority_kwarg(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec
            def action(self) -> str: ...

        class ImplFive:
            @hookimpl(priority=5)
            def action(self) -> str:
                return "five"

        class ImplOne:
            @hookimpl(priority=1)
            def action(self) -> str:
                return "one"

        ns.add_specs_from(Spec)
        ns.add_impls_from(ImplFive())
        ns.add_impls_from(ImplOne())

        results = ns.call("action")
        assert results == ["one", "five"]


class TestHookDispatch:
    """Sync/async/firstresult dispatch behavior via caller module."""

    def test_firstresult_stops_after_first_non_none(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec(firstresult=True)
            def find(self, name: str) -> str | None: ...

        class ImplNone:
            @hookimpl(priority=0)
            def find(self, name: str) -> str | None:
                return None

        class ImplHit:
            @hookimpl(priority=1)
            def find(self, name: str) -> str | None:
                return f"found:{name}"

        class ImplSkipped:
            @hookimpl(priority=2)
            def find(self, name: str) -> str | None:
                return "should-not-reach"

        ns.add_specs_from(Spec)
        ns.add_impls_from(ImplNone())
        ns.add_impls_from(ImplHit())
        ns.add_impls_from(ImplSkipped())

        results = ns.call("find", name="alice")
        assert results == ["found:alice"]

    def test_firstresult_returns_empty_if_all_none(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("auth", registry=reg)

        class Spec:
            @hookspec(firstresult=True)
            def find(self, name: str) -> str | None: ...

        class ImplNothing:
            @hookimpl
            def find(self, name: str) -> str | None:
                return None

        ns.add_specs_from(Spec)
        ns.add_impls_from(ImplNothing())

        results = ns.call("find", name="alice")
        assert results == []

    def test_regular_dispatch_collects_all_results(self) -> None:
        reg = HookRegistry()
        ns = HookNamespace("events", registry=reg)

        class Spec:
            @hookspec
            def on_event(self) -> str: ...

        class ImplA:
            @hookimpl
            def on_event(self) -> str:
                return "a"

        class ImplB:
            @hookimpl
            def on_event(self) -> str:
                return "b"

        ns.add_specs_from(Spec)
        ns.add_impls_from(ImplA())
        ns.add_impls_from(ImplB())

        results = ns.call("on_event")
        assert results == ["a", "b"]


class TestHookNamespaceIsolation:
    """Namespaces with the same hook name don't interfere."""

    def test_separate_namespaces_independent(self) -> None:
        reg = HookRegistry()
        ns_auth = HookNamespace("auth", registry=reg)
        ns_cache = HookNamespace("cache", registry=reg)

        class AuthSpec:
            @hookspec
            def reset(self) -> str: ...

        class CacheSpec:
            @hookspec
            def reset(self) -> str: ...

        class AuthImpl:
            @hookimpl
            def reset(self) -> str:
                return "auth-reset"

        class CacheImpl:
            @hookimpl
            def reset(self) -> str:
                return "cache-reset"

        ns_auth.add_specs_from(AuthSpec)
        ns_cache.add_specs_from(CacheSpec)
        ns_auth.add_impls_from(AuthImpl())
        ns_cache.add_impls_from(CacheImpl())

        auth_results = ns_auth.call("reset")
        cache_results = ns_cache.call("reset")

        assert auth_results == ["auth-reset"]
        assert cache_results == ["cache-reset"]
