"""Unit tests for the SettingsProxy deferred-init proxy."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from bedrock.conf import SettingsProxy
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class DummyAppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DUMMY_")
    APP_ENV: str
    DB_PORT: int = Field(default=5432)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUMMY_APP_ENV", raising=False)
    monkeypatch.delenv("DUMMY_DB_PORT", raising=False)


class TestLazyInstantiation:
    def test_proxy_does_not_trigger_validation_on_creation(self) -> None:
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]
        assert proxy._wrapped is None

    def test_access_triggers_validation_error_when_env_missing(self) -> None:
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]
        with pytest.raises(ValidationError, match="APP_ENV"):
            _ = proxy.APP_ENV

    def test_successful_access_after_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]
        monkeypatch.setenv("DUMMY_APP_ENV", "production")
        monkeypatch.setenv("DUMMY_DB_PORT", "3306")

        assert proxy.APP_ENV == "production"
        assert proxy.DB_PORT == 3306

    def test_factory_called_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "test")
        call_count = 0
        original_init = DummyAppConfig.__init__

        def counting_init(self_inner, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            original_init(self_inner, *args, **kwargs)

        monkeypatch.setattr(DummyAppConfig, "__init__", counting_init)
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        _ = proxy.APP_ENV
        _ = proxy.DB_PORT
        _ = proxy.APP_ENV

        assert call_count == 1


class TestProxyDelegation:
    def test_setattr_delegates_to_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "test")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        proxy.DB_PORT = 6379

        assert proxy.APP_ENV == "test"
        assert proxy.DB_PORT == 6379

    def test_isinstance_camouflage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "test")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        _ = proxy.APP_ENV
        assert isinstance(proxy, DummyAppConfig)
        assert isinstance(proxy, BaseSettings)

    def test_dir_includes_model_fields(self) -> None:
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        attrs = dir(proxy)
        assert "APP_ENV" in attrs
        assert "DB_PORT" in attrs

    def test_repr_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "dev")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        r = repr(proxy)
        assert "DummyAppConfig" in r
        assert "dev" in r

    def test_str_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "dev")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        s = str(proxy)
        assert "dev" in s

    def test_bool_triggers_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "test")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        assert bool(proxy) is True

    def test_eq_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "test")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        _ = proxy.APP_ENV
        other = DummyAppConfig()
        assert proxy == other

    def test_hash_delegates_to_unhashable_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "test")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        with pytest.raises(TypeError, match="unhashable"):
            hash(proxy)

    def test_model_dump_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "staging")
        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        data = proxy.model_dump()
        assert data["APP_ENV"] == "staging"
        assert data["DB_PORT"] == 5432


class TestThreadSafety:
    def test_concurrent_first_access_single_instantiation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DUMMY_APP_ENV", "concurrent")
        barrier = threading.Barrier(4)
        results: list[str] = []

        proxy: DummyAppConfig = SettingsProxy(DummyAppConfig)  # type: ignore[assignment]

        def reader() -> None:
            barrier.wait()
            results.append(proxy.APP_ENV)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert all(r == "concurrent" for r in results)
        assert len(results) == 4


class TestCallableFactory:
    def test_accepts_callable_factory(self) -> None:
        mock_settings = MagicMock()
        mock_settings.VALUE = 42

        proxy = SettingsProxy(lambda: mock_settings)

        assert proxy.VALUE == 42
