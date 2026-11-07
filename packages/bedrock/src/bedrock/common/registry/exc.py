"""Errors raised by typed class registration and lazy resolution."""

from ...exc import BedrockExc


class ClassRegistryError(BedrockExc):
    """Base error for class registration and lookup failures."""

    detail: str = "Class registry operation failed."


class DuplicateRegistrationError(ClassRegistryError):
    """Raised when a class name is already registered."""

    detail: str = "A class is already registered under this name."


class ClassNotRegisteredError(ClassRegistryError):
    """Raised when a requested name has not been registered."""

    detail: str = "No class is registered under this name."


class InvalidClassPathError(ClassRegistryError):
    """Raised when a lazy class path is not module:qualified.attribute."""

    detail: str = "Class paths must use the module:qualified.attribute format."


class ClassLoadError(ClassRegistryError):
    """Raised when importing or locating a registered class fails."""

    detail: str = "The registered class could not be loaded."


class InvalidRegisteredClassError(ClassRegistryError):
    """Raised when a value does not satisfy the nominal class contract."""

    detail: str = "The registered value must be a concrete, explicit subclass of the registry's base class."
