"""Declarative query policies and SQLAlchemy model metadata resolution."""

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    Time,
    Uuid,
    inspect,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.ext.associationproxy import AssociationProxyExtensionType
from sqlalchemy.ext.hybrid import HybridExtensionType
from sqlalchemy.orm import InstrumentedAttribute, Mapper, RelationshipProperty
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.type_api import TypeDecorator, TypeEngine

from ...exc import BadFilterFormatError
from .ast import UNARY_OPS

DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_NODES = 100
DEFAULT_MAX_RELATION_DEPTH = 2
DEFAULT_MAX_IN_ITEMS = 200
DEFAULT_MAX_LIMIT = 1000
DEFAULT_LIMIT = 10

ScalarKind = Literal["bool", "int", "float", "decimal", "str", "datetime", "date", "time", "uuid", "enum"]

NUMERIC_OPS: frozenset[str] = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in", "between"})
STRING_SAFE_OPS: frozenset[str] = frozenset({"eq", "ne", "in", "contains", "icontains", "starts_with", "ends_with"})
STRING_RAW_LIKE_OPS: frozenset[str] = frozenset({"like", "ilike"})
BOOLEAN_OPS: frozenset[str] = frozenset({"eq", "ne"})
TEMPORAL_OPS: frozenset[str] = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in", "between"})
ENUM_OPS: frozenset[str] = frozenset({"eq", "ne", "in"})
UUID_OPS: frozenset[str] = frozenset({"eq", "ne", "in"})
ALL_CANONICAL_OPS: frozenset[str] = NUMERIC_OPS | STRING_SAFE_OPS | STRING_RAW_LIKE_OPS | UNARY_OPS


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    """Fine-grained query policy for a single model field or extra expression.

    Attributes:
        filterable: Whether the field may appear in ``filter`` clauses.
        sortable: Whether the field may appear in ``sort`` clauses.
        operators: Explicit override of allowed canonical operators. When
            ``None``, operators are inferred from the column's SQL type.
        nullable: Explicit override for whether ``is_null`` / ``is_not_null``
            are allowed. When ``None``, inferred from ``column.nullable``.
        value_type: Explicit Python scalar type (for hybrid properties or custom
            ``TypeDecorator`` columns whose SQL type cannot be inferred).
        column: Explicit SQLAlchemy column or expression used for
            ``QueryPolicy.extra_fields``.
        allow_raw_like: When ``True`` on string columns, enables raw ``like``
            and ``ilike`` operators where the client controls wildcards.
    """

    filterable: bool = True
    sortable: bool = True
    operators: frozenset[str] | None = None
    nullable: bool | None = None
    value_type: Any = None
    column: ColumnElement[Any] | None = None
    allow_raw_like: bool = False


_UNSET: Any = object()


