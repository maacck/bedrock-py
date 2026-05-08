"""Class registry for dynamic class registration and lookup."""

from __future__ import annotations

from typing import Any, TypeVar

from .dict_manager import DictManager

T = TypeVar("T", bound=type[Any])


class ClassRegistry[T: type[Any]](DictManager):
    """A specialized registry for registering and retrieving classes.

    Extends :class:`DictManager` with type-aware registration that
    automatically computes the fully-qualified class path from the
    class object itself.
    """

    def register(self, name: str, class_cls: T) -> None:
        """Register a class under a given name.

        The fully-qualified path (``module.path:ClassName``) is derived
        automatically from the class's ``__module__`` and ``__qualname__``
        attributes.

        Args:
            name: Unique identifier for the registered class.
            class_cls: The class object to register.
        """
        class_path = f"{class_cls.__module__}:{class_cls.__qualname__}"
        self.add(name, class_path)

    def get(self, name: str) -> T | None:
        """Retrieve and dynamically import a class by its registered name.

        Args:
            name: The registered identifier for the class.

        Returns:
            The imported class object, or ``None`` if the import fails.

        Raises:
            KeyError: If the name is not found in the registry.
        """
        return super().get(name)
