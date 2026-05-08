from sqlalchemy.orm import DeclarativeBase


def _get_class_registry(class_: type[DeclarativeBase]):
    try:
        return class_.registry._class_registry
    except AttributeError:  # SQLAlchemy <1.4
        return class_._decl_class_registry


def get_model_by_class_name(class_name: str):
    from bedrock.database.base import BedrockModel

    class_registry = _get_class_registry(BedrockModel)
    return class_registry.get(class_name, None)


def get_class_by_table(tablename: str):
    from bedrock.database.base import BedrockModel

    for c in _get_class_registry(BedrockModel).values():
        if hasattr(c, "__tablename__") and c.__tablename__ == tablename:
            return c
    return None
