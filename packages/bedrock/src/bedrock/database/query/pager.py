"""Extensible pagination strategies (Page, Limit/Offset, Cursor, and custom subclasses)."""

import base64
import binascii
import enum
import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, ClassVar, Literal
from uuid import UUID

import orjson
from pydantic import TypeAdapter, ValidationError
from pydantic_core import CoreSchema, PydanticUndefined, core_schema
from sqlalchemy import Select, and_, false, inspect, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.base import ExecutableOption
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.sqltypes import NullType
from sqlalchemy.sql.visitors import iterate as sql_iterate

from ...exc import BadFilterFormatError, InvalidCursorError, InvalidQueryLimitError
from .ast import (
    BasePageInfo,
    CursorPageInfo,
    CursorParams,
    LimitOffsetPageInfo,
    LimitOffsetParams,
    NullsOrder,
    PageInfo,
    PageResult,
    PaginationParams,
    PaginationSpec,
    QuerySpec,
    SortDirection,
)
from .compiler import _normalize_scalar_value, compile_order_item, session_dialect_name
from .policy import QueryPolicy, ResolvedField, ResolvedModelPolicy, _infer_from_sql_type

_CURSOR_COL_PREFIX = "__bedrock_cursor_"
_AGGREGATE_FUNC_NAMES: frozenset[str] = frozenset(
    {
        "array_agg",
        "avg",
        "bit_and",
        "bit_or",
        "bool_and",
        "bool_or",
        "count",
        "every",
        "group_concat",
        "json_agg",
        "json_object_agg",
        "jsonb_agg",
        "jsonb_object_agg",
        "max",
        "min",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "string_agg",
        "sum",
        "var_pop",
        "var_samp",
        "variance",
    }
)


@dataclass(frozen=True, slots=True)
class PaginationContext[T, ParamsT: PaginationParams]:
    """Execution context handed by :class:`QuerySchema` to a :class:`BasePager`.

    Attributes:
        filtered_stmt: SQLAlchemy ``Select`` with ``WHERE`` (and optional
            ``GROUP BY``) already applied, prior to ``ORDER BY`` and slicing.
        order_clauses: Compiled ``ORDER BY`` expressions from ``spec.sort``.
        spec: Validated :class:`QuerySpec`.
        params: Validated pager-specific pagination parameters.
        resolved: Resolved model policy (columns, primary keys, sort map).
        count_stmt: Optimal ``SELECT count(*)`` statement matching ``filtered_stmt``.
        mode: Execution shape (``"models"``, ``"fields"``, or ``"select"``).
        options: Optional SQLAlchemy loader options (used in ``"models"`` mode).
        has_group_by: Whether the underlying query uses ``GROUP BY``.
    """

    filtered_stmt: Select[Any]
    order_clauses: list[ColumnElement[Any]]
    spec: QuerySpec
    params: ParamsT
    resolved: ResolvedModelPolicy
    count_stmt: Select[Any]
    mode: Literal["models", "fields", "select"]
    options: Sequence[ExecutableOption] | None = None
    has_group_by: bool = False

    def with_options(self, stmt: Select[Any]) -> Select[Any]:
        """Attach configured loader options to *stmt* when present."""
        if self.options:
            return stmt.options(*self.options)
        return stmt

    def fetch_rows(self, session: Session, stmt: Select[Any]) -> list[T]:
        """Execute *stmt* synchronously and decode rows according to ``mode``."""
        exec_stmt = self.with_options(stmt)
        if self.mode == "models":
            return list(session.execute(exec_stmt).unique().scalars().all())
        return [dict(row) for row in session.execute(exec_stmt).mappings().all()]  # type: ignore[misc]

    async def afetch_rows(self, session: AsyncSession, stmt: Select[Any]) -> list[T]:
        """Execute *stmt* asynchronously and decode rows according to ``mode``."""
        exec_stmt = self.with_options(stmt)
        result = await session.execute(exec_stmt)
        if self.mode == "models":
            return list(result.unique().scalars().all())
        return [dict(row) for row in result.mappings().all()]  # type: ignore[misc]

    def fetch_total(self, session: Session) -> int:
        """Execute ``count_stmt`` synchronously and return total row count."""
        return int(session.execute(self.count_stmt).scalar() or 0)

    async def afetch_total(self, session: AsyncSession) -> int:
        """Execute ``count_stmt`` asynchronously and return total row count."""
        return int((await session.execute(self.count_stmt)).scalar() or 0)


