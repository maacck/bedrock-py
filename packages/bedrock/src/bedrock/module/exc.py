"""Exception types for the Bedrock module runtime."""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class ModuleFailure:
    """One module-scoped failure recorded during a lifecycle operation.

    Attributes:
        module: Import path of the module that failed.
        phase: Lifecycle step that failed, e.g. ``"on_load"``, ``"ready"`` or ``"on_shutdown"``.
        error: The exception raised by that step. For hook failures this is the
            ``ModuleLifecycleError`` wrapper whose ``__cause__`` is the hook's own exception.
    """

    module: str
    phase: str
    error: BaseException

    @property
    def cause(self) -> BaseException:
        """Return the original exception raised by the module's own code."""
        return self.error.__cause__ or self.error


class ModuleLifecycleError(ModuleError):
    """Raised when a module lifecycle operation fails.

    Attributes:
        failures: Every module-scoped failure behind this error, in the order they occurred.
            Empty when the error is not tied to a specific module step.
    """

    detail: str = "Module lifecycle execution failed."

    def __init__(self, msg: str | None = None, *, failures: tuple[ModuleFailure, ...] = ()) -> None:
        """Initialize the error.

        Args:
            msg: Optional detail message.
            failures: Module-scoped failures aggregated by this error.
        """
        super().__init__(msg)
        self.failures = failures

    @property
    def failed_modules(self) -> list[str]:
        """Return the distinct import paths of failed modules in failure order."""
        return list(dict.fromkeys(failure.module for failure in self.failures))


class ModuleStartupRollbackError(ModuleLifecycleError):
    """Raised when a failed installation could not be fully unwound.

    The installation failure itself is the first entry of ``failures`` and the
    ``__cause__`` of this error; the remaining entries are the modules whose
    ``on_shutdown`` hook failed while unwinding. Membership is rolled back
    regardless: no module introduced by the failed operation stays installed.
    """

    detail: str = "Module installation failed and its cleanup was incomplete."
