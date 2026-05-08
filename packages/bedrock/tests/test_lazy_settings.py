"""Unit tests for the LazySettings deferred-init proxy."""

from __future__ import annotations

import pytest
from bedrock.conf import LazySettings
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings


class DummyAppConfig(LazySettings):
    APP_ENV: str
    DB_PORT: int = Field(default=5432)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove test env vars before each test to avoid cross-contamination."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DB_PORT", raising=False)


class TestLazyInstantiation:
    """Verify that LazySettings defers validation until first attribute access."""

    def test_instantiation_does_not_trigger_validation(self) -> None:
        config = DummyAppConfig()
        assert isinstance(config, DummyAppConfig)

    def test_access_triggers_validation_error_when_env_missing(self) -> None:
        config = DummyAppConfig()

        with pytest.raises(ValidationError, match="APP_ENV"):
            _ = config.APP_ENV

    def test_successful_access_after_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = DummyAppConfig()

        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("DB_PORT", "3306")

        assert config.APP_ENV == "production"
        assert config.DB_PORT == 3306


class TestLazySettingsProxy:
    """Verify proxy behaviour: setattr, isinstance camouflage, and dir()."""

    def test_setattr_triggers_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = DummyAppConfig()
        monkeypatch.setenv("APP_ENV", "test")

        config.DB_PORT = 6379

        assert config.APP_ENV == "test"
        assert config.DB_PORT == 6379

    def test_isinstance_camouflage(self) -> None:
        config = DummyAppConfig()

        assert isinstance(config, DummyAppConfig)
        assert isinstance(config, BaseSettings)

    def test_dir_includes_model_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config = DummyAppConfig()
        monkeypatch.setenv("APP_ENV", "dev")

        attrs = dir(config)
        assert "APP_ENV" in attrs
        assert "DB_PORT" in attrs
