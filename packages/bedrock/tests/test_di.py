"""Unit tests for the Bedrock DI container."""

import threading

import pytest
from bedrock.di import Container, Lifetime, container
from bedrock.di.exc import DuplicateServiceError, ScopeError, ServiceNotFoundError


class _FakeService:
    """Minimal service stub for DI tests."""

    def __init__(self, value: int = 0) -> None:
        self.value = value
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _AnotherService:
    pass


class TestContainerRegistration:
    """register / register_instance / is_registered / resolve / reset."""

    def test_register_and_resolve_singleton(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService(42), lifetime=Lifetime.SINGLETON)

        assert c.is_registered(_FakeService) is True
        instance = c.resolve(_FakeService)
        assert instance.value == 42

    def test_register_with_string_key(self) -> None:
        c = Container()
        c.register("my_svc", factory=lambda: _FakeService(7), lifetime=Lifetime.SINGLETON)

        assert c.is_registered("my_svc") is True
        assert c.resolve("my_svc").value == 7

    def test_register_duplicate_raises(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService())

        with pytest.raises(DuplicateServiceError):
            c.register(_FakeService, factory=lambda: _FakeService())

    def test_register_instance(self) -> None:
        c = Container()
        sentinel = _FakeService(99)
        c.register_instance(_FakeService, sentinel)

        assert c.resolve(_FakeService) is sentinel

    def test_register_instance_duplicate_raises(self) -> None:
        c = Container()
        c.register_instance(_FakeService, _FakeService())

        with pytest.raises(DuplicateServiceError):
            c.register_instance(_FakeService, _FakeService())

    def test_is_registered_returns_false_for_unknown(self) -> None:
        c = Container()
        assert c.is_registered(_FakeService) is False

    def test_resolve_unregistered_raises(self) -> None:
        c = Container()
        with pytest.raises(ServiceNotFoundError):
            c.resolve(_FakeService)

    def test_resolve_unregistered_string_key_raises(self) -> None:
        c = Container()
        with pytest.raises(ServiceNotFoundError):
            c.resolve("nonexistent")

    def test_reset_clears_registrations(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService())
        c.resolve(_FakeService)

        c.reset()

        assert c.is_registered(_FakeService) is False
        with pytest.raises(ServiceNotFoundError):
            c.resolve(_FakeService)


class TestContainerLifetimes:
    """SINGLETON / TRANSIENT / SCOPED lifetime behaviors."""

    def test_singleton_returns_same_instance(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService(1), lifetime=Lifetime.SINGLETON)

        a = c.resolve(_FakeService)
        b = c.resolve(_FakeService)

        assert a is b

    def test_transient_returns_new_instance_each_time(self) -> None:
        c = Container()
        call_count = 0

        def factory() -> _FakeService:
            nonlocal call_count
            call_count += 1
            return _FakeService(call_count)

        c.register(_FakeService, factory=factory, lifetime=Lifetime.TRANSIENT)

        a = c.resolve(_FakeService)
        b = c.resolve(_FakeService)

        assert a is not b
        assert a.value == 1
        assert b.value == 2

    def test_scoped_returns_same_instance_within_scope(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService(5), lifetime=Lifetime.SCOPED)

        with c.scope("test"):
            a = c.resolve(_FakeService)
            b = c.resolve(_FakeService)
            assert a is b
            assert a.value == 5

    def test_scoped_raises_without_active_scope(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService(), lifetime=Lifetime.SCOPED)

        with pytest.raises(ScopeError, match="without an active scope"):
            c.resolve(_FakeService)

    def test_scoped_different_instances_in_different_scopes(self) -> None:
        c = Container()
        counter = 0

        def factory() -> _FakeService:
            nonlocal counter
            counter += 1
            return _FakeService(counter)

        c.register(_FakeService, factory=factory, lifetime=Lifetime.SCOPED)

        with c.scope("first"):
            a = c.resolve(_FakeService)

        with c.scope("second"):
            b = c.resolve(_FakeService)

        assert a is not b
        assert a.value == 1
        assert b.value == 2


