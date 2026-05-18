"""Declarative base model and CRUD mixin for SQLAlchemy ORM."""

from typing import Any

from sqlalchemy import event, inspect, or_, true
from sqlalchemy.orm import DeclarativeBase, Session, object_session
from sqlalchemy.sql.elements import ColumnElement

from bedrock.exc import BedrockExc


class CrudMixin:
    """Mixin providing common CRUD operations for SQLAlchemy models."""

    @property
    def _object_session(self) -> Session | None:
        return object_session(self)

    def update(self, **kwargs: Any) -> None:
        """Update model attributes from keyword arguments.

        Only attributes that already exist on the instance are updated;
        unknown keys are silently ignored.

        Args:
            **kwargs: Attribute names and their new values.
        """
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def delete(self) -> None:
        """Delete this instance from its owning session.

        Raises:
            BedrockExc: If the instance is not attached to a session.
        """
        if self._object_session is None:
            raise BedrockExc("Cannot delete an object that is not attached to a session.")
        self._object_session.delete(self)


class BedrockModel(DeclarativeBase, CrudMixin):
    """Base declarative model for all Bedrock ORM classes.

    Combines SQLAlchemy's :class:`~sqlalchemy.orm.DeclarativeBase` with
    :class:`CrudMixin` convenience methods and common helpers (primary-key
    access, dict serialisation, human-readable repr).

    Subclasses may set:

    * ``__searchable_columns__`` — list of column names to search via
      :meth:`text_search` (default: empty, meaning no-op).
    * ``__search_op__`` — SQLAlchemy-compatible operator string applied to
      each searchable column (default: ``"ilike"``).  The query term is
      wrapped in ``%…%`` for pattern operators (``ilike``, ``like``).
    """

    __abstract__ = True
    __searchable_columns__: list[str] = []
    __search_op__: str = "ilike"

    @property
    def verbose_name(self) -> str:
        """Return the class name as a human-readable label."""
        return self.__name__

    def dict(self) -> dict[str, Any]:
        """Return a dictionary mapping column names to their current values.

        Returns:
            A ``dict`` keyed by column name with the live attribute values.
        """
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    @property
    def _pks_columns(self) -> list[str]:
        return [col.name for col in self.__table__.primary_key.columns]

    def get_primary_keys(self) -> list[str]:
        """Return the names of the primary-key columns.

        This is the public counterpart of the ``_pks_columns`` property.

        Returns:
            A list of column-name strings that form the primary key.
        """
        return [col.name for col in self.__table__.primary_key.columns]

    @property
    def _id_str(self) -> str:
        """Return a dash-separated string of the identity-pk values.

        Returns ``"None"`` when the instance has not yet been persisted.
        """
        ids = inspect(self).identity
        if ids:
            return "-".join([str(x) for x in ids]) if len(ids) > 1 else str(ids[0])
        else:
            return "None"

    @classmethod
    def text_search(cls, q: str) -> ColumnElement[bool]:
        """Return a SQLAlchemy clause that searches ``__searchable_columns__``.

        Each column is tested with ``__search_op__`` (default ``"ilike"``).
        The term *q* is wrapped in ``%…%`` for pattern operators (``ilike``,
        ``like``).  When ``__searchable_columns__`` is empty a no-op ``TRUE``
        clause is returned.

        Args:
            q: Free-text search term.

        Returns:
            An OR clause across all searchable columns, or ``true()``.
        """
        if not cls.__searchable_columns__:
            return true()

        op_name = cls.__search_op__
        use_wildcards = op_name in ("ilike", "like")
        term = f"%{q}%" if use_wildcards else q

        clauses = []
        for col_name in cls.__searchable_columns__:
            col = getattr(cls, col_name)
            clauses.append(col.op(op_name)(term))

        return or_(*clauses)

    def __repr__(self) -> str:
        id_str = ("#" + self._id_str) if self._id_str else ""
        return f"<{self.__class__.__name__} {id_str}>"


@event.listens_for(BedrockModel, "init", propagate=True)
def set_defaults(target: BedrockModel, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Populate ``kwargs`` with column defaults before the ORM ``__init__`` runs.

    Callable defaults are invoked with *target* as their argument.  Values
    supplied by the caller (present in the original *kwargs*) take precedence
    over column defaults.

    Args:
        target: The model instance being initialised.
        args: Positional arguments forwarded by SQLAlchemy (unused).
        kwargs: Mutable dict of keyword arguments that will be passed to
            ``__init__``.  Modified in-place.
    """
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