def _validate_limit_value(limit: Any, *, max_limit: int) -> None:
    """Validate that a raw ``limit`` value is a non-boolean int within ``[1, max_limit]``."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_limit:
        raise InvalidQueryLimitError(f"`pagination.limit` must be an integer between 1 and {max_limit}.")


class BasePager[ParamsT: PaginationParams, InfoT: BasePageInfo](ABC):
    """Abstract base class for pluggable query pagination strategies.

    Subclasses declare ``params_model`` (a :class:`PaginationParams` subclass)
    and ``page_info_model`` (a :class:`BasePageInfo` subclass), then implement
    :meth:`apply`, :meth:`paginate`, and :meth:`apaginate`.

    The default :meth:`build_params_schema`, :meth:`precheck_raw`, and
    :meth:`parse_params` implementations automatically enforce ``limit`` bounds
    from :class:`QueryPolicy` and emit Draft 2020-12 JSON Schema for every
    field on ``params_model``.
    """

    params_model: ClassVar[type[PaginationParams]] = PaginationParams
    page_info_model: ClassVar[type[BasePageInfo]] = BasePageInfo

    def build_params_schema(self, policy: QueryPolicy) -> CoreSchema:
        """Build a strict ``pydantic_core`` schema for ``query.pagination``."""
        fields: dict[str, core_schema.TypedDictField] = {}
        for name, field_info in self.params_model.model_fields.items():
            if name == "limit":
                fields["limit"] = core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.int_schema(strict=True, ge=1, le=policy.max_limit),
                        default=policy.default_limit,
                    ),
                    required=False,
                )
                continue

            annotation: Any = field_info.annotation if field_info.annotation is not None else Any
            annotated_type: Any = Annotated[(annotation, *field_info.metadata)] if field_info.metadata else annotation
            field_schema = TypeAdapter(annotated_type).core_schema

            alias_val = field_info.validation_alias if field_info.validation_alias is not None else field_info.alias
            td_kwargs: dict[str, Any] = {}
            if isinstance(alias_val, str):
                td_kwargs["validation_alias"] = alias_val

            if field_info.is_required():
                fields[name] = core_schema.typed_dict_field(field_schema, required=True, **td_kwargs)
            elif field_info.default_factory is not None:
                fields[name] = core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        field_schema,
                        default_factory=field_info.default_factory,
                        default_factory_takes_data=bool(field_info.default_factory_takes_validated_data),
                    ),
                    required=False,
                    **td_kwargs,
                )
            else:
                default_val = None if field_info.default is PydanticUndefined else field_info.default
                fields[name] = core_schema.typed_dict_field(
                    core_schema.with_default_schema(field_schema, default=default_val),
                    required=False,
                    **td_kwargs,
                )

        td_config: core_schema.CoreConfig | None = None
        if self.params_model.model_config.get("populate_by_name") or self.params_model.model_config.get(
            "validate_by_name"
        ):
            td_config = core_schema.CoreConfig(validate_by_name=True)

        return core_schema.typed_dict_schema(fields, extra_behavior="forbid", config=td_config)

    def precheck_raw(self, raw: Any, *, max_limit: int) -> None:
        """Run fast pre-Pydantic validation on a raw ``pagination`` mapping."""
        if not isinstance(raw, Mapping):
            raise InvalidQueryLimitError("`pagination` must be an object.")
        if "limit" in raw:
            _validate_limit_value(raw["limit"], max_limit=max_limit)

    def parse_params(self, raw: Mapping[str, Any] | PaginationParams | None, policy: QueryPolicy) -> ParamsT:
        """Normalize validated raw pagination data or ``None`` into ``params_model``."""
        if raw is None:
            data: dict[str, Any] = {}
        elif isinstance(raw, self.params_model):
            return raw  # type: ignore[return-value]
        elif isinstance(raw, PaginationParams):
            data = raw.model_dump(mode="python", by_alias=True)
        elif isinstance(raw, Mapping):
            data = dict(raw)
        else:
            raise InvalidQueryLimitError("`pagination` must be an object.")
        data.setdefault("limit", policy.default_limit)
        try:
            return self.params_model.model_validate(data, by_alias=True, by_name=True)  # type: ignore[return-value]
        except ValidationError as err:
            raise InvalidQueryLimitError(f"Invalid pagination parameters: {err.errors()[0]['msg']}") from err

    @abstractmethod
    def apply(self, stmt: Select[Any], ctx: PaginationContext[Any, ParamsT]) -> Select[Any]:
        """Apply ordering and pagination clauses to *stmt* without executing it."""

    @abstractmethod
    def paginate[T](self, session: Session, ctx: PaginationContext[T, ParamsT]) -> PageResult[T]:
        """Execute a paginated query synchronously and return :class:`PageResult`."""

    @abstractmethod
    async def apaginate[T](self, session: AsyncSession, ctx: PaginationContext[T, ParamsT]) -> PageResult[T]:
        """Execute a paginated query asynchronously and return :class:`PageResult`."""


class PagePager(BasePager[PaginationSpec, PageInfo]):
    """Default 1-based page-number pager (``page`` + ``limit``)."""

    params_model: ClassVar[type[PaginationParams]] = PaginationSpec
    page_info_model: ClassVar[type[BasePageInfo]] = PageInfo

    def build_params_schema(self, policy: QueryPolicy) -> CoreSchema:
        """Build the strict typed-dict schema for ``{page, limit}``."""
        return core_schema.typed_dict_schema(
            {
                "page": core_schema.typed_dict_field(
                    core_schema.with_default_schema(core_schema.int_schema(strict=True, ge=1), default=1),
                    required=False,
                ),
                "limit": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.int_schema(strict=True, ge=1, le=policy.max_limit),
                        default=policy.default_limit,
                    ),
                    required=False,
                ),
            },
            extra_behavior="forbid",
        )

    def precheck_raw(self, raw: Any, *, max_limit: int) -> None:
        """Validate raw ``page`` and ``limit`` bounds before Pydantic validation."""
        super().precheck_raw(raw, max_limit=max_limit)
        if "page" in raw:
            page = raw["page"]
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise InvalidQueryLimitError("`pagination.page` must be an integer >= 1.")

    def parse_params(self, raw: Mapping[str, Any] | PaginationParams | None, policy: QueryPolicy) -> PaginationSpec:
        """Construct a :class:`PaginationSpec` applying policy default limit."""
        if raw is None:
            return PaginationSpec.model_construct(page=1, limit=policy.default_limit)
        if isinstance(raw, PaginationSpec):
            return raw
        if isinstance(raw, Mapping):
            return PaginationSpec.model_construct(
                page=raw.get("page", 1),
                limit=raw.get("limit", policy.default_limit),
            )
        return super().parse_params(raw, policy)

    def apply(self, stmt: Select[Any], ctx: PaginationContext[Any, PaginationSpec]) -> Select[Any]:
        """Apply ``ORDER BY``, ``OFFSET``, and ``LIMIT`` for 1-based page pagination."""
        out = stmt.order_by(*ctx.order_clauses) if ctx.order_clauses else stmt
        offset = (ctx.params.page - 1) * ctx.params.limit
        return out.fetch(None).offset(offset).limit(ctx.params.limit)

    def _assemble[T](self, items: list[T], total: int, params: PaginationSpec) -> PageResult[T]:
        offset = (params.page - 1) * params.limit
        return PageResult[T](
            items=items,
            total=total,
            page_info=PageInfo(
                total=total,
                page=params.page,
                limit=params.limit,
                offset=offset,
                has_more=total > offset + params.limit,
            ),
        )

    def paginate[T](self, session: Session, ctx: PaginationContext[T, PaginationSpec]) -> PageResult[T]:
        """Execute count + page slice synchronously."""
        total = ctx.fetch_total(session)
        items = ctx.fetch_rows(session, self.apply(ctx.filtered_stmt, ctx))
        return self._assemble(items, total, ctx.params)

    async def apaginate[T](self, session: AsyncSession, ctx: PaginationContext[T, PaginationSpec]) -> PageResult[T]:
        """Execute count + page slice asynchronously."""
        total = await ctx.afetch_total(session)
        items = await ctx.afetch_rows(session, self.apply(ctx.filtered_stmt, ctx))
        return self._assemble(items, total, ctx.params)


class LimitOffsetPager(BasePager[LimitOffsetParams, LimitOffsetPageInfo]):
    """Offset-based pager (``limit`` + ``offset``) with optional ``COUNT(*)``."""

    params_model: ClassVar[type[PaginationParams]] = LimitOffsetParams
    page_info_model: ClassVar[type[BasePageInfo]] = LimitOffsetPageInfo

    def __init__(self, *, include_total: bool = True) -> None:
        self.include_total = include_total

    def build_params_schema(self, policy: QueryPolicy) -> CoreSchema:
        """Build the strict typed-dict schema for ``{limit, offset}``."""
        return core_schema.typed_dict_schema(
            {
                "limit": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.int_schema(strict=True, ge=1, le=policy.max_limit),
                        default=policy.default_limit,
                    ),
                    required=False,
                ),
                "offset": core_schema.typed_dict_field(
                    core_schema.with_default_schema(core_schema.int_schema(strict=True, ge=0), default=0),
                    required=False,
                ),
            },
            extra_behavior="forbid",
        )

    def precheck_raw(self, raw: Any, *, max_limit: int) -> None:
        """Validate raw ``offset`` and ``limit`` bounds before Pydantic validation."""
        super().precheck_raw(raw, max_limit=max_limit)
        if "offset" in raw:
            offset = raw["offset"]
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise InvalidQueryLimitError("`pagination.offset` must be an integer >= 0.")

    def parse_params(self, raw: Mapping[str, Any] | PaginationParams | None, policy: QueryPolicy) -> LimitOffsetParams:
        """Construct :class:`LimitOffsetParams` applying policy default limit."""
        if raw is None:
            return LimitOffsetParams.model_construct(limit=policy.default_limit, offset=0)
        if isinstance(raw, LimitOffsetParams):
            return raw
        if isinstance(raw, Mapping):
            return LimitOffsetParams.model_construct(
                limit=raw.get("limit", policy.default_limit),
                offset=raw.get("offset", 0),
            )
        return super().parse_params(raw, policy)

    def apply(self, stmt: Select[Any], ctx: PaginationContext[Any, LimitOffsetParams]) -> Select[Any]:
        """Apply ``ORDER BY``, ``OFFSET``, and ``LIMIT`` for offset pagination."""
        out = stmt.order_by(*ctx.order_clauses) if ctx.order_clauses else stmt
        return out.fetch(None).offset(ctx.params.offset).limit(ctx.params.limit)

    def _query_stmt(self, ctx: PaginationContext[Any, LimitOffsetParams]) -> Select[Any]:
        out = ctx.filtered_stmt.order_by(*ctx.order_clauses) if ctx.order_clauses else ctx.filtered_stmt
        fetch_limit = ctx.params.limit if self.include_total else ctx.params.limit + 1
        return out.fetch(None).offset(ctx.params.offset).limit(fetch_limit)

    def _assemble[T](self, raw_items: list[T], total: int | None, params: LimitOffsetParams) -> PageResult[T]:
        if self.include_total:
            assert total is not None
            items = raw_items
            has_more = total > params.offset + params.limit
        else:
            has_more = len(raw_items) > params.limit
            items = raw_items[: params.limit]
        return PageResult[T](
            items=items,
            total=total,
            page_info=LimitOffsetPageInfo(
                total=total,
                limit=params.limit,
                offset=params.offset,
                has_more=has_more,
            ),
        )

    def paginate[T](self, session: Session, ctx: PaginationContext[T, LimitOffsetParams]) -> PageResult[T]:
        """Execute offset pagination synchronously."""
        total = ctx.fetch_total(session) if self.include_total else None
        raw_items = ctx.fetch_rows(session, self._query_stmt(ctx))
        return self._assemble(raw_items, total, ctx.params)

    async def apaginate[T](self, session: AsyncSession, ctx: PaginationContext[T, LimitOffsetParams]) -> PageResult[T]:
        """Execute offset pagination asynchronously."""
        total = await ctx.afetch_total(session) if self.include_total else None
        raw_items = await ctx.afetch_rows(session, self._query_stmt(ctx))
        return self._assemble(raw_items, total, ctx.params)


@dataclass(frozen=True, slots=True)
class _CursorSortKey:
    """One resolved sort key participating in keyset cursor ordering."""

    name: str
    expr: Any
    direction: SortDirection
    nulls: NullsOrder | None
    field: ResolvedField | None
    nulls_first: bool


def _encode_cursor_scalar(value: Any) -> Any:
    """Convert a row's scalar sort value into a JSON-serializable primitive."""
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidCursorError("Cursor cannot encode non-finite float values (NaN or Infinity).")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidCursorError("Cursor cannot encode non-finite Decimal values (NaN or Infinity).")
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _decode_cursor_scalar(raw: Any, key: _CursorSortKey) -> Any:
    """Decode and validate a single scalar value extracted from a cursor token."""
    field = key.field
    if raw is None:
        if field is not None and not field.nullable:
            raise InvalidCursorError(f"Cursor value for non-nullable sort key `{key.name}` cannot be null.")
        return None

    if field is None:
        return raw

    kind = field.scalar_kind
    try:
        if kind == "bool":
            if not isinstance(raw, bool):
                raise ValueError("Expected boolean")
            parsed: Any = raw
        elif kind == "int":
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError("Expected integer")
            parsed = raw
        elif kind == "float":
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                raise ValueError("Expected number")
            parsed = float(raw)
            if not math.isfinite(parsed):
                raise ValueError("Expected finite float")
        elif kind == "decimal":
            if isinstance(raw, bool) or not isinstance(raw, int | float | str):
                raise ValueError("Expected decimal")
            parsed = Decimal(str(raw))
            if not parsed.is_finite():
                raise ValueError("Expected finite decimal")
        elif kind == "str":
            if not isinstance(raw, str):
                raise ValueError("Expected string")
            parsed = raw
        elif kind == "datetime":
            if not isinstance(raw, str):
                raise ValueError("Expected ISO-8601 datetime string")
            parsed = datetime.fromisoformat(raw)
        elif kind == "date":
            if not isinstance(raw, str):
                raise ValueError("Expected ISO-8601 date string")
            parsed = date.fromisoformat(raw)
        elif kind == "time":
            if not isinstance(raw, str):
                raise ValueError("Expected ISO-8601 time string")
            parsed = time.fromisoformat(raw)
        elif kind == "uuid":
            if not isinstance(raw, str):
                raise ValueError("Expected UUID string")
            parsed = UUID(raw)
        elif kind == "enum":
            allowed = field.enum_values or ()
            if allowed:
                if not any(type(raw) is type(v) and raw == v for v in allowed):
                    raise ValueError("Invalid enum value")
            elif not isinstance(raw, str):
                raise ValueError("Expected string enum value")
            parsed = raw
        else:
            parsed = raw
        return _normalize_scalar_value(field, parsed)
    except (TypeError, ValueError, InvalidOperation) as err:
        raise InvalidCursorError(f"Invalid cursor value for sort key `{key.name}`.") from err