class TestContainerScope:
    """Scope lifecycle and cleanup behavior."""

    def test_scope_cleanup_calls_close(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService(), lifetime=Lifetime.SCOPED)

        with c.scope("test"):
            instance = c.resolve(_FakeService)

        assert instance.closed is True

    def test_scope_without_close_method_does_not_error(self) -> None:
        c = Container()
        c.register(_AnotherService, factory=lambda: _AnotherService(), lifetime=Lifetime.SCOPED)

        with c.scope("test"):
            c.resolve(_AnotherService)

    def test_nested_scopes_isolate_instances(self) -> None:
        c = Container()
        counter = 0

        def factory() -> _FakeService:
            nonlocal counter
            counter += 1
            return _FakeService(counter)

        c.register(_FakeService, factory=factory, lifetime=Lifetime.SCOPED)

        with c.scope("outer"):
            outer = c.resolve(_FakeService)
            with c.scope("inner"):
                inner = c.resolve(_FakeService)
                assert inner is not outer

    def test_default_scope_name(self) -> None:
        c = Container()
        c.register(_FakeService, factory=lambda: _FakeService(), lifetime=Lifetime.SCOPED)

        with c.scope():
            instance = c.resolve(_FakeService)
            assert instance is not None


class TestContainerOverride:
    """Context-manager override behavior."""

    def test_override_replaces_service(self) -> None:
        c = Container()
        original = _FakeService(1)
        replacement = _FakeService(2)
        c.register_instance(_FakeService, original)

        with c.override(_FakeService, replacement):
            assert c.resolve(_FakeService) is replacement

    def test_override_restores_after_exit(self) -> None:
        c = Container()
        original = _FakeService(1)
        replacement = _FakeService(2)
        c.register_instance(_FakeService, original)

        with c.override(_FakeService, replacement):
            pass

        assert c.resolve(_FakeService) is original

    def test_override_for_unregistered_key(self) -> None:
        c = Container()
        replacement = _FakeService(42)

        with c.override(_FakeService, replacement):
            assert c.resolve(_FakeService) is replacement

        assert c.is_registered(_FakeService) is False

    def test_nested_overrides(self) -> None:
        c = Container()
        original = _FakeService(1)
        mid = _FakeService(2)
        inner = _FakeService(3)
        c.register_instance(_FakeService, original)

        with c.override(_FakeService, mid):
            assert c.resolve(_FakeService) is mid
            with c.override(_FakeService, inner):
                assert c.resolve(_FakeService) is inner
            assert c.resolve(_FakeService) is mid

        assert c.resolve(_FakeService) is original

    def test_override_cleans_up_singleton_cache(self) -> None:
        c = Container()
        replacement = _FakeService(99)

        with c.override(_FakeService, replacement):
            c.resolve(_FakeService)

        assert c.is_registered(_FakeService) is False