@dataclass(frozen=True, slots=True, init=False)
class QueryPolicy:
    """Model-level or call-site policy controlling schema generation and safety budgets.

    Attributes:
        fields: Per-field overrides keyed by model attribute name.
        relations: Explicit set of allowed relationship names. When ``None``,
            relationships listed in ``__filterable_fields__`` are used.
        extra_fields: Additional virtual/joined/computed fields exposed to
            ``QuerySchema`` (typically paired with ``search_select`` /
            ``paginate_select``).
        sort_expressions: Explicit SQLAlchemy sort expressions keyed by sort
            field name (e.g. scalar subqueries or aggregate expressions).
        allow_dotted_paths: When ``True``, exposes to-one relationship paths
            such as ``"owner.name"`` as syntax sugar compiled to ``has()``.
        max_depth: Maximum nesting depth of the filter AST.
        max_nodes: Maximum total node count across a filter AST.
        max_relation_depth: Maximum relationship hop depth (shared by explicit
            ``relation`` nodes and to-one dotted paths).
        max_in_items: Maximum number of elements allowed in an ``in`` list.
        max_limit: Maximum allowed ``pagination.limit``.
        default_limit: Default ``pagination.limit`` when omitted.
        reveal_allowed_fields: When ``False`` (default, SEC-01), hidden and
            missing fields raise indistinguishable errors. Set ``True`` for
            internal tools or agent self-correction loops.
    """

    fields: dict[str, FieldPolicy]
    relations: frozenset[str] | None
    extra_fields: dict[str, FieldPolicy]
    sort_expressions: dict[str, ColumnElement[Any]]
    allow_dotted_paths: bool
    max_depth: int
    max_nodes: int
    max_relation_depth: int
    max_in_items: int
    max_limit: int
    default_limit: int
    reveal_allowed_fields: bool
    _provided: frozenset[str] = dc_field(init=False, repr=False, compare=False)

    def __init__(
        self,
        fields: dict[str, FieldPolicy] | Any = _UNSET,
        relations: frozenset[str] | None | Any = _UNSET,
        extra_fields: dict[str, FieldPolicy] | Any = _UNSET,
        sort_expressions: dict[str, ColumnElement[Any]] | Any = _UNSET,
        allow_dotted_paths: bool | Any = _UNSET,
        max_depth: int | Any = _UNSET,
        max_nodes: int | Any = _UNSET,
        max_relation_depth: int | Any = _UNSET,
        max_in_items: int | Any = _UNSET,
        max_limit: int | Any = _UNSET,
        default_limit: int | Any = _UNSET,
        reveal_allowed_fields: bool | Any = _UNSET,
    ) -> None:
        provided: set[str] = set()
        raw_args: dict[str, tuple[Any, Any]] = {
            "fields": (fields, {}),
            "relations": (relations, None),
            "extra_fields": (extra_fields, {}),
            "sort_expressions": (sort_expressions, {}),
            "allow_dotted_paths": (allow_dotted_paths, True),
            "max_depth": (max_depth, DEFAULT_MAX_DEPTH),
            "max_nodes": (max_nodes, DEFAULT_MAX_NODES),
            "max_relation_depth": (max_relation_depth, DEFAULT_MAX_RELATION_DEPTH),
            "max_in_items": (max_in_items, DEFAULT_MAX_IN_ITEMS),
            "max_limit": (max_limit, DEFAULT_MAX_LIMIT),
            "default_limit": (default_limit, DEFAULT_LIMIT),
            "reveal_allowed_fields": (reveal_allowed_fields, False),
        }
        for attr, (val, default) in raw_args.items():
            if val is _UNSET:
                object.__setattr__(self, attr, default)
            else:
                provided.add(attr)
                object.__setattr__(self, attr, val)
        object.__setattr__(self, "_provided", frozenset(provided))


@dataclass(frozen=True, slots=True)
class ResolvedField:
    """Resolved scalar field metadata used for schema generation and SQL compilation."""

    name: str
    expression: Any
    scalar_kind: ScalarKind
    operators: frozenset[str]
    nullable: bool
    filterable: bool
    sortable: bool
    projectable: bool = True
    datetime_timezone: bool = False
    enum_values: tuple[Any, ...] | None = None
    enum_type: type[enum.Enum] | None = None


@dataclass(frozen=True, slots=True)
class ResolvedRelation:
    """Resolved allowlisted relationship metadata."""

    name: str
    attribute: InstrumentedAttribute[Any]
    target_model: type[Any]
    uselist: bool


@dataclass(frozen=True, slots=True)
class ResolvedDottedField:
    """Resolved to-one dotted path (e.g. ``owner.name``) compiled via ``has()``."""

    path: str
    hops: tuple[ResolvedRelation, ...]
    target_field: ResolvedField
    max_in_items: int = DEFAULT_MAX_IN_ITEMS


@dataclass(frozen=True, slots=True)
class ResolvedModelPolicy:
    """Complete introspected metadata and merged policy for one SQLAlchemy model."""

    model: type[Any]
    policy: QueryPolicy
    primary_keys: frozenset[str]
    primary_key_order: tuple[str, ...]
    fields: dict[str, ResolvedField]
    dotted_fields: dict[str, ResolvedDottedField]
    relations: dict[str, ResolvedRelation]
    sortable_expressions: dict[str, Any]


def primary_key_order(model: type[Any]) -> tuple[str, ...]:
    """Return the mapped attribute names of *model*'s primary-key columns in declared PK order."""
    mapper: Mapper[Any] = inspect(model)
    return tuple(mapper.get_property_by_column(col).key for col in mapper.primary_key)