def _sort_fingerprint(keys: Sequence[_CursorSortKey]) -> str:
    """Compute a stable short hash representing the effective cursor sort order."""
    descriptor = [[k.name, k.direction, "first" if k.nulls_first else "last"] for k in keys]
    return hashlib.sha256(orjson.dumps(descriptor)).hexdigest()[:16]


def _encode_cursor_token(values: Sequence[Any], fingerprint: str) -> str:
    """Encode cursor key values and sort fingerprint as unpadded URL-safe Base64 JSON."""
    payload = {"v": [_encode_cursor_scalar(v) for v in values], "s": fingerprint}
    raw = orjson.dumps(payload)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_cursor_token(token: str, keys: Sequence[_CursorSortKey], fingerprint: str) -> list[Any]:
    """Decode and validate an opaque URL-safe Base64 JSON cursor token."""
    if not token or not token.isascii():
        raise InvalidCursorError("Cursor token is malformed.")
    padded = token + "=" * (-len(token) % 4)
    try:
        raw_bytes = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        parsed = orjson.loads(raw_bytes)
    except (binascii.Error, ValueError, orjson.JSONDecodeError) as err:
        raise InvalidCursorError("Cursor token is malformed.") from err

    if not isinstance(parsed, dict) or set(parsed.keys()) != {"v", "s"}:
        raise InvalidCursorError("Cursor token payload is malformed.")
    if parsed["s"] != fingerprint:
        raise InvalidCursorError("Cursor token does not match the current sort order.")
    raw_values = parsed["v"]
    if not isinstance(raw_values, list) or len(raw_values) != len(keys):
        raise InvalidCursorError("Cursor token value count does not match the sort keys.")

    return [_decode_cursor_scalar(raw_val, key) for raw_val, key in zip(raw_values, keys, strict=True)]


