"""Behavior tests for cache backend registration and configuration."""

from __future__ import annotations

import pytest
from bedrock.common.registry import ClassLoadError, ClassRegistry, InvalidRegisteredClassError
from bedrock.contrib.cache import BackendNotConfiguredError, register_backend
from bedrock.contrib.cache import service as service_module
from bedrock.contrib.cache.base import CacheBackend
from bedrock.contrib.cache.service import CacheService


class DuckBackend:
    """Has the right shape in spirit but no relation to the CacheBackend protocol."""


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> ClassRegistry[CacheBackend]:
    """Swap the global backend registry for a private one so tests leave no residue."""
    registry = ClassRegistry[CacheBackend](
        base_class=CacheBackend,
        lazy_classes={
            "memory": "bedrock.contrib.cache.backends.memory:InMemoryBackend",
            "missing": "bedrock_no_such_cache_module:Backend",
        },
    )
    monkeypatch.setattr(service_module, "_BACKEND_REGISTRY", registry)
    return registry


class TestRegisterBackend:
    def test_rejects_class_that_does_not_subclass_protocol(
        self, isolated_registry: ClassRegistry[CacheBackend]
    ) -> None:
        with pytest.raises(InvalidRegisteredClassError):
            register_backend("duck", DuckBackend)  # type: ignore[arg-type]

        assert not isolated_registry.has("duck")


class TestConfigure:
    def test_failed_lazy_load_is_translated_with_cause(self, isolated_registry: object) -> None:
        with pytest.raises(BackendNotConfiguredError, match="missing") as excinfo:
            CacheService().configure("missing")

        assert isinstance(excinfo.value.__cause__, ClassLoadError)
