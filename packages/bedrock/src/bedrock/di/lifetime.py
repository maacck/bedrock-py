"""Service lifetime definitions for the DI container."""

from enum import Enum


class Lifetime(Enum):
    """Defines how instances are managed by the DI container.

    Attributes:
        SINGLETON: Created once and cached for the container's lifetime.
        TRANSIENT: A new instance is created on every ``resolve()`` call.
        SCOPED: One instance per active scope (ContextVar-backed).
    """

    SINGLETON = "singleton"
    TRANSIENT = "transient"
    SCOPED = "scoped"