def _keyset_strict_after(key: _CursorSortKey, val: Any) -> ColumnElement[bool]:
    """Build the strict ``>``/``<`` predicate for one sort key with NULL and boolean ordering."""
    expr = key.expr
    nullable = key.field is None or key.field.nullable

    if val is None:
        return expr.is_not(None) if key.nulls_first else false()

    if isinstance(val, bool):
        if key.direction == "asc":
            cmp_clause: ColumnElement[bool] = expr.is_(True) if val is False else false()
        else:
            cmp_clause = expr.is_(False) if val is True else false()
    else:
        cmp_clause = expr > val if key.direction == "asc" else expr < val

    if nullable and not key.nulls_first:
        return or_(cmp_clause, expr.is_(None))
    return cmp_clause


def _keyset_equal(key: _CursorSortKey, val: Any) -> ColumnElement[bool]:
    """Build the NULL-safe equality predicate for one sort key."""
    if val is None:
        return key.expr.is_(None)
    if isinstance(val, bool):
        return key.expr.is_(val)
    return key.expr == val


def _has_aggregate_projection(stmt: Select[Any]) -> bool:
    """Return ``True`` when any selected column of *stmt* contains a SQL aggregate function."""
    for col in stmt.selected_columns:
        for element in sql_iterate(col):
            if isinstance(element, FunctionElement) and (element.name or "").lower() in _AGGREGATE_FUNC_NAMES:
                return True
    return False