def primary_key_names(model: type[Any]) -> frozenset[str]:
    """Return the mapped attribute names of *model*'s primary-key columns."""
    return frozenset(primary_key_order(model))


def _merge_policies(base: QueryPolicy | None, override: QueryPolicy | None) -> QueryPolicy:
    """Merge an optional model-level policy with an optional call-site override."""
    if base is None and override is None:
        return QueryPolicy()
    if base is None:
        assert override is not None
        return override
    if override is None:
        return base

    merged_fields = {**base.fields, **override.fields}
    merged_extra = {**base.extra_fields, **override.extra_fields}
    merged_sort = {**base.sort_expressions, **override.sort_expressions}

    def _pick(attr: str) -> Any:
        return getattr(override, attr) if attr in override._provided else getattr(base, attr)

    merged = QueryPolicy(
        fields=merged_fields,
        relations=_pick("relations"),
        extra_fields=merged_extra,
        sort_expressions=merged_sort,
        allow_dotted_paths=_pick("allow_dotted_paths"),
        max_depth=_pick("max_depth"),
        max_nodes=_pick("max_nodes"),
        max_relation_depth=_pick("max_relation_depth"),
        max_in_items=_pick("max_in_items"),
        max_limit=_pick("max_limit"),
        default_limit=_pick("default_limit"),
        reveal_allowed_fields=_pick("reveal_allowed_fields"),
    )
    object.__setattr__(merged, "_provided", base._provided | override._provided)
    return merged


def _unwrap_type_engine(sql_type: TypeEngine[Any]) -> TypeEngine[Any]:
    """Unwrap ``TypeDecorator`` layers to inspect the underlying SQLAlchemy type."""
    seen: set[int] = set()
    current: Any = sql_type
    while isinstance(current, TypeDecorator) and id(current) not in seen:
        seen.add(id(current))
        impl = getattr(current, "impl", None)
        if impl is None:
            break
        current = impl() if isinstance(impl, type) else impl
    return current


def _infer_from_python_type(
    py_type: Any,
    *,
    allow_raw_like: bool,
) -> tuple[ScalarKind, frozenset[str], bool, tuple[Any, ...] | None, type[enum.Enum] | None]:
    """Infer scalar metadata from an explicit Python type annotation."""
    if isinstance(py_type, type) and issubclass(py_type, enum.Enum):
        values = tuple(member.value for member in py_type)
        return ("enum", ENUM_OPS, False, values, py_type)
    if py_type is bool:
        return ("bool", BOOLEAN_OPS, False, None, None)
    if py_type is int:
        return ("int", NUMERIC_OPS, False, None, None)
    if py_type is float:
        return ("float", NUMERIC_OPS, False, None, None)
    if py_type is Decimal:
        return ("decimal", NUMERIC_OPS, False, None, None)
    if py_type is datetime:
        return ("datetime", TEMPORAL_OPS, False, None, None)
    if py_type is date:
        return ("date", TEMPORAL_OPS, False, None, None)
    if py_type is time:
        return ("time", TEMPORAL_OPS, False, None, None)
    if py_type is UUID:
        return ("uuid", UUID_OPS, False, None, None)
    if py_type is str:
        ops = STRING_SAFE_OPS | (STRING_RAW_LIKE_OPS if allow_raw_like else frozenset())
        return ("str", ops, False, None, None)
    raise BadFilterFormatError(f"Unsupported explicit field value_type `{py_type!r}`.")


