"""Tests for cache backend registration and configuration."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from bedrock.common.registry import DuplicateRegistrationError
from bedrock.contrib.cache import BackendNotConfiguredError, register_backend, register_backend_lazy
from bedrock.contrib.cache import service as service_module
from bedrock.contrib.cache.backends.memory import InMemoryBackend
from bedrock.contrib.cache.base import CacheBackend
from bedrock.contrib.cache.service import CacheService, list_backends
from bedrock.provider import InvalidProviderError, ProviderCatalog, ProviderLoadError, ProviderSpec


class DuckBackend:
    """Accepts ``settings=`` but implements none of the CacheBackend protocol."""

    def __init__(self, settings: object = None) -> None:
        pass


def _delegate(name: str) -> Callable[..., Any]:
    def method(self: StructuralBackend, *args: Any, **kwargs: Any) -> Any:
        return getattr(self._inner, name)(*args, **kwargs)

    return method


class StructuralBackend:
    """Implements CacheBackend without inheriting it, by delegating every member to an InMemoryBackend."""

    holds_async_resources = False

    def __init__(self, settings: object = None) -> None:
        self._inner = InMemoryBackend()

    @property
    def settings(self) -> object:
        return self._inner.settings


for _name, _member in vars(CacheBackend).items():
    if not _name.startswith("_") and callable(_member):
        setattr(StructuralBackend, _name, _delegate(_name))


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> ProviderCatalog[CacheBackend]:
    """Swap the global backend catalog for a private one so tests leave no residue."""
    registry = ProviderCatalog(
        protocol=CacheBackend,
        specs=(*service_module._BUILTIN_SPECS, ProviderSpec("missing", "bedrock_no_such_cache_module:Backend")),
    )
    monkeypatch.setattr(service_module, "_BACKEND_REGISTRY", registry)
    return registry


class TestRegisterBackend:
    def test_registered_object_that_does_not_implement_protocol_fails_at_configure(
        self, isolated_registry: ProviderCatalog[CacheBackend]
    ) -> None:
        register_backend("duck", DuckBackend)  # type: ignore[arg-type]

        with pytest.raises(BackendNotConfiguredError, match="duck") as excinfo:
            CacheService().configure("duck")

        assert isinstance(excinfo.value.__cause__, InvalidProviderError)

    def test_structural_backend_without_inheritance_is_configured_and_used(
        self, isolated_registry: ProviderCatalog[CacheBackend]
    ) -> None:
        register_backend("structural", StructuralBackend)  # type: ignore[arg-type]
        service = CacheService()

        assert isinstance(service.configure("structural"), StructuralBackend)
        service.set("k", "v")
        assert service.get("k") == "v"

    def test_same_object_again_is_a_no_op_but_a_builtin_name_conflicts(
        self, isolated_registry: ProviderCatalog[CacheBackend]
    ) -> None:
        register_backend("structural", StructuralBackend)  # type: ignore[arg-type]
        register_backend("structural", StructuralBackend)  # type: ignore[arg-type]

        with pytest.raises(DuplicateRegistrationError):
            register_backend("memory", StructuralBackend)  # type: ignore[arg-type]
        with pytest.raises(DuplicateRegistrationError):
            register_backend_lazy("memory", "acme_cache:Backend")
        assert list_backends() == ["memory", "missing", "redis", "structural"]

    def test_lazy_backend_is_imported_only_when_configured(
        self, isolated_registry: ProviderCatalog[CacheBackend], fake_package: Path
    ) -> None:
        (fake_package / "acme_lazy_cache.py").write_text(
            "from bedrock.contrib.cache.backends.memory import InMemoryBackend as Backend\n"
        )
        register_backend_lazy("lazy", "acme_lazy_cache:Backend", install_requirement="acme[x]")

        assert "lazy" in list_backends()
        assert "acme_lazy_cache" not in sys.modules
        CacheService().configure("lazy")
        assert "acme_lazy_cache" in sys.modules


class TestConfigure:
    def test_failed_lazy_load_is_translated_with_cause(self, isolated_registry: object) -> None:
        with pytest.raises(BackendNotConfiguredError, match="missing") as excinfo:
            CacheService().configure("missing")

        assert isinstance(excinfo.value.__cause__, ProviderLoadError)

    def test_missing_redis_sdk_names_the_install_extra(
        self, isolated_registry: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "redis", None)

        with pytest.raises(BackendNotConfiguredError, match=r"bedrock-core\[cache-redis\]") as excinfo:
            CacheService().configure("redis")

        assert isinstance(excinfo.value.__cause__, ProviderLoadError)

    def test_failed_configure_keeps_the_previous_backend(self, isolated_registry: object) -> None:
        service = CacheService()
        backend = service.configure("memory")

        with pytest.raises(BackendNotConfiguredError):
            service.configure("missing")

        assert service.get_backend() is backend


class TestCacheBackendEnv:
    def test_unset_env_configures_memory(self, isolated_registry: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHE_BACKEND", raising=False)

        assert isinstance(CacheService().get_backend(), InMemoryBackend)

    def test_env_selects_backend_for_configure_and_lazy_get_backend(
        self, isolated_registry: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_backend("structural", StructuralBackend)  # type: ignore[arg-type]
        monkeypatch.setenv("CACHE_BACKEND", "structural")

        assert isinstance(CacheService().configure(), StructuralBackend)
        assert isinstance(CacheService().get_backend(), StructuralBackend)

    def test_explicit_name_wins_over_env(self, isolated_registry: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHE_BACKEND", "missing")

        assert isinstance(CacheService().configure("memory"), InMemoryBackend)

    def test_unknown_env_name_says_where_it_came_from(
        self, isolated_registry: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CACHE_BACKEND", "redsi")

        with pytest.raises(BackendNotConfiguredError, match=r"'redsi' \(from CACHE_BACKEND\)"):
            CacheService().configure()


def test_importing_cache_loads_no_backend_module() -> None:
    code = "import sys, bedrock.contrib.cache; print('bedrock.contrib.cache.backends.redis' in sys.modules)"

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "False"
