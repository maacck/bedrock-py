"""Pure SQLAlchemy 2.0 clause compiler (WHERE / EXISTS / ORDER BY, zero auto_join)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, not_, or_, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ...exc import BadFilterFormatError
from .ast import (
    FilterNode,
    LogicalAndFilter,
    LogicalNotFilter,
    LogicalOrFilter,
    QuerySpec,
    RelationFilter,
)
from .policy import ResolvedField, ResolvedModelPolicy, resolve_model_policy


def escape_like(value: str) -> str:
    """Escape ``\\``, ``%``, and ``_`` so they match literally in SQL LIKE patterns."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_datetime(value: datetime, *, timezone_aware: bool) -> datetime:
    """Align a validated ``datetime`` with the target column's ``timezone`` flag."""
    if timezone_aware:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _normalize_scalar_value(field: ResolvedField, value: Any) -> Any:
    """Coerce a validated scalar value to match SQLAlchemy column binding expectations."""
    if field.scalar_kind == "datetime" and isinstance(value, datetime):
        return _normalize_datetime(value, timezone_aware=field.datetime_timezone)
    if field.scalar_kind == "enum" and field.enum_type is not None and not isinstance(value, field.enum_type):
        return field.enum_type(value)
    if field.scalar_kind == "uuid":
        if field.uuid_as_uuid:
            return value if isinstance(value, UUID) else UUID(str(value))
        return str(value) if isinstance(value, UUID) else value
    return value


def _compile_leaf_predicate(field: ResolvedField, op: str, value: Any) -> ColumnElement[bool]:
    """Compile one validated leaf predicate against *field*'s SQLAlchemy expression."""
    col = field.expression

    if op == "is_null":
        return col.is_(None)
    if op == "is_not_null":
        return col.is_not(None)

    if op == "in":
        items = [_normalize_scalar_value(field, item) for item in value]
        return col.in_(items)

    if op == "between":
        lo = _normalize_scalar_value(field, value[0])
        hi = _normalize_scalar_value(field, value[1])
        return col.between(lo, hi)

    bound = _normalize_scalar_value(field, value)
    if op == "eq":
        return col == bound
    if op == "ne":
        return col != bound
    if op == "gt":
        return col > bound
    if op == "gte":
        return col >= bound
    if op == "lt":
        return col < bound
    if op == "lte":
        return col <= bound
    if op == "contains":
        return col.like(f"%{escape_like(bound)}%", escape="\\")
    if op == "icontains":
        return col.ilike(f"%{escape_like(bound)}%", escape="\\")
    if op == "starts_with":
        return col.like(f"{escape_like(bound)}%", escape="\\")
    if op == "ends_with":
        return col.like(f"%{escape_like(bound)}", escape="\\")
    if op == "like":
        return col.like(bound)
    if op == "ilike":
        return col.ilike(bound)

    raise BadFilterFormatError(f"Unsupported filter operator `{op}`.")


def compile_filter_node(node: FilterNode, resolved: ResolvedModelPolicy) -> ColumnElement[bool]:
    """Translate a validated :class:`FilterNode` AST into a pure SQLAlchemy ``WHERE`` clause.

    All relationship filters (both explicit :class:`RelationFilter` nodes and
    to-one dotted path syntax sugar) compile to correlated ``EXISTS``
    subqueries via ``.any()`` / ``.has()`` so the caller's ``FROM`` / ``JOIN``
    topology is never modified.

    Args:
        node: Validated filter AST node.
        resolved: Resolved policy for the target SQLAlchemy model.

    Returns:
        A SQLAlchemy boolean ``ColumnElement``.
    """
    if isinstance(node, LogicalAndFilter):
        return and_(*[compile_filter_node(child, resolved) for child in node.conditions])
    if isinstance(node, LogicalOrFilter):
        return or_(*[compile_filter_node(child, resolved) for child in node.conditions])
    if isinstance(node, LogicalNotFilter):
        return not_(compile_filter_node(node.condition, resolved))

    if isinstance(node, RelationFilter):
        rel = resolved.relations.get(node.relation)
        if rel is None:
            raise BadFilterFormatError(f"Relation `{node.relation}` is not allowed on {resolved.model.__name__}.")
        sub_clause: ColumnElement[bool] | None = None
        if node.filter is not None:
            child_resolved = resolve_model_policy(rel.target_model, _collect_dotted=False)
            sub_clause = compile_filter_node(node.filter, child_resolved)

        if rel.uselist:
            if node.quantifier == "some":
                return rel.attribute.any(sub_clause) if sub_clause is not None else rel.attribute.any()
            if node.quantifier == "none":
                return not_(rel.attribute.any(sub_clause) if sub_clause is not None else rel.attribute.any())
            if node.quantifier == "every":
                if sub_clause is None:
                    raise BadFilterFormatError("Quantifier `every` requires a child `filter`.")
                return not_(rel.attribute.any(sub_clause.is_not(True)))
            raise BadFilterFormatError(
                f"Quantifier `{node.quantifier}` is not valid for collection relation `{node.relation}`."
            )

        if node.quantifier == "has":
            return rel.attribute.has(sub_clause) if sub_clause is not None else rel.attribute.has()
        if node.quantifier == "missing":
            return not_(rel.attribute.has(sub_clause) if sub_clause is not None else rel.attribute.has())
        raise BadFilterFormatError(
            f"Quantifier `{node.quantifier}` is not valid for scalar relation `{node.relation}`."
        )

    # FieldFilter: direct field or to-one dotted path
    direct = resolved.fields.get(node.field)
    if direct is not None and direct.filterable:
        return _compile_leaf_predicate(direct, node.op, node.value)

    dotted = resolved.dotted_fields.get(node.field)
    if dotted is not None:
        clause = _compile_leaf_predicate(dotted.target_field, node.op, node.value)
        for hop in reversed(dotted.hops):
            clause = hop.attribute.has(clause)
        return clause

    raise BadFilterFormatError(f"Field `{node.field}` is not allowed on {resolved.model.__name__}.")