def _infer_from_sql_type(
    sql_type: TypeEngine[Any],
    *,
    allow_raw_like: bool,
) -> tuple[ScalarKind, frozenset[str], bool, tuple[Any, ...] | None, type[enum.Enum] | None]:
    """Infer scalar metadata and default operators from a SQLAlchemy ``TypeEngine``."""
    unwrapped = _unwrap_type_engine(sql_type)

    if isinstance(unwrapped, SAEnum):
        enum_cls = unwrapped.enum_class
        if isinstance(enum_cls, type) and issubclass(enum_cls, enum.Enum):
            values = tuple(member.value for member in enum_cls)
            return ("enum", ENUM_OPS, False, values, enum_cls)
        if unwrapped.enums:
            return ("enum", ENUM_OPS, False, tuple(unwrapped.enums), None)
        return ("str", STRING_SAFE_OPS | (STRING_RAW_LIKE_OPS if allow_raw_like else frozenset()), False, None, None)

    if isinstance(unwrapped, Boolean):
        return ("bool", BOOLEAN_OPS, False, None, None)
    if isinstance(unwrapped, Integer):
        return ("int", NUMERIC_OPS, False, None, None)
    if isinstance(unwrapped, Float):
        return ("float", NUMERIC_OPS, False, None, None)
    if isinstance(unwrapped, Numeric):
        kind: ScalarKind = "decimal" if getattr(unwrapped, "asdecimal", True) else "float"
        return (kind, NUMERIC_OPS, False, None, None)
    if isinstance(unwrapped, DateTime):
        return ("datetime", TEMPORAL_OPS, bool(getattr(unwrapped, "timezone", False)), None, None)
    if isinstance(unwrapped, Date):
        return ("date", TEMPORAL_OPS, False, None, None)
    if isinstance(unwrapped, Time):
        return ("time", TEMPORAL_OPS, False, None, None)
    if isinstance(unwrapped, Uuid):
        return ("uuid", UUID_OPS, False, None, None)
    if isinstance(unwrapped, String):
        ops = STRING_SAFE_OPS | (STRING_RAW_LIKE_OPS if allow_raw_like else frozenset())
        return ("str", ops, False, None, None)

    # Try dialect-agnostic python_type fallback for custom types (e.g. Postgres UUID)
    try:
        py_type = unwrapped.python_type
    except NotImplementedError as err:
        raise BadFilterFormatError(
            f"Cannot infer query type for SQL type `{sql_type!r}`; declare `FieldPolicy(value_type=...)`."
        ) from err
    return _infer_from_python_type(py_type, allow_raw_like=allow_raw_like)


def _validate_policy_budgets(policy: QueryPolicy) -> None:
    """Reject nonsensical policy budgets at schema compile time."""
    if policy.max_depth < 1:
        raise BadFilterFormatError("`max_depth` must be >= 1.")
    if policy.max_nodes < 1:
        raise BadFilterFormatError("`max_nodes` must be >= 1.")
    if policy.max_relation_depth < 0:
        raise BadFilterFormatError("`max_relation_depth` must be >= 0.")
    if policy.max_in_items < 1:
        raise BadFilterFormatError("`max_in_items` must be >= 1.")
    if policy.max_limit < 1 or policy.default_limit < 1 or policy.default_limit > policy.max_limit:
        raise BadFilterFormatError("`default_limit` and `max_limit` must satisfy 1 <= default_limit <= max_limit.")


def _validate_operator_override(field_name: str, operators: frozenset[str]) -> frozenset[str]:
    """Ensure every explicitly configured operator is a valid canonical operator."""
    invalid = operators - ALL_CANONICAL_OPS
    if invalid:
        raise BadFilterFormatError(
            f"Invalid operator override(s) {sorted(invalid)} configured for field `{field_name}`."
        )
    return operators


