"""Registry module for dynamic class registration and lookup.

This package provides utilities for registering, storing, and dynamically
importing classes by name.
"""

from .class_registry import ClassRegistry
from .dict_manager import DictManager

__all__ = [
    "ClassRegistry",
    "DictManager",
]