def compile_where(spec: QuerySpec, resolved: ResolvedModelPolicy) -> ColumnElement[bool]:
    """Compile *spec*'s ``filter`` and ``search`` into a single SQLAlchemy ``WHERE`` clause."""
    clauses: list[ColumnElement[bool]] = []
    if spec.filter is not None:
        clauses.append(compile_filter_node(spec.filter, resolved))
    if spec.search:
        text_search_fn = getattr(resolved.model, "text_search", None)
        if callable(text_search_fn):
            clauses.append(text_search_fn(spec.search))
    if not clauses:
        return true()
    if len(clauses) == 1:
        return clauses[0]
    return and_(*clauses)


_MYSQL_DIALECTS: frozenset[str] = frozenset({"mysql", "mariadb"})


def session_dialect_name(session: Session | AsyncSession, *, mapper: Any = None) -> str | None:
    """Extract the SQLAlchemy dialect name from a sync or async session if bound."""
    sync_session = session.sync_session if isinstance(session, AsyncSession) else session
    try:
        bind = sync_session.get_bind(mapper=mapper) if mapper is not None else sync_session.get_bind()
    except Exception:
        try:
            bind = sync_session.get_bind()
        except Exception:
            return None
    return getattr(getattr(bind, "dialect", None), "name", None)


def compile_order_item(
    expr: ColumnElement[Any],
    *,
    direction: str,
    nulls: str | None,
    dialect_name: str | None = None,
) -> list[ColumnElement[Any]]:
    """Compile one sort expression with dialect-portable ``NULLS FIRST`` / ``NULLS LAST``.

    MySQL and MariaDB do not support ``ORDER BY ... NULLS FIRST/LAST`` syntax,
    so explicit null positioning is emulated via a leading ``IS NOT NULL`` /
    ``IS NULL`` boolean expression on those dialects.
    """
    ordered = expr.desc() if direction == "desc" else expr.asc()
    if nulls is not None and dialect_name in _MYSQL_DIALECTS:
        null_flag = expr.is_not(None).asc() if nulls == "first" else expr.is_(None).asc()
        return [null_flag, ordered]
    if nulls == "first":
        ordered = ordered.nulls_first()
    elif nulls == "last":
        ordered = ordered.nulls_last()
    return [ordered]


def compile_order_by(
    spec: QuerySpec,
    resolved: ResolvedModelPolicy,
    *,
    dialect_name: str | None = None,
) -> list[ColumnElement[Any]]:
    """Compile *spec*'s multi-field ``sort`` directives into SQLAlchemy ``ORDER BY`` clauses."""
    clauses: list[ColumnElement[Any]] = []
    for item in spec.sort:
        expr = resolved.sortable_expressions.get(item.field)
        if expr is None:
            raise BadFilterFormatError(f"Field `{item.field}` is not allowed on {resolved.model.__name__}.")
        clauses.extend(
            compile_order_item(
                expr,
                direction=item.direction,
                nulls=item.nulls,
                dialect_name=dialect_name,
            )
        )
    return clauses
