"""Strongly typed AST and pagination result entities for JSON Schema queries."""

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ConfigDict, Field, SerializerFunctionWrapHandler, StrictInt, model_serializer, model_validator
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from ...entities import BedrockEntity

type CanonicalOp = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "between",
    "is_null",
    "is_not_null",
    "contains",
    "icontains",
    "starts_with",
    "ends_with",
    "like",
    "ilike",
]
type CollectionQuantifier = Literal["some", "none", "every"]
type ScalarQuantifier = Literal["has", "missing"]
type RelationQuantifier = Literal["some", "none", "every", "has", "missing"]
type SortDirection = Literal["asc", "desc"]
type NullsOrder = Literal["first", "last"]
type FieldExpr = str | ColumnElement[Any] | InstrumentedAttribute[Any]
type GroupByExpr = str | ColumnElement[Any] | InstrumentedAttribute[Any]

UNARY_OPS: frozenset[str] = frozenset({"is_null", "is_not_null"})


class _StrictQueryEntity(BedrockEntity):
    """Base entity for query AST nodes that forbids unknown keys."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class FieldFilter(_StrictQueryEntity):
    """Leaf predicate targeting a model field, extra field, or to-one dotted path."""

    field: str = Field(min_length=1)
    op: CanonicalOp
    value: Any = None

    @model_validator(mode="after")
    def _validate_unary_value(self) -> "FieldFilter":
        if self.op in UNARY_OPS and self.value is not None:
            raise ValueError(f"Operator `{self.op}` does not accept a `value`.")
        return self

    @model_serializer(mode="wrap")
    def _serialize_field_filter(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.op in UNARY_OPS:
            data.pop("value", None)
        return data


class LogicalAndFilter(_StrictQueryEntity):
    """Logical conjunction combining one or more child filter nodes."""

    logic: Literal["and"] = "and"
    conditions: list["FilterNode"] = Field(min_length=1)


class LogicalOrFilter(_StrictQueryEntity):
    """Logical disjunction combining one or more child filter nodes."""

    logic: Literal["or"] = "or"
    conditions: list["FilterNode"] = Field(min_length=1)


class LogicalNotFilter(_StrictQueryEntity):
    """Logical negation wrapping a single child filter node."""

    logic: Literal["not"] = "not"
    condition: "FilterNode"


class RelationFilter(_StrictQueryEntity):
    """Relationship existence/quantifier predicate compiled to EXISTS subqueries."""

    relation: str = Field(min_length=1)
    quantifier: RelationQuantifier
    filter: "FilterNode | None" = None


type FilterNode = LogicalAndFilter | LogicalOrFilter | LogicalNotFilter | RelationFilter | FieldFilter


class SortSpec(_StrictQueryEntity):
    """Single sort directive within a multi-field ordering list."""

    field: str = Field(min_length=1)
    direction: SortDirection = "asc"
    nulls: NullsOrder | None = None


class PaginationSpec(_StrictQueryEntity):
    """1-based page pagination specification."""

    page: StrictInt = Field(default=1, ge=1)
    limit: StrictInt = Field(default=10, ge=1)


class QuerySpec(_StrictQueryEntity):
    """Validated top-level declarative query specification."""

    filter: FilterNode | None = None
    sort: list[SortSpec] = Field(default_factory=list)
    search: str | None = None
    pagination: PaginationSpec | None = None


type QueryInput = str | bytes | Mapping[str, Any] | QuerySpec | None


class PageInfo(BedrockEntity):
    """Pagination metadata returned alongside paged query items."""

    total: int
    page: int
    limit: int
    offset: int
    has_more: bool


class PageResult[M](BedrockEntity):
    """Typed container returned by paginated query executors."""

    items: list[M]
    total: int
    page_info: PageInfo


LogicalAndFilter.model_rebuild()
LogicalOrFilter.model_rebuild()
LogicalNotFilter.model_rebuild()
RelationFilter.model_rebuild()
QuerySpec.model_rebuild()
