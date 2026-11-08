"""Released contrib modules close sync and async resources through the registry shutdown entrypoints."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import Iterator

import pytest
from bedrock.contrib.cache import CacheCloseError, CacheService, cache, register_backend
from bedrock.contrib.cache.backends.memory import InMemoryBackend
from bedrock.contrib.metric import MetricsManager, metrics
from bedrock.contrib.storage import storage
from bedrock.module import ModuleAsyncRequiredError, ModuleLifecycleError, ModuleLifecycleState, ModuleRegistry

pytestmark = pytest.mark.filterwarnings(
    "error::RuntimeWarning",
    "ignore::ResourceWarning",
    "error::pytest.PytestUnraisableExceptionWarning",
)


@pytest.fixture(autouse=True)
def _collect_garbage() -> Iterator[None]:
    yield
    gc.collect()


class FakeRedisLikeBackend(InMemoryBackend):
    """In-memory backend that behaves like Redis: separate sync and async clients with observable close."""

    holds_async_resources = True
    log: list[str] = []
    fail_close = False
    fail_aclose = False

    def close(self) -> None:
        type(self).log.append("close")
        if type(self).fail_close:
            raise RuntimeError("sync client boom")

    async def aclose(self) -> None:
        type(self).log.append("aclose:start")
        await asyncio.sleep(0.01)
        if type(self).fail_aclose:
            raise RuntimeError("async client boom")
        type(self).log.append("aclose:done")


@pytest.fixture(autouse=True, scope="module")
def _register_fake_redis() -> None:
    register_backend("lifecycle-fake-redis", FakeRedisLikeBackend)


@pytest.fixture
def fake_log() -> Iterator[list[str]]:
    FakeRedisLikeBackend.log = []
    FakeRedisLikeBackend.fail_close = False
    FakeRedisLikeBackend.fail_aclose = False
    yield FakeRedisLikeBackend.log


@pytest.fixture
def clean_cache() -> Iterator[CacheService]:
    cache._backend = None
    cache._open = []
    yield cache
    cache._backend = None
    cache._open = []


class TestCacheService:
    def test_close_and_aclose_each_release_their_own_side(self, fake_log: list[str]) -> None:
        service = CacheService()
        service.configure("lifecycle-fake-redis")

        assert service.has_async_resources() is True
        service.close()
        assert fake_log == ["close"]
        assert service.has_async_resources() is True
        assert service._backend is None

        asyncio.run(service.aclose())
        assert fake_log == ["close", "aclose:start", "aclose:done"]
        assert service.has_async_resources() is False
        assert service._open == []

    def test_aclose_first_then_close(self, fake_log: list[str]) -> None:
        service = CacheService()
        service.configure("lifecycle-fake-redis")

        asyncio.run(service.aclose())
        service.close()

        assert fake_log == ["aclose:start", "aclose:done", "close"]
        assert service._open == []

    def test_replaced_backend_is_still_closed(self, fake_log: list[str]) -> None:
        service = CacheService()
        service.configure("lifecycle-fake-redis")
        service.configure("lifecycle-fake-redis")

        service.close()
        asyncio.run(service.aclose())

        assert fake_log.count("close") == 2
        assert fake_log.count("aclose:done") == 2

    def test_memory_backend_needs_no_async_close(self) -> None:
        service = CacheService()
        service.configure("memory")

        assert service.has_async_resources() is False
        service.close()
        assert service._open == []

    def test_failures_attempt_every_backend_and_can_be_retried(self, fake_log: list[str]) -> None:
        service = CacheService()
        service.configure("lifecycle-fake-redis")
        service.configure("lifecycle-fake-redis")
        FakeRedisLikeBackend.fail_close = True

        with pytest.raises(CacheCloseError, match=r"2 cache backend\(s\) \(sync\)"):
            service.close()
        assert fake_log == ["close", "close"]
        assert service._backend is not None

        FakeRedisLikeBackend.fail_close = False
        service.close()
        assert fake_log == ["close", "close", "close", "close"]
        assert service._backend is None

    def test_async_failure_is_reported_and_retryable(self, fake_log: list[str]) -> None:
        service = CacheService()
        service.configure("lifecycle-fake-redis")
        service.close()
        FakeRedisLikeBackend.fail_aclose = True

        with pytest.raises(CacheCloseError, match=r"\(async\)"):
            asyncio.run(service.aclose())
        assert service.has_async_resources() is True

        FakeRedisLikeBackend.fail_aclose = False
        asyncio.run(service.aclose())
        assert service.has_async_resources() is False

    def test_close_without_backend_is_a_no_op(self) -> None:
        service = CacheService()
        service.close()
        asyncio.run(service.aclose())
        assert service.has_async_resources() is False


class TestRedisBackendClients:
    def test_close_and_aclose_release_different_clients(self, monkeypatch: pytest.MonkeyPatch) -> None:
        redis = pytest.importorskip("redis")
        from bedrock.contrib.cache.backends.redis import RedisBackend

        backend = RedisBackend()
        calls: list[str] = []

        async def aclose_async_client() -> None:
            calls.append("async")

        monkeypatch.setattr(backend._client, "close", lambda: calls.append("sync"))
        monkeypatch.setattr(backend._async_client, "close", aclose_async_client)

        assert backend.holds_async_resources is True
        backend.close()
        assert calls == ["sync"]
        asyncio.run(backend.aclose())
        assert calls == ["sync", "async"]
        assert redis is not None


class TestCacheModuleHooks:
    def _registry(self, clean_cache: CacheService) -> ModuleRegistry:
        registry = ModuleRegistry()
        registry.install("bedrock.contrib.cache")
        clean_cache.configure("lifecycle-fake-redis")
        return registry

    def test_sync_shutdown_awaits_the_async_close_before_reporting_stopped(
        self, clean_cache: CacheService, fake_log: list[str]
    ) -> None:
        registry = self._registry(clean_cache)

        registry.shutdown()

        assert fake_log == ["close", "aclose:start", "aclose:done"]
        assert registry._app_configs["bedrock.contrib.cache"].lifecycle_state is ModuleLifecycleState.SHUTDOWN
        assert not registry._adapter.is_open

    @pytest.mark.asyncio
    async def test_async_shutdown_awaits_the_async_close(self, clean_cache: CacheService, fake_log: list[str]) -> None:
        registry = self._registry(clean_cache)

        await registry.ashutdown()

        assert fake_log == ["close", "aclose:start", "aclose:done"]
        assert registry._app_configs["bedrock.contrib.cache"].lifecycle_state is ModuleLifecycleState.SHUTDOWN
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    @pytest.mark.asyncio
    async def test_sync_shutdown_in_a_running_loop_is_rejected_then_async_finishes(
        self, clean_cache: CacheService, fake_log: list[str]
    ) -> None:
        registry = self._registry(clean_cache)

        with pytest.raises(ModuleAsyncRequiredError, match=r"ashutdown"):
            registry.shutdown()

        assert fake_log == ["close"]
        assert registry._app_configs["bedrock.contrib.cache"].lifecycle_state is ModuleLifecycleState.LOADED

        await registry.ashutdown()
        assert fake_log == ["close", "aclose:start", "aclose:done"]
        assert registry._app_configs["bedrock.contrib.cache"].lifecycle_state is ModuleLifecycleState.SHUTDOWN

    @pytest.mark.asyncio
    async def test_memory_only_cache_still_shuts_down_in_a_running_loop(self, clean_cache: CacheService) -> None:
        registry = ModuleRegistry()
        registry.install("bedrock.contrib.cache")
        clean_cache.configure("memory")

        registry.shutdown()

        assert registry._app_configs["bedrock.contrib.cache"].lifecycle_state is ModuleLifecycleState.SHUTDOWN

    def test_ready_configures_the_backend_named_by_cache_backend(
        self, clean_cache: CacheService, fake_log: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CACHE_BACKEND", "lifecycle-fake-redis")
        registry = ModuleRegistry()
        registry.install("bedrock.contrib.cache")

        registry.mark_ready()

        assert isinstance(clean_cache.get_backend(), FakeRedisLikeBackend)
        asyncio.run(registry.ashutdown())
        assert fake_log == ["close", "aclose:start", "aclose:done"]

    def test_async_close_failure_keeps_the_module_retryable(
        self, clean_cache: CacheService, fake_log: list[str]
    ) -> None:
        registry = self._registry(clean_cache)
        FakeRedisLikeBackend.fail_aclose = True

        with pytest.raises(ModuleLifecycleError, match="async client boom"):
            registry.shutdown()
        assert registry._app_configs["bedrock.contrib.cache"].lifecycle_state is ModuleLifecycleState.LOADED

        FakeRedisLikeBackend.fail_aclose = False
        registry.shutdown()
        assert registry._app_configs["bedrock.contrib.cache"].lifecycle_state is ModuleLifecycleState.SHUTDOWN
        assert fake_log[-1] == "aclose:done"


class _AsyncProvider:
    """Provider with an async client whose flush and close completion is observable."""

    def __init__(self, log: list[str], *, fail_flush: bool = False) -> None:
        self.log = log
        self.fail_flush = fail_flush

    def emit(self, event: object) -> None:
        self.log.append("emit")

    def flush(self) -> None:
        self.log.append("flush")

    def close(self) -> None:
        self.log.append("close")

    async def aflush(self) -> None:
        self.log.append("aflush:start")
        await asyncio.sleep(0.01)
        if self.fail_flush:
            raise RuntimeError("flush boom")
        self.log.append("aflush:done")

    async def aclose(self) -> None:
        self.log.append("aclose:start")
        await asyncio.sleep(0.01)
        self.log.append("aclose:done")


class _SyncProvider:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def emit(self, event: object) -> None:
        pass

    def close(self) -> None:
        self.log.append("sync-provider-close")


class TestMetricsManager:
    def test_flush_runs_before_close_then_async_flush_before_async_close(self) -> None:
        log: list[str] = []
        manager = MetricsManager()
        manager.register_provider(_AsyncProvider(log))

        manager.close()
        assert log == ["flush", "close"]
        assert manager.has_async_resources() is True

        asyncio.run(manager.aclose())
        assert log == ["flush", "close", "aflush:start", "aflush:done", "aclose:start", "aclose:done"]
        assert manager.has_async_resources() is False
        assert manager.list_providers() == []

    def test_every_provider_is_attempted_and_failures_are_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        log: list[str] = []
        manager = MetricsManager()
        manager.register_provider(_AsyncProvider(log, fail_flush=True))
        manager.register_provider(_SyncProvider(log))

        manager.close()
        asyncio.run(manager.aclose())

        assert "sync-provider-close" in log
        assert "aclose:done" in log
        assert "aflush:start" in log and "aflush:done" not in log

    def test_sync_only_providers_need_no_async_close(self) -> None:
        log: list[str] = []
        manager = MetricsManager()
        manager.register_provider(_SyncProvider(log))

        assert manager.has_async_resources() is False
        manager.close()
        assert log == ["sync-provider-close"]
        assert manager.has_async_resources() is False


@pytest.fixture
def clean_metrics() -> Iterator[MetricsManager]:
    metrics._providers.clear()
    metrics._async_pending.clear()
    yield metrics
    metrics._providers.clear()
    metrics._async_pending.clear()


class TestMetricsModuleHooks:
    def test_sync_shutdown_flushes_and_closes_before_reporting_stopped(self, clean_metrics: MetricsManager) -> None:
        log: list[str] = []
        clean_metrics.register_provider(_AsyncProvider(log))
        registry = ModuleRegistry()
        registry.install("bedrock.contrib.metric")

        registry.shutdown()

        assert log == ["flush", "close", "aflush:start", "aflush:done", "aclose:start", "aclose:done"]
        assert registry._app_configs["bedrock.contrib.metric"].lifecycle_state is ModuleLifecycleState.SHUTDOWN
        assert not registry._adapter.is_open

    @pytest.mark.asyncio
    async def test_async_shutdown_awaits_the_provider(self, clean_metrics: MetricsManager) -> None:
        log: list[str] = []
        clean_metrics.register_provider(_AsyncProvider(log))
        registry = ModuleRegistry()
        await registry.ainstall("bedrock.contrib.metric")

        await registry.ashutdown()

        assert log[-2:] == ["aclose:start", "aclose:done"]
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    @pytest.mark.asyncio
    async def test_sync_providers_shut_down_in_a_running_loop(self, clean_metrics: MetricsManager) -> None:
        log: list[str] = []
        clean_metrics.register_provider(_SyncProvider(log))
        registry = ModuleRegistry()
        registry.install("bedrock.contrib.metric")

        registry.shutdown()

        assert log == ["sync-provider-close"]
        assert registry._app_configs["bedrock.contrib.metric"].lifecycle_state is ModuleLifecycleState.SHUTDOWN


class _FakeS3Backend:
    """Stand-in for the synchronous S3 backend; boto3 has no async client in Bedrock."""

    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class TestStorageModuleHooks:
    @pytest.mark.parametrize("mode", ["sync", "async"])
    def test_sync_s3_backend_is_closed_by_both_entrypoints(self, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _FakeS3Backend()
        monkeypatch.setattr(storage, "_backend", backend)
        registry = ModuleRegistry()
        registry.install("bedrock.contrib.storage")

        if mode == "sync":
            registry.shutdown()
        else:
            asyncio.run(registry.ashutdown())

        assert backend.closed == 1
        assert storage._backend is None
