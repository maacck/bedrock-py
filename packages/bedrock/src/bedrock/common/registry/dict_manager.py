"""Dict-based registry manager for dynamic class loading and lookup."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable
from typing import Any

logger: logging.Logger = logging.getLogger(__name__)


class DictManager:
    """A dictionary-backed manager for storing and retrieving class paths.

    This class provides a simple interface for registering named class
    identifiers (in ``module.path:ClassName`` format) and dynamically
    importing them on demand.

    Attributes:
        instances: Internal dictionary mapping names to fully-qualified
            class path strings.
    """

    def __init__(self, instances: dict[str, str] | None = None) -> None:
        """Initialize the DictManager.

        Args:
            instances: Optional initial dictionary of name-to-class-path
                mappings. Defaults to an empty dictionary.
        """
        self.instances: dict[str, str] = instances or {}

    def add(self, name: str, class_path: str) -> None:
        """Register a class path under a given name.

        The entry is only added if the name does not already exist,
        preventing accidental overwrites.

        Args:
            name: Unique identifier for the registered class.
            class_path: Fully-qualified class path in ``module.path:ClassName``
                format.
        """
        if name not in self.instances:
            self.instances[name] = class_path

    def get(self, name: str) -> type[Any] | None:
        """Retrieve and dynamically import a class by its registered name.

        Args:
            name: The registered identifier for the class.

        Returns:
            The imported class object, or ``None`` if the import fails.

        Raises:
            KeyError: If the name is not found in the registry.
        """
        cls_path: str = self.instances[name]
        module_name, class_name = cls_path.rsplit(":", maxsplit=1)

        try:
            module = importlib.import_module(module_name)
            cls: type[Any] = getattr(module, class_name)
            return cls
        except Exception as e:
            logger.exception("Unable to import %s. Reason: %s", cls_path, e)
            return None

    def remove(self, name: str) -> None:
        """Remove an entry from the registry.

        Args:
            name: The registered identifier to remove.

        Raises:
            KeyError: If the name is not found in the registry.
        """
        del self.instances[name]

    def has(self, name: str) -> bool:
        """Check whether a name exists in the registry.

        Args:
            name: The registered identifier to look up.

        Returns:
            ``True`` if the name is registered, ``False`` otherwise.
        """
        return name in self.instances

    def update(self, instances: dict[str, str]) -> None:
        """Merge new entries into the registry.

        Args:
            instances: Dictionary of name-to-class-path mappings to add.
                Existing keys will be overwritten.
        """
        self.instances.update(instances)

    def all(self) -> Iterable[str, str]:
        """Return all registered name-to-path mappings.

        Returns:
            A view of all key-value pairs in the registry.
        """
        return self.instances.items()

    def keys(self) -> list[str]:
        """Return all registered names

        Returns:
            A list of all registered names in the registry.
        """
        return list(self.instances.keys())