def _infer_sort_expression_field(name: str, expr: Any) -> ResolvedField | None:
    """Infer a synthetic :class:`ResolvedField` for a custom ``QueryPolicy.sort_expressions`` entry."""
    sql_type = getattr(expr, "type", None)
    if sql_type is None or isinstance(sql_type, NullType):
        return None
    try:
        scalar_kind, ops, dt_tz, enum_vals, enum_cls, uuid_as_uuid = _infer_from_sql_type(
            sql_type, allow_raw_like=False
        )
    except BadFilterFormatError:
        return None
    return ResolvedField(
        name=name,
        expression=expr,
        scalar_kind=scalar_kind,
        operators=ops,
        nullable=bool(getattr(expr, "nullable", True)),
        filterable=False,
        sortable=True,
        projectable=False,
        datetime_timezone=dt_tz,
        enum_values=enum_vals,
        enum_type=enum_cls,
        uuid_as_uuid=uuid_as_uuid,
    )


def _build_keyset_where(keys: Sequence[_CursorSortKey], values: Sequence[Any]) -> ColumnElement[bool]:
    """Build a lexicographical keyset ``WHERE`` clause supporting mixed directions and NULLs."""
    branches: list[ColumnElement[bool]] = []
    eq_prefix: list[ColumnElement[bool]] = []

    for key, val in zip(keys, values, strict=True):
        after_clause = _keyset_strict_after(key, val)
        if eq_prefix:
            branches.append(and_(*eq_prefix, after_clause))
        else:
            branches.append(after_clause)
        eq_prefix.append(_keyset_equal(key, val))

    if len(branches) == 1:
        return branches[0]
    return or_(*branches)