class TestContainerThreadSafety:
    """Thread-safe singleton resolution."""

    def test_singleton_resolution_is_thread_safe(self) -> None:
        c = Container()
        instances: list[_FakeService] = []
        barrier = threading.Barrier(10)

        c.register(_FakeService, factory=lambda: _FakeService(1), lifetime=Lifetime.SINGLETON)

        def resolve_in_thread() -> None:
            barrier.wait()
            instances.append(c.resolve(_FakeService))

        threads = [threading.Thread(target=resolve_in_thread) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(inst is instances[0] for inst in instances)


class TestProviderDecorator:
    """@provider class decorator for global container registration."""

    def setup_method(self) -> None:
        container.reset()

    def test_provider_registers_class_as_singleton(self) -> None:
        from bedrock.di.decorators import provider

        @provider
        class MyService:
            pass

        assert container.is_registered(MyService) is True
        a = container.resolve(MyService)
        b = container.resolve(MyService)
        assert a is b

    def test_provider_with_explicit_key(self) -> None:
        from bedrock.di.decorators import provider

        class IRepo:
            pass

        @provider(IRepo, lifetime=Lifetime.SINGLETON)
        class SqlRepo(IRepo):
            pass

        assert container.is_registered(IRepo) is True
        instance = container.resolve(IRepo)
        assert isinstance(instance, SqlRepo)

    def test_provider_with_explicit_key_and_default_lifetime(self) -> None:
        from bedrock.di.decorators import provider

        class IRepository:
            pass

        @provider(IRepository)
        class SqlRepository(IRepository):
            pass

        assert container.is_registered(IRepository) is True
        assert container.is_registered(SqlRepository) is False
        instance = container.resolve(IRepository)
        assert isinstance(instance, SqlRepository)

    def test_provider_bare_decorator_still_registers_concrete_class(self) -> None:
        from bedrock.di.decorators import provider

        @provider
        class ConcreteService:
            pass

        assert container.is_registered(ConcreteService) is True
        assert isinstance(container.resolve(ConcreteService), ConcreteService)

    def test_provider_with_lifetime(self) -> None:
        from bedrock.di.decorators import provider

        @provider(lifetime=Lifetime.TRANSIENT)
        class TransientService:
            pass

        a = container.resolve(TransientService)
        b = container.resolve(TransientService)
        assert a is not b

    def test_provider_with_key_and_lifetime(self) -> None:
        from bedrock.di import provider

        class ILogger:
            pass

        @provider(ILogger, lifetime=Lifetime.TRANSIENT)
        class FileLogger(ILogger):
            pass

        assert container.is_registered(ILogger) is True
        a = container.resolve(ILogger)
        b = container.resolve(ILogger)
        assert a is not b
        assert isinstance(a, FileLogger)

    def test_container_provider_registers_into_custom_container_only(self) -> None:
        custom = Container()
        container.reset()

        @custom.provider
        class MyService:
            pass

        assert custom.is_registered(MyService) is True
        assert container.is_registered(MyService) is False
        assert isinstance(custom.resolve(MyService), MyService)

    def test_container_provider_with_explicit_key_uses_custom_container(self) -> None:
        custom = Container()
        container.reset()

        class IService:
            pass

        @custom.provider(IService, lifetime=Lifetime.SINGLETON)
        class MyService(IService):
            pass

        assert custom.is_registered(IService) is True
        assert container.is_registered(IService) is False
        assert isinstance(custom.resolve(IService), MyService)

    def test_container_provider_with_explicit_key_and_default_lifetime(self) -> None:
        custom = Container()
        container.reset()

        class IService:
            pass

        @custom.provider(IService)
        class MyService(IService):
            pass

        assert custom.is_registered(IService) is True
        assert custom.is_registered(MyService) is False
        assert container.is_registered(IService) is False
        assert isinstance(custom.resolve(IService), MyService)


class TestInjectDecorator:
    """@inject function decorator for dependency resolution."""

    def setup_method(self) -> None:
        container.reset()

    def test_inject_resolves_dependencies(self) -> None:
        from bedrock.di.decorators import inject

        svc = _FakeService(42)
        container.register_instance(_FakeService, svc)

        @inject(dep=_FakeService)
        def my_func(*, dep: _FakeService) -> int:
            return dep.value

        assert my_func() == 42

    def test_inject_explicit_kwargs_take_precedence(self) -> None:
        from bedrock.di.decorators import inject

        container.register_instance(_FakeService, _FakeService(1))
        override = _FakeService(99)

        @inject(dep=_FakeService)
        def my_func(*, dep: _FakeService) -> int:
            return dep.value

        assert my_func(dep=override) == 99

    def test_inject_with_string_key(self) -> None:
        from bedrock.di.decorators import inject

        container.register_instance("cache", _FakeService(77))

        @inject(cache="cache")
        def my_func(*, cache: _FakeService) -> int:
            return cache.value

        assert my_func() == 77

    def test_inject_preserves_function_metadata(self) -> None:
        from bedrock.di import inject

        @inject(dep=_FakeService)
        def my_func():
            """My docstring."""
            pass

        assert my_func.__name__ == "my_func"
        assert my_func.__doc__ == "My docstring."

    def test_container_inject_resolves_from_custom_container_only(self) -> None:
        custom = Container()
        container.reset()
        custom.register_instance(_FakeService, _FakeService(55))

        @custom.inject(dep=_FakeService)
        def my_func(*, dep: _FakeService) -> int:
            return dep.value

        assert my_func() == 55

    def test_container_inject_does_not_resolve_from_default_container(self) -> None:
        custom = Container()
        container.reset()
        container.register_instance(_FakeService, _FakeService(1))
        custom.register_instance(_FakeService, _FakeService(2))

        @custom.inject(dep=_FakeService)
        def my_func(*, dep: _FakeService) -> int:
            return dep.value

        assert my_func() == 2

    def test_top_level_aliases_remain_bound_to_default_container(self) -> None:
        from bedrock.di import inject, provider

        custom = Container()
        custom.register_instance(_FakeService, _FakeService(9))
        container.register_instance(_FakeService, _FakeService(4))

        @provider
        class MyService:
            pass

        @inject(dep=_FakeService)
        def my_func(*, dep: _FakeService) -> int:
            return dep.value

        assert container.is_registered(MyService) is True
        assert custom.is_registered(MyService) is False
        assert my_func() == 4