def _resolve_scalar_field(
    *,
    model: type[Any],
    mapper: Mapper[Any],
    name: str,
    field_policy: FieldPolicy,
    is_extra: bool = False,
) -> ResolvedField:
    """Resolve one scalar column, hybrid property, or extra SQL expression."""
    expression: Any
    inferred_nullable = True
    sql_type: TypeEngine[Any] | None = None
    is_assoc_proxy = False

    if is_extra:
        if field_policy.column is None:
            raise BadFilterFormatError(f"Extra field `{name}` must specify `FieldPolicy(column=...)`.")
        expression = field_policy.column
        sql_type = getattr(expression, "type", None)
        inferred_nullable = bool(getattr(expression, "nullable", True))
    else:
        if name in mapper.column_attrs:
            attr = getattr(model, name)
            col = mapper.column_attrs[name].columns[0]
            expression = attr
            sql_type = getattr(col, "type", None)
            inferred_nullable = bool(getattr(col, "nullable", True))
        elif name in mapper.all_orm_descriptors:
            descriptor = mapper.all_orm_descriptors[name]
            ext = getattr(descriptor, "extension_type", None)
            if ext not in (HybridExtensionType.HYBRID_PROPERTY, AssociationProxyExtensionType.ASSOCIATION_PROXY):
                raise BadFilterFormatError(
                    f"Attribute `{name}` on {model.__name__} is not a supported filterable field."
                )
            is_assoc_proxy = ext == AssociationProxyExtensionType.ASSOCIATION_PROXY
            expression = getattr(model, name)
            sql_type = getattr(expression, "type", None)
            inferred_nullable = True
        else:
            raise BadFilterFormatError(f"Model {model.__name__} has no column or attribute `{name}`.")

    if field_policy.value_type is not None:
        scalar_kind, default_ops, dt_tz, enum_vals, enum_cls = _infer_from_python_type(
            field_policy.value_type, allow_raw_like=field_policy.allow_raw_like
        )
        if sql_type is not None:
            unwrapped = _unwrap_type_engine(sql_type)
            if isinstance(unwrapped, DateTime):
                dt_tz = bool(getattr(unwrapped, "timezone", False))
    elif sql_type is not None:
        scalar_kind, default_ops, dt_tz, enum_vals, enum_cls = _infer_from_sql_type(
            sql_type, allow_raw_like=field_policy.allow_raw_like
        )
    else:
        raise BadFilterFormatError(
            f"Cannot determine SQL type for `{name}` on {model.__name__}. "
            "Provide `FieldPolicy(value_type=...)` in `__query_policy__` or `QueryPolicy`."
        )

    nullable = inferred_nullable if field_policy.nullable is None else field_policy.nullable
    if field_policy.operators is not None:
        ops = _validate_operator_override(name, field_policy.operators)
    else:
        ops = default_ops | (UNARY_OPS if nullable else frozenset())

    return ResolvedField(
        name=name,
        expression=expression,
        scalar_kind=scalar_kind,
        operators=ops,
        nullable=nullable,
        filterable=field_policy.filterable,
        sortable=False if is_assoc_proxy else field_policy.sortable,
        projectable=not is_assoc_proxy,
        datetime_timezone=dt_tz,
        enum_values=enum_vals,
        enum_type=enum_cls,
    )


def _declared_allowlist(model: type[Any]) -> tuple[bool, tuple[str, ...]]:
    """Return ``(is_all, declared_names)`` from ``model.__filterable_fields__``."""
    declared = getattr(model, "__filterable_fields__", ())
    if declared == "__all__":
        return True, ()
    if isinstance(declared, str):
        return False, (declared,)
    if isinstance(declared, Sequence):
        return False, tuple(declared)
    return False, tuple(declared)


def _collect_dotted_fields(
    *,
    resolved: ResolvedModelPolicy,
    remaining_depth: int,
    visited_models: frozenset[type[Any]],
    prefix_hops: tuple[ResolvedRelation, ...],
    prefix_path: str,
    effective_max_in_items: int,
    target_out: dict[str, ResolvedDottedField],
) -> None:
    """Recursively collect allowlisted to-one dotted paths without cycles."""
    if remaining_depth <= 0:
        return
    for rel in resolved.relations.values():
        if rel.uselist:
            continue
        if rel.target_model in visited_models:
            continue
        next_hops = (*prefix_hops, rel)
        next_prefix = f"{prefix_path}.{rel.name}" if prefix_path else rel.name
        child_resolved = resolve_model_policy(rel.target_model, _collect_dotted=False)
        child_max_in = min(effective_max_in_items, child_resolved.policy.max_in_items)
        for field_name, child_field in child_resolved.fields.items():
            if not child_field.filterable:
                continue
            full_path = f"{next_prefix}.{field_name}"
            target_out[full_path] = ResolvedDottedField(
                path=full_path,
                hops=next_hops,
                target_field=child_field,
                max_in_items=child_max_in,
            )
        _collect_dotted_fields(
            resolved=child_resolved,
            remaining_depth=remaining_depth - 1,
            visited_models=visited_models | {rel.target_model},
            prefix_hops=next_hops,
            prefix_path=next_prefix,
            effective_max_in_items=child_max_in,
            target_out=target_out,
        )


