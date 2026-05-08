from pydantic import BaseModel, ConfigDict


class BedrockEntity(BaseModel):
    """Base entity model used across the Bedrock runtime."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
