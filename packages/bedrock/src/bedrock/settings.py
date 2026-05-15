from pydantic_settings import BaseSettings, SettingsConfigDict

from .conf import SettingsProxy


class BedrockSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEDROCK_")
    APP: str | None = None


class BedrockRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEDROCK_RUNTIME_")
    APP: str | None = None
    APP_FACTORY: str


settings: BedrockSettings = SettingsProxy(BedrockSettings)  # type: ignore[assignment]

__all__ = ["settings"]