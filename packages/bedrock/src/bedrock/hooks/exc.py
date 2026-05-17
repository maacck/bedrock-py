"""Hook system exceptions."""

from bedrock.exc import BedrockExc


class HookError(BedrockExc):
    """Base exception for all hook-related errors."""

    detail: str = "Hook system error."


class HookSpecNotFoundError(HookError):
    """Raised when a hook specification is not found in the registry."""

    detail: str = "Hook specification not found."


class HookCallError(HookError):
    """Raised when a hook call fails."""

    detail: str = "Hook call failed."


class HookValidationError(HookError):
    """Raised when hook validation fails."""

    detail: str = "Hook validation failed."
