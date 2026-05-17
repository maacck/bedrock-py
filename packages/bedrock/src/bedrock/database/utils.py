"""Utilities for resolving SQLAlchemy model classes by name or table."""

from sqlalchemy.orm import DeclarativeBase


def _get_class_registry(class_: type[DeclarativeBase]) -> dict[str, type]:
    return class_.registry._class_registry


def get_model_by_class_name(class_name: str) -> type[DeclarativeBase] | None:
    """Look up a model class by its Python class name.

    Args:
        class_name: The ``__name__`` of the desired model class.

    Returns:
        The matching model class, or ``None`` if no model is registered
        under that name.
    """
    from bedrock.database.base import BedrockModel

    class_registry = _get_class_registry(BedrockModel)
    return class_registry.get(class_name, None)


def get_class_by_table(tablename: str) -> type[DeclarativeBase] | None:
    """Look up a model class by its ``__tablename__``.

    Args:
        tablename: The database table name to search for.

    Returns:
        The model class whose ``__tablename__`` matches, or ``None``.
    """
    from bedrock.database.base import BedrockModel

    for c in _get_class_registry(BedrockModel).values():
        if hasattr(c, "__tablename__") and c.__tablename__ == tablename:
            return c
    return None
