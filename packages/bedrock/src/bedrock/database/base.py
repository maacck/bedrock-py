from sqlalchemy import event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, object_session

from bedrock.exc import BedrockExc


class CrudMixin:
    """Mixin providing common CRUD operations for SQLAlchemy models."""

    @property
    def _object_session(self) -> Session | None:
        return object_session(self)

    def update(self, **kwargs):
        """Update model attributes from keyword arguments."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def delete(self):
        if self._object_session is not None:
            raise BedrockExc("Cannot delete an object that is not attached to a session.")
        self._object_session.delete(self)


class BedrockModel(DeclarativeBase, CrudMixin):
    __abstract__ = True

    @property
    def verbose_name(cls) -> str:
        return cls.__name__

    def dict(self) -> dict:
        """Returns a dict representation of a model."""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    @property
    def _pks_columns(self) -> list[str]:
        return [col.name for col in self.__table__.primary_key.columns]

    @property
    def _id_str(self) -> str:
        ids = inspect(self).identity
        if ids:
            return "-".join([str(x) for x in ids]) if len(ids) > 1 else str(ids[0])
        else:
            return "None"

    def __repr__(self):
        # get id like '#123'
        id_str = ("#" + self._id_str) if self._id_str else ""
        # join class name, id and repr_attrs
        return f"<{self.__class__.__name__} {id_str}>"


@event.listens_for(BedrockModel, "init", propagate=True)
def set_defaults(target, args, kwargs):
    original = kwargs.copy()
    kwargs.clear()
    for key, column in inspect(target.__class__).columns.items():
        if hasattr(column, "default") and column.default is not None:
            if callable(column.default.arg):
                kwargs[key] = column.default.arg(target)
            else:
                kwargs[key] = column.default.arg

    # supersede w/initial in case target uses setters overriding defaults
    kwargs.update(original)
