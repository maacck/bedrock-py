"""Exception types for the Bedrock module runtime."""

from __future__ import annotations

from ..exc import BedrockExc


class AppRegistryNotReady(BedrockExc):
    """Raised when code expects a populated module registry before load."""

    detail: str = "App registry isn't ready yet."


class ModuleError(BedrockExc):
    """Base exception for Bedrock module runtime failures."""

    detail: str = "Module runtime error."


class InvalidManifestError(ModuleError):
    """Raised when a module manifest is missing or invalid."""

    detail: str = "Invalid module manifest."


class DuplicateModuleError(ModuleError):
    """Raised when discovered modules are not uniquely identifiable."""

    detail: str = "Duplicate module detected."


class ModuleDependencyError(ModuleError):
    """Raised when module dependencies are missing or cyclic."""

    detail: str = "Invalid module dependency graph."


class InvalidModuleCallableError(ModuleError):
    """Raised when a configured lifecycle callable cannot be resolved."""

    detail: str = "Invalid module callable reference."


class ModuleLifecycleError(ModuleError):
    """Raised when a lifecycle hook fails during module initialization."""

    detail: str = "Module lifecycle execution failed."
