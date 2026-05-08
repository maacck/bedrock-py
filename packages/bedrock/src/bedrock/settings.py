from pydantic_settings import SettingsConfigDict

from .conf import LazySettings


class BedrockSettings(LazySettings):
    model_config = SettingsConfigDict(env_prefix="BEDROCK_")
    APP: str | None = None


class BedrockRuntimeSettings(LazySettings):
    model_config = SettingsConfigDict(env_prefix="BEDROCK_RUNTIME_")
    APP: str | None = None
    APP_FACTORY: str


runtime_settings = BedrockRuntimeSettings()
settings = BedrockSettings()