class CursorPager(BasePager[CursorParams, CursorPageInfo]):
    """Forward keyset cursor pager with opaque URL-safe Base64 JSON tokens.

    For single-table queries, automatically appends the model's primary key
    columns as deterministic tie-breakers (in declared PK order, ascending)
    whenever they are not already present in ``query.sort``.

    For multi-table, joined, or subquery selects where the base model's primary
    key may not uniquely identify each output row (for example a 1-to-N join),
    callers must declare explicit row-unique sortable keys via
    ``CursorPager(unique_keys=[...])``; otherwise a :class:`BadFilterFormatError`
    is raised instead of silently skipping rows.

    By default ``include_total=True`` runs ``COUNT(*)`` so ``PageResult.total``
    and ``CursorPageInfo.total`` are populated consistently across all pagers.
    Pass ``CursorPager(include_total=False)`` on very large tables to skip
    ``COUNT(*)`` entirely.
    """

    params_model: ClassVar[type[PaginationParams]] = CursorParams
    page_info_model: ClassVar[type[BasePageInfo]] = CursorPageInfo

    def __init__(
        self,
        *,
        include_total: bool = True,
        unique_keys: Sequence[str] | None = None,
    ) -> None:
        if unique_keys is not None:
            keys_tuple = tuple(unique_keys)
            if not keys_tuple:
                raise BadFilterFormatError("`unique_keys` must contain at least one sortable field name.")
            self.unique_keys: tuple[str, ...] | None = keys_tuple
        else:
            self.unique_keys = None
        self.include_total = include_total

    def build_params_schema(self, policy: QueryPolicy) -> CoreSchema:
        """Build the strict typed-dict schema for ``{cursor, limit}``."""
        return core_schema.typed_dict_schema(
            {
                "cursor": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.nullable_schema(core_schema.str_schema(strict=True)),
                        default=None,
                    ),
                    required=False,
                ),
                "limit": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.int_schema(strict=True, ge=1, le=policy.max_limit),
                        default=policy.default_limit,
                    ),
                    required=False,
                ),
            },
            extra_behavior="forbid",
        )

    def precheck_raw(self, raw: Any, *, max_limit: int) -> None:
        """Validate raw ``cursor`` and ``limit`` before Pydantic validation."""
        super().precheck_raw(raw, max_limit=max_limit)
        if "cursor" in raw:
            cursor = raw["cursor"]
            if cursor is not None and not isinstance(cursor, str):
                raise InvalidCursorError("`pagination.cursor` must be a string or null.")

    def parse_params(self, raw: Mapping[str, Any] | PaginationParams | None, policy: QueryPolicy) -> CursorParams:
        """Construct :class:`CursorParams` applying policy default limit."""
        if raw is None:
            return CursorParams.model_construct(cursor=None, limit=policy.default_limit)
        if isinstance(raw, CursorParams):
            return raw
        if isinstance(raw, Mapping):
            return CursorParams.model_construct(
                cursor=raw.get("cursor"),
                limit=raw.get("limit", policy.default_limit),
            )
        return super().parse_params(raw, policy)

    def _resolve_sort_keys(self, ctx: PaginationContext[Any, CursorParams]) -> list[_CursorSortKey]:
        stmt = ctx.filtered_stmt
        if ctx.has_group_by or bool(getattr(stmt, "_group_by_clauses", ())):
            raise BadFilterFormatError(
                "CursorPager does not support `group_by` queries; use PagePager or LimitOffsetPager."
            )
        if bool(getattr(stmt, "_distinct", False)) or bool(getattr(stmt, "_distinct_on", ())):
            raise BadFilterFormatError(
                "CursorPager does not support `DISTINCT` queries; use PagePager or LimitOffsetPager."
            )
        if bool(getattr(stmt, "_having_criteria", ())):
            raise BadFilterFormatError(
                "CursorPager does not support `HAVING` queries; use PagePager or LimitOffsetPager."
            )
        if ctx.mode != "models" and _has_aggregate_projection(stmt):
            raise BadFilterFormatError(
                "CursorPager does not support aggregate projections; use PagePager or LimitOffsetPager."
            )

        resolved = ctx.resolved
        keys: list[_CursorSortKey] = []
        seen: set[str] = set()

        for item in ctx.spec.sort:
            expr = resolved.sortable_expressions.get(item.field)
            if expr is None:
                raise BadFilterFormatError(f"Field `{item.field}` is not allowed on {resolved.model.__name__}.")
            field_meta = resolved.fields.get(item.field)
            if field_meta is None or field_meta.expression is not expr:
                field_meta = _infer_sort_expression_field(item.field, expr)
            nullable = field_meta is None or field_meta.nullable
            nulls_first = item.nulls == "first" if item.nulls is not None else False
            effective_nulls: NullsOrder | None = item.nulls
            if nullable and effective_nulls is None:
                effective_nulls = "last"
            keys.append(
                _CursorSortKey(
                    name=item.field,
                    expr=expr,
                    direction=item.direction,
                    nulls=effective_nulls,
                    field=field_meta,
                    nulls_first=nulls_first,
                )
            )
            seen.add(item.field)

        froms = stmt.get_final_froms()
        model_table = getattr(resolved.model, "__table__", None)
        sole_from = froms[0]._deannotate() if len(froms) == 1 and hasattr(froms[0], "_deannotate") else None
        is_single_model_table = len(froms) == 1 and (froms[0] is model_table or sole_from is model_table)

        if self.unique_keys is not None:
            for key_name in self.unique_keys:
                if key_name in seen:
                    continue
                expr = resolved.sortable_expressions.get(key_name)
                if expr is None:
                    raise BadFilterFormatError(
                        f"Cursor unique key `{key_name}` is not a sortable field on {resolved.model.__name__}."
                    )
                field_meta = resolved.fields.get(key_name)
                if field_meta is None or field_meta.expression is not expr:
                    field_meta = _infer_sort_expression_field(key_name, expr)
                nullable = field_meta is None or field_meta.nullable
                keys.append(
                    _CursorSortKey(
                        name=key_name,
                        expr=expr,
                        direction="asc",
                        nulls="last" if nullable else None,
                        field=field_meta,
                        nulls_first=False,
                    )
                )
                seen.add(key_name)
        elif is_single_model_table:
            # Resolve automatic PK tie-breakers from mapped primary-key columns directly
            # so `QueryPolicy.sort_expressions` overrides or `sortable=False` on a PK name
            # can never replace or drop the real uniqueness guarantee.
            for pk_name in resolved.primary_key_order:
                pk_field = resolved.fields.get(pk_name)
                if pk_field is None:
                    raise BadFilterFormatError(
                        f"Cursor primary key `{pk_name}` is not mapped on {resolved.model.__name__}."
                    )
                if any(k.expr is pk_field.expression for k in keys):
                    continue
                keys.append(
                    _CursorSortKey(
                        name=pk_name,
                        expr=pk_field.expression,
                        direction="asc",
                        nulls="last" if pk_field.nullable else None,
                        field=pk_field,
                        nulls_first=False,
                    )
                )
        else:
            raise BadFilterFormatError(
                "CursorPager requires explicit `unique_keys` for multi-table or joined queries; "
                "pass `CursorPager(unique_keys=[...])` or use PagePager/LimitOffsetPager."
            )

        if not keys:
            raise BadFilterFormatError(
                f"CursorPager requires at least one sort or primary key on {resolved.model.__name__}."
            )
        return keys

    def _compile_order_by(
        self,
        keys: Sequence[_CursorSortKey],
        *,
        dialect_name: str | None = None,
    ) -> list[ColumnElement[Any]]:
        clauses: list[ColumnElement[Any]] = []
        for key in keys:
            clauses.extend(
                compile_order_item(
                    key.expr,
                    direction=key.direction,
                    nulls=key.nulls,
                    dialect_name=dialect_name,
                )
            )
        return clauses

    def _prepare_statement(
        self,
        stmt: Select[Any],
        ctx: PaginationContext[Any, CursorParams],
        *,
        limit: int,
        dialect_name: str | None = None,
    ) -> tuple[Select[Any], list[_CursorSortKey], str]:
        keys = self._resolve_sort_keys(ctx)
        fingerprint = _sort_fingerprint(keys)
        out = stmt.order_by(None)

        if ctx.params.cursor is not None:
            cursor_values = _decode_cursor_token(ctx.params.cursor, keys, fingerprint)
            out = out.where(_build_keyset_where(keys, cursor_values))

        out = out.order_by(*self._compile_order_by(keys, dialect_name=dialect_name))
        out = out.fetch(None).offset(None).limit(limit)
        return out, keys, fingerprint

    def apply(self, stmt: Select[Any], ctx: PaginationContext[Any, CursorParams]) -> Select[Any]:
        """Apply keyset ``WHERE``, deterministic ``ORDER BY`` (with PKs), and ``LIMIT``."""
        out, _, _ = self._prepare_statement(stmt, ctx, limit=ctx.params.limit)
        return out

    def _needs_hidden_cursor_cols(
        self,
        ctx: PaginationContext[Any, CursorParams],
        keys: Sequence[_CursorSortKey],
    ) -> bool:
        if ctx.mode != "models" or bool(ctx.options) or bool(getattr(ctx.filtered_stmt, "_with_options", ())):
            return True
        model = ctx.resolved.model
        mapper = inspect(model)
        for key in keys:
            if key.name not in ctx.resolved.fields:
                return True
            if getattr(model, key.name, None) is not key.expr:
                return True
            col_attr = mapper.column_attrs.get(key.name)
            if col_attr is None or bool(getattr(col_attr, "deferred", False)):
                return True
        return False

    def _add_hidden_cols(self, stmt: Select[Any], keys: Sequence[_CursorSortKey]) -> tuple[Select[Any], list[str]]:
        labels = [f"{_CURSOR_COL_PREFIX}{idx}" for idx in range(len(keys))]
        extra_exprs = [key.expr.label(lbl) for key, lbl in zip(keys, labels, strict=True)]
        return stmt.add_columns(*extra_exprs), labels

    def _extract_results[T](
        self,
        raw_rows: list[Any],
        *,
        ctx: PaginationContext[T, CursorParams],
        keys: Sequence[_CursorSortKey],
        fingerprint: str,
        hidden_labels: list[str] | None,
        total: int | None,
    ) -> PageResult[T]:
        limit = ctx.params.limit
        has_more = len(raw_rows) > limit
        page_rows = raw_rows[:limit]

        items: list[T]
        last_cursor_values: list[Any] | None = None

        if ctx.mode == "models":
            if hidden_labels is None:
                items = page_rows
                if has_more and items:
                    last_obj = items[-1]
                    last_cursor_values = [getattr(last_obj, k.name) for k in keys]
            else:
                items = [row[0] for row in page_rows]
                if has_more and page_rows:
                    last_row = page_rows[-1]
                    last_cursor_values = [last_row._mapping[lbl] for lbl in hidden_labels]
        else:
            assert hidden_labels is not None
            hidden_set = set(hidden_labels)
            if has_more and page_rows:
                last_mapping = page_rows[-1]
                last_cursor_values = [last_mapping[lbl] for lbl in hidden_labels]
            items = [{k: v for k, v in row.items() if k not in hidden_set} for row in page_rows]  # type: ignore[misc]

        next_cursor = (
            _encode_cursor_token(last_cursor_values, fingerprint)
            if (has_more and last_cursor_values is not None)
            else None
        )
        return PageResult[T](
            items=items,
            total=total,
            page_info=CursorPageInfo(
                total=total,
                limit=limit,
                has_more=has_more,
                next_cursor=next_cursor,
            ),
        )

    def paginate[T](self, session: Session, ctx: PaginationContext[T, CursorParams]) -> PageResult[T]:
        """Execute forward keyset cursor pagination synchronously."""
        dialect_name = session_dialect_name(session, mapper=ctx.resolved.model)
        data_stmt, keys, fingerprint = self._prepare_statement(
            ctx.filtered_stmt,
            ctx,
            limit=ctx.params.limit + 1,
            dialect_name=dialect_name,
        )
        total = ctx.fetch_total(session) if self.include_total else None

        needs_hidden = self._needs_hidden_cursor_cols(ctx, keys)
        hidden_labels: list[str] | None = None
        if needs_hidden:
            data_stmt, hidden_labels = self._add_hidden_cols(data_stmt, keys)

        exec_stmt = ctx.with_options(data_stmt)
        if ctx.mode == "models":
            if hidden_labels is None:
                raw_rows: list[Any] = list(session.execute(exec_stmt).unique().scalars().all())
            else:
                raw_rows = list(session.execute(exec_stmt).unique().all())
        else:
            raw_rows = [dict(row) for row in session.execute(exec_stmt).mappings().all()]

        return self._extract_results(
            raw_rows,
            ctx=ctx,
            keys=keys,
            fingerprint=fingerprint,
            hidden_labels=hidden_labels,
            total=total,
        )

    async def apaginate[T](self, session: AsyncSession, ctx: PaginationContext[T, CursorParams]) -> PageResult[T]:
        """Execute forward keyset cursor pagination asynchronously."""
        dialect_name = session_dialect_name(session, mapper=ctx.resolved.model)
        data_stmt, keys, fingerprint = self._prepare_statement(
            ctx.filtered_stmt,
            ctx,
            limit=ctx.params.limit + 1,
            dialect_name=dialect_name,
        )
        total = await ctx.afetch_total(session) if self.include_total else None

        needs_hidden = self._needs_hidden_cursor_cols(ctx, keys)
        hidden_labels: list[str] | None = None
        if needs_hidden:
            data_stmt, hidden_labels = self._add_hidden_cols(data_stmt, keys)

        exec_stmt = ctx.with_options(data_stmt)
        result = await session.execute(exec_stmt)
        if ctx.mode == "models":
            if hidden_labels is None:
                raw_rows: list[Any] = list(result.unique().scalars().all())
            else:
                raw_rows = list(result.unique().all())
        else:
            raw_rows = [dict(row) for row in result.mappings().all()]

        return self._extract_results(
            raw_rows,
            ctx=ctx,
            keys=keys,
            fingerprint=fingerprint,
            hidden_labels=hidden_labels,
            total=total,
        )