def resolve_model_policy(
    model: type[Any],
    policy: QueryPolicy | None = None,
    *,
    _collect_dotted: bool = True,
) -> ResolvedModelPolicy:
    """Introspect *model* and merge its allowlist, ``__query_policy__``, and *policy*.

    Args:
        model: SQLAlchemy ORM model class to introspect.
        policy: Optional call-site :class:`QueryPolicy` override.

    Returns:
        A :class:`ResolvedModelPolicy` describing every allowed scalar field,
        to-one dotted path, relationship, and sortable expression.

    Raises:
        BadFilterFormatError: If a configured field or policy budget is invalid.
    """
    mapper: Mapper[Any] = inspect(model)
    model_policy = getattr(model, "__query_policy__", None)
    if model_policy is not None and not isinstance(model_policy, QueryPolicy):
        raise BadFilterFormatError(f"{model.__name__}.__query_policy__ must be a QueryPolicy instance.")

    merged = _merge_policies(model_policy, policy)
    _validate_policy_budgets(merged)

    pk_order = primary_key_order(model)
    pks = frozenset(pk_order)
    is_all, declared_names = _declared_allowlist(model)

    # Determine allowed scalar attribute names and relationship names.
    # "__all__" expands all mapped columns on this model, NEVER relationships (ADR-4).
    allowed_scalar_names: list[str] = list(pk_order)
    allowed_relation_names: list[str] = []

    if is_all:
        for col_name in mapper.column_attrs.keys():
            if col_name not in allowed_scalar_names:
                allowed_scalar_names.append(col_name)
    else:
        for name in declared_names:
            if name in mapper.relationships:
                if name not in allowed_relation_names:
                    allowed_relation_names.append(name)
            elif name not in allowed_scalar_names:
                allowed_scalar_names.append(name)

    # Fields explicitly listed in merged.fields are also considered allowlisted.
    for name in merged.fields:
        if name in mapper.relationships:
            raise BadFilterFormatError(
                f"Relationship `{name}` on {model.__name__} must be configured in `QueryPolicy.relations`, not `fields`."
            )
        if name not in allowed_scalar_names:
            allowed_scalar_names.append(name)

    if merged.relations is not None:
        allowed_relation_names = list(merged.relations)

    fields: dict[str, ResolvedField] = {}
    for name in allowed_scalar_names:
        fp = merged.fields.get(name, FieldPolicy())
        fields[name] = _resolve_scalar_field(model=model, mapper=mapper, name=name, field_policy=fp)

    for extra_name, extra_fp in merged.extra_fields.items():
        if extra_name in fields:
            raise BadFilterFormatError(f"Extra field `{extra_name}` conflicts with model field on {model.__name__}.")
        fields[extra_name] = _resolve_scalar_field(
            model=model, mapper=mapper, name=extra_name, field_policy=extra_fp, is_extra=True
        )

    relations: dict[str, ResolvedRelation] = {}
    for rel_name in allowed_relation_names:
        if rel_name not in mapper.relationships:
            raise BadFilterFormatError(f"Model {model.__name__} has no relationship `{rel_name}`.")
        rel_prop: RelationshipProperty[Any] = mapper.relationships[rel_name]
        relations[rel_name] = ResolvedRelation(
            name=rel_name,
            attribute=getattr(model, rel_name),
            target_model=rel_prop.mapper.class_,
            uselist=bool(rel_prop.uselist),
        )

    sortable_expressions: dict[str, Any] = {name: f.expression for name, f in fields.items() if f.sortable}
    sortable_expressions.update(merged.sort_expressions)

    base_resolved = ResolvedModelPolicy(
        model=model,
        policy=merged,
        primary_keys=pks,
        primary_key_order=pk_order,
        fields=fields,
        dotted_fields={},
        relations=relations,
        sortable_expressions=sortable_expressions,
    )

    dotted_fields: dict[str, ResolvedDottedField] = {}
    if _collect_dotted and merged.allow_dotted_paths and merged.max_relation_depth > 0:
        _collect_dotted_fields(
            resolved=base_resolved,
            remaining_depth=merged.max_relation_depth,
            visited_models=frozenset({model}),
            prefix_hops=(),
            prefix_path="",
            effective_max_in_items=merged.max_in_items,
            target_out=dotted_fields,
        )

    return ResolvedModelPolicy(
        model=model,
        policy=merged,
        primary_keys=pks,
        primary_key_order=pk_order,
        fields=fields,
        dotted_fields=dotted_fields,
        relations=relations,
        sortable_expressions=sortable_expressions,
    )
