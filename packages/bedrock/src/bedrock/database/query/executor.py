"""QuerySchema facade and sync/async executors for models, fields, and selects."""

from collections.abc import Sequence
from threading import Lock
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema
from sqlalchemy import Column, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.base import ExecutableOption
from sqlalchemy.sql.elements import ColumnElement, Label

from ...exc import BadFilterFormatError, InvalidQueryLimitError
from ..base import BedrockModel
from .ast import (
    FieldExpr,
    FilterNode,
    GroupByExpr,
    PageInfo,
    PageResult,
    QueryInput,
    QuerySpec,
)
from .compiler import compile_order_by, compile_where
from .policy import QueryPolicy, ResolvedModelPolicy, resolve_model_policy
from .schema import CompiledQueryValidator

M = TypeVar("M", bound=BedrockModel)

_DEFAULT_SCHEMA_CACHE: WeakKeyDictionary[type[Any], "QuerySchema[Any]"] = WeakKeyDictionary()
_CACHE_LOCK = Lock()


def _validate_server_limit(limit: int | None) -> int | None:
    """Validate an explicit server-side row cap passed to non-paginated search helpers."""
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise InvalidQueryLimitError("`limit` must be a positive integer.")
    return limit


def _resolve_projection_exprs(
    items: Sequence[FieldExpr | GroupByExpr],
    resolved: ResolvedModelPolicy,
    *,
    param_name: str,
) -> list[Any]:
    """Resolve string field names against allowlisted scalar fields; pass SQL expressions through."""
    expressions: list[Any] = []
    for item in items:
        if isinstance(item, str):
            field = resolved.fields.get(item)
            if field is None:
                raise BadFilterFormatError(
                    f"Field `{item}` is not allowed on {resolved.model.__name__} in `{param_name}`."
                )
            expressions.append(field.expression)
        else:
            expressions.append(item)
    return expressions


def _is_plain_model_column(expr: Any, model: type[Any]) -> bool:
    """Return ``True`` only when *expr* projects a plain column on *model* (1:1 row cardinality)."""
    target = expr.element if isinstance(expr, Label) else expr
    if isinstance(target, InstrumentedAttribute):
        return bool(target.class_ is model and hasattr(target.property, "columns"))
    if isinstance(target, Column):
        return target.table is getattr(model, "__table__", None)
    return False


def _build_page_result[T](items: list[T], total: int, *, page: int, limit: int) -> PageResult[T]:
    """Assemble a typed ``PageResult`` from executed rows and total count."""
    offset = (page - 1) * limit
    return PageResult[T](
        items=items,
        total=total,
        page_info=PageInfo(
            total=total,
            page=page,
            limit=limit,
            offset=offset,
            has_more=total > offset + limit,
        ),
    )


class QuerySchema[M: BedrockModel]:
    """Compiled JSON Schema query contract, validator, SQLAlchemy compiler, and executor.

    Instances implement Pydantic v2's ``__get_pydantic_core_schema__`` protocol,
    so they can be used directly in ``Annotated[QuerySpec, order_schema]`` or
    ``Annotated[FilterNode, order_schema]`` for FastAPI routes and Pydantic
    entities without HTTP coupling in ``bedrock-core``.
    """

    def __init__(self, model: type[M], *, policy: QueryPolicy | None = None) -> None:
        self.model: type[M] = model
        self._resolved: ResolvedModelPolicy = resolve_model_policy(model, policy=policy)
        self.policy: QueryPolicy = self._resolved.policy
        self._validator = CompiledQueryValidator(self._resolved)

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Integrate with Pydantic v2 / FastAPI via ``Annotated[QuerySpec, schema]``."""
        return self._validator.build_pydantic_core_schema(source_type, handler)

    def json_schema(self) -> dict[str, Any]:
        """Return the Draft 2020-12 JSON Schema for the full query payload."""
        return self._validator.json_schema()

    def filter_json_schema(self) -> dict[str, Any]:
        """Return the Draft 2020-12 JSON Schema for standalone filter payloads."""
        return self._validator.filter_json_schema()

    def validate_json(self, raw: str | bytes) -> QuerySpec:
        """Parse and validate a JSON string/bytes payload into a :class:`QuerySpec`."""
        return self._validator.validate_json(raw)

    def validate_python(self, raw: QueryInput) -> QuerySpec:
        """Validate a Python dict, :class:`QuerySpec`, JSON bytes/str, or ``None``."""
        return self._validator.validate_python(raw)

    def validate(self, raw: QueryInput) -> QuerySpec:
        """Alias for :meth:`validate_python` accepting any :data:`QueryInput`."""
        return self._validator.validate_python(raw)

    def validate_filter(self, raw: str | bytes | dict[str, Any] | FilterNode) -> FilterNode:
        """Validate a standalone filter payload into a :class:`FilterNode`."""
        return self._validator.validate_filter(raw)

    def compile_where(self, query: QueryInput) -> ColumnElement[bool]:
        """Validate *query* and compile its ``filter`` and ``search`` into a ``WHERE`` clause."""
        spec = self.validate(query)
        return compile_where(spec, self._resolved)

    def compile_order_by(self, query: QueryInput) -> list[ColumnElement[Any]]:
        """Validate *query* and compile its multi-field ``sort`` into ``ORDER BY`` clauses."""
        spec = self.validate(query)
        return compile_order_by(spec, self._resolved)

    def apply(
        self,
        stmt: Select[Any],
        query: QueryInput,
        *,
        paginate: bool = True,
    ) -> Select[Any]:
        """Apply validated ``WHERE``, ``ORDER BY``, and optional pagination to *stmt*."""
        spec = self.validate(query)
        where_clause = compile_where(spec, self._resolved)
        order_clauses = compile_order_by(spec, self._resolved)
        out = stmt.where(where_clause)
        if order_clauses:
            out = out.order_by(*order_clauses)
        if paginate:
            page, limit = self._pagination_bounds(spec)
            out = out.offset((page - 1) * limit).limit(limit)
        return out

    def _pagination_bounds(self, spec: QuerySpec) -> tuple[int, int]:
        if spec.pagination is None:
            return 1, self.policy.default_limit
        return spec.pagination.page, spec.pagination.limit

    def _model_statements(
        self,
        query: QueryInput,
        *,
        base_select: Select[Any] | None,
        options: Sequence[ExecutableOption] | None,
        paginated: bool,
        server_limit: int | None = None,
    ) -> tuple[Select[Any], Select[Any] | None, int, int]:
        spec = self.validate(query)
        where_clause = compile_where(spec, self._resolved)
        order_clauses = compile_order_by(spec, self._resolved)

        base = select(self.model) if base_select is None else base_select
        filtered = base.where(where_clause)

        data_stmt = filtered
        if order_clauses:
            data_stmt = data_stmt.order_by(*order_clauses)
        if options:
            data_stmt = data_stmt.options(*options)

        if paginated:
            page, limit = self._pagination_bounds(spec)
            data_stmt = data_stmt.offset((page - 1) * limit).limit(limit)
            if base_select is None:
                count_stmt: Select[Any] | None = select(func.count()).select_from(self.model).where(where_clause)
            else:
                count_stmt = select(func.count()).select_from(filtered.order_by(None).subquery())
            return data_stmt, count_stmt, page, limit

        cap = _validate_server_limit(server_limit)
        if cap is not None:
            data_stmt = data_stmt.limit(cap)
        return data_stmt, None, 1, 0

    def _fields_statements(
        self,
        fields: Sequence[FieldExpr],
        query: QueryInput,
        *,
        group_by: Sequence[GroupByExpr] | None,
        base_select: Select[Any] | None,
        paginated: bool,
        server_limit: int | None = None,
    ) -> tuple[Select[Any], Select[Any] | None, int, int]:
        if not fields:
            raise BadFilterFormatError("`fields` must contain at least one column or expression.")

        spec = self.validate(query)
        where_clause = compile_where(spec, self._resolved)
        order_clauses = compile_order_by(spec, self._resolved)
        projected = _resolve_projection_exprs(fields, self._resolved, param_name="fields")
        grouped = _resolve_projection_exprs(group_by, self._resolved, param_name="group_by") if group_by else []

        if base_select is None:
            base: Select[Any] = select(*projected).select_from(self.model)
        else:
            base = base_select.with_only_columns(*projected)

        filtered = base.where(where_clause)
        if grouped:
            filtered = filtered.group_by(*grouped)

        data_stmt = filtered
        if order_clauses:
            data_stmt = data_stmt.order_by(*order_clauses)

        if paginated:
            page, limit = self._pagination_bounds(spec)
            data_stmt = data_stmt.offset((page - 1) * limit).limit(limit)
            all_plain_columns = all(_is_plain_model_column(expr, self.model) for expr in projected)
            if not grouped and base_select is None and all_plain_columns:
                count_stmt: Select[Any] | None = select(func.count()).select_from(self.model).where(where_clause)
            else:
                count_stmt = select(func.count()).select_from(filtered.order_by(None).subquery())
            return data_stmt, count_stmt, page, limit

        cap = _validate_server_limit(server_limit)
        if cap is not None:
            data_stmt = data_stmt.limit(cap)
        return data_stmt, None, 1, 0

    def _select_statements(
        self,
        stmt: Select[Any],
        query: QueryInput,
        *,
        paginated: bool,
        server_limit: int | None = None,
    ) -> tuple[Select[Any], Select[Any] | None, int, int]:
        spec = self.validate(query)
        where_clause = compile_where(spec, self._resolved)
        order_clauses = compile_order_by(spec, self._resolved)

        filtered = stmt.where(where_clause)
        data_stmt = filtered.order_by(*order_clauses) if order_clauses else filtered

        if paginated:
            page, limit = self._pagination_bounds(spec)
            data_stmt = data_stmt.offset((page - 1) * limit).limit(limit)
            count_stmt: Select[Any] | None = select(func.count()).select_from(filtered.order_by(None).subquery())
            return data_stmt, count_stmt, page, limit

        cap = _validate_server_limit(server_limit)
        if cap is not None:
            data_stmt = data_stmt.limit(cap)
        return data_stmt, None, 1, 0

    # --- 1. Model entity search & paginate ---

    def search(
        self,
        session: Session,
        query: QueryInput = None,
        *,
        base_select: Select[Any] | None = None,
        options: Sequence[ExecutableOption] | None = None,
        limit: int | None = None,
    ) -> list[M]:
        """Execute an unpaginated entity search returning ORM instances.

        Validates *query* (including ``pagination`` if present) and silently
        ignores ``query.pagination`` without running a ``COUNT(*)`` query.
        """
        data_stmt, _, _, _ = self._model_statements(
            query,
            base_select=base_select,
            options=options,
            paginated=False,
            server_limit=limit,
        )
        return list(session.execute(data_stmt).unique().scalars().all())

    async def asearch(
        self,
        session: AsyncSession,
        query: QueryInput = None,
        *,
        base_select: Select[Any] | None = None,
        options: Sequence[ExecutableOption] | None = None,
        limit: int | None = None,
    ) -> list[M]:
        """Asynchronous counterpart of :meth:`search` for ``AsyncSession``."""
        data_stmt, _, _, _ = self._model_statements(
            query,
            base_select=base_select,
            options=options,
            paginated=False,
            server_limit=limit,
        )
        result = await session.execute(data_stmt)
        return list(result.unique().scalars().all())

    def paginate(
        self,
        session: Session,
        query: QueryInput = None,
        *,
        base_select: Select[Any] | None = None,
        options: Sequence[ExecutableOption] | None = None,
    ) -> PageResult[M]:
        """Execute a paginated entity search returning :class:`PageResult`."""
        data_stmt, count_stmt, page, limit = self._model_statements(
            query,
            base_select=base_select,
            options=options,
            paginated=True,
        )
        assert count_stmt is not None
        total = int(session.execute(count_stmt).scalar() or 0)
        items = list(session.execute(data_stmt).unique().scalars().all())
        return _build_page_result(items, total, page=page, limit=limit)

    async def apaginate(
        self,
        session: AsyncSession,
        query: QueryInput = None,
        *,
        base_select: Select[Any] | None = None,
        options: Sequence[ExecutableOption] | None = None,
    ) -> PageResult[M]:
        """Asynchronous counterpart of :meth:`paginate` for ``AsyncSession``."""
        data_stmt, count_stmt, page, limit = self._model_statements(
            query,
            base_select=base_select,
            options=options,
            paginated=True,
        )
        assert count_stmt is not None
        total = int((await session.execute(count_stmt)).scalar() or 0)
        items = list((await session.execute(data_stmt)).unique().scalars().all())
        return _build_page_result(items, total, page=page, limit=limit)

    # --- 2. Field projection / aggregation search & paginate ---

    def search_fields(
        self,
        session: Session,
        fields: Sequence[FieldExpr],
        query: QueryInput = None,
        *,
        group_by: Sequence[GroupByExpr] | None = None,
        base_select: Select[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute an unpaginated column/aggregate projection returning dict rows."""
        data_stmt, _, _, _ = self._fields_statements(
            fields,
            query,
            group_by=group_by,
            base_select=base_select,
            paginated=False,
            server_limit=limit,
        )
        return [dict(row) for row in session.execute(data_stmt).mappings().all()]

    async def asearch_fields(
        self,
        session: AsyncSession,
        fields: Sequence[FieldExpr],
        query: QueryInput = None,
        *,
        group_by: Sequence[GroupByExpr] | None = None,
        base_select: Select[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Asynchronous counterpart of :meth:`search_fields` for ``AsyncSession``."""
        data_stmt, _, _, _ = self._fields_statements(
            fields,
            query,
            group_by=group_by,
            base_select=base_select,
            paginated=False,
            server_limit=limit,
        )
        result = await session.execute(data_stmt)
        return [dict(row) for row in result.mappings().all()]

    def paginate_fields(
        self,
        session: Session,
        fields: Sequence[FieldExpr],
        query: QueryInput = None,
        *,
        group_by: Sequence[GroupByExpr] | None = None,
        base_select: Select[Any] | None = None,
    ) -> PageResult[dict[str, Any]]:
        """Execute a paginated column/aggregate projection returning ``PageResult[dict]``."""
        data_stmt, count_stmt, page, limit = self._fields_statements(
            fields,
            query,
            group_by=group_by,
            base_select=base_select,
            paginated=True,
        )
        assert count_stmt is not None
        total = int(session.execute(count_stmt).scalar() or 0)
        items = [dict(row) for row in session.execute(data_stmt).mappings().all()]
        return _build_page_result(items, total, page=page, limit=limit)

    async def apaginate_fields(
        self,
        session: AsyncSession,
        fields: Sequence[FieldExpr],
        query: QueryInput = None,
        *,
        group_by: Sequence[GroupByExpr] | None = None,
        base_select: Select[Any] | None = None,
    ) -> PageResult[dict[str, Any]]:
        """Asynchronous counterpart of :meth:`paginate_fields` for ``AsyncSession``."""
        data_stmt, count_stmt, page, limit = self._fields_statements(
            fields,
            query,
            group_by=group_by,
            base_select=base_select,
            paginated=True,
        )
        assert count_stmt is not None
        total = int((await session.execute(count_stmt)).scalar() or 0)
        items = [dict(row) for row in (await session.execute(data_stmt)).mappings().all()]
        return _build_page_result(items, total, page=page, limit=limit)

    # --- 3. Custom cross-table Select search & paginate ---

    def search_select(
        self,
        session: Session,
        stmt: Select[Any],
        query: QueryInput = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Apply *query* to an explicit SQLAlchemy ``Select`` and return dict rows."""
        data_stmt, _, _, _ = self._select_statements(stmt, query, paginated=False, server_limit=limit)
        return [dict(row) for row in session.execute(data_stmt).mappings().all()]

    async def asearch_select(
        self,
        session: AsyncSession,
        stmt: Select[Any],
        query: QueryInput = None,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Asynchronous counterpart of :meth:`search_select` for ``AsyncSession``."""
        data_stmt, _, _, _ = self._select_statements(stmt, query, paginated=False, server_limit=limit)
        result = await session.execute(data_stmt)
        return [dict(row) for row in result.mappings().all()]

    def paginate_select(
        self,
        session: Session,
        stmt: Select[Any],
        query: QueryInput = None,
    ) -> PageResult[dict[str, Any]]:
        """Apply *query* to an explicit SQLAlchemy ``Select`` and return paginated dict rows."""
        data_stmt, count_stmt, page, limit = self._select_statements(stmt, query, paginated=True)
        assert count_stmt is not None
        total = int(session.execute(count_stmt).scalar() or 0)
        items = [dict(row) for row in session.execute(data_stmt).mappings().all()]
        return _build_page_result(items, total, page=page, limit=limit)

    async def apaginate_select(
        self,
        session: AsyncSession,
        stmt: Select[Any],
        query: QueryInput = None,
    ) -> PageResult[dict[str, Any]]:
        """Asynchronous counterpart of :meth:`paginate_select` for ``AsyncSession``."""
        data_stmt, count_stmt, page, limit = self._select_statements(stmt, query, paginated=True)
        assert count_stmt is not None
        total = int((await session.execute(count_stmt)).scalar() or 0)
        items = [dict(row) for row in (await session.execute(data_stmt)).mappings().all()]
        return _build_page_result(items, total, page=page, limit=limit)


def compile_query_schema[M: BedrockModel](
    model: type[M],
    *,
    policy: QueryPolicy | None = None,
) -> QuerySchema[M]:
    """Compile (or reuse cached) :class:`QuerySchema` for *model*.

    Default-policy schemas (``policy=None``) are cached in a
    ``WeakKeyDictionary`` keyed by *model*. When a custom *policy* is passed,
    a dedicated schema is returned for the caller (such as a router module) to
    hold without growing global cache memory.
    """
    if policy is not None:
        return QuerySchema(model, policy=policy)
    with _CACHE_LOCK:
        cached = _DEFAULT_SCHEMA_CACHE.get(model)
        if cached is not None:
            return cached
        compiled: QuerySchema[M] = QuerySchema(model)
        _DEFAULT_SCHEMA_CACHE[model] = compiled
        return compiled


def search_models[M: BedrockModel](
    session: Session,
    model: type[M],
    query: QueryInput = None,
    *,
    base_select: Select[Any] | None = None,
    options: Sequence[ExecutableOption] | None = None,
    limit: int | None = None,
    policy: QueryPolicy | None = None,
) -> list[M]:
    """Execute an unpaginated entity search against *model*."""
    return compile_query_schema(model, policy=policy).search(
        session,
        query,
        base_select=base_select,
        options=options,
        limit=limit,
    )


async def asearch_models[M: BedrockModel](
    session: AsyncSession,
    model: type[M],
    query: QueryInput = None,
    *,
    base_select: Select[Any] | None = None,
    options: Sequence[ExecutableOption] | None = None,
    limit: int | None = None,
    policy: QueryPolicy | None = None,
) -> list[M]:
    """Asynchronous counterpart of :func:`search_models` for ``AsyncSession``."""
    return await compile_query_schema(model, policy=policy).asearch(
        session,
        query,
        base_select=base_select,
        options=options,
        limit=limit,
    )


def paginate_models[M: BedrockModel](
    session: Session,
    model: type[M],
    query: QueryInput = None,
    *,
    base_select: Select[Any] | None = None,
    options: Sequence[ExecutableOption] | None = None,
    policy: QueryPolicy | None = None,
) -> PageResult[M]:
    """Execute a paginated entity search against *model*."""
    return compile_query_schema(model, policy=policy).paginate(
        session,
        query,
        base_select=base_select,
        options=options,
    )


async def apaginate_models[M: BedrockModel](
    session: AsyncSession,
    model: type[M],
    query: QueryInput = None,
    *,
    base_select: Select[Any] | None = None,
    options: Sequence[ExecutableOption] | None = None,
    policy: QueryPolicy | None = None,
) -> PageResult[M]:
    """Asynchronous counterpart of :func:`paginate_models` for ``AsyncSession``."""
    return await compile_query_schema(model, policy=policy).apaginate(
        session,
        query,
        base_select=base_select,
        options=options,
    )


def search_fields[M: BedrockModel](
    session: Session,
    model: type[M],
    fields: Sequence[FieldExpr],
    query: QueryInput = None,
    *,
    group_by: Sequence[GroupByExpr] | None = None,
    base_select: Select[Any] | None = None,
    limit: int | None = None,
    policy: QueryPolicy | None = None,
) -> list[dict[str, Any]]:
    """Execute an unpaginated column/aggregate projection against *model*."""
    return compile_query_schema(model, policy=policy).search_fields(
        session,
        fields,
        query,
        group_by=group_by,
        base_select=base_select,
        limit=limit,
    )


async def asearch_fields[M: BedrockModel](
    session: AsyncSession,
    model: type[M],
    fields: Sequence[FieldExpr],
    query: QueryInput = None,
    *,
    group_by: Sequence[GroupByExpr] | None = None,
    base_select: Select[Any] | None = None,
    limit: int | None = None,
    policy: QueryPolicy | None = None,
) -> list[dict[str, Any]]:
    """Asynchronous counterpart of :func:`search_fields` for ``AsyncSession``."""
    return await compile_query_schema(model, policy=policy).asearch_fields(
        session,
        fields,
        query,
        group_by=group_by,
        base_select=base_select,
        limit=limit,
    )


def paginate_fields[M: BedrockModel](
    session: Session,
    model: type[M],
    fields: Sequence[FieldExpr],
    query: QueryInput = None,
    *,
    group_by: Sequence[GroupByExpr] | None = None,
    base_select: Select[Any] | None = None,
    policy: QueryPolicy | None = None,
) -> PageResult[dict[str, Any]]:
    """Execute a paginated column/aggregate projection against *model*."""
    return compile_query_schema(model, policy=policy).paginate_fields(
        session,
        fields,
        query,
        group_by=group_by,
        base_select=base_select,
    )


async def apaginate_fields[M: BedrockModel](
    session: AsyncSession,
    model: type[M],
    fields: Sequence[FieldExpr],
    query: QueryInput = None,
    *,
    group_by: Sequence[GroupByExpr] | None = None,
    base_select: Select[Any] | None = None,
    policy: QueryPolicy | None = None,
) -> PageResult[dict[str, Any]]:
    """Asynchronous counterpart of :func:`paginate_fields` for ``AsyncSession``."""
    return await compile_query_schema(model, policy=policy).apaginate_fields(
        session,
        fields,
        query,
        group_by=group_by,
        base_select=base_select,
    )


def search_select[M: BedrockModel](
    session: Session,
    model: type[M],
    stmt: Select[Any],
    query: QueryInput = None,
    *,
    limit: int | None = None,
    policy: QueryPolicy | None = None,
) -> list[dict[str, Any]]:
    """Execute an unpaginated query over an explicit SQLAlchemy ``Select``."""
    return compile_query_schema(model, policy=policy).search_select(
        session,
        stmt,
        query,
        limit=limit,
    )


async def asearch_select[M: BedrockModel](
    session: AsyncSession,
    model: type[M],
    stmt: Select[Any],
    query: QueryInput = None,
    *,
    limit: int | None = None,
    policy: QueryPolicy | None = None,
) -> list[dict[str, Any]]:
    """Asynchronous counterpart of :func:`search_select` for ``AsyncSession``."""
    return await compile_query_schema(model, policy=policy).asearch_select(
        session,
        stmt,
        query,
        limit=limit,
    )


def paginate_select[M: BedrockModel](
    session: Session,
    model: type[M],
    stmt: Select[Any],
    query: QueryInput = None,
    *,
    policy: QueryPolicy | None = None,
) -> PageResult[dict[str, Any]]:
    """Execute a paginated query over an explicit SQLAlchemy ``Select``."""
    return compile_query_schema(model, policy=policy).paginate_select(
        session,
        stmt,
        query,
    )


async def apaginate_select[M: BedrockModel](
    session: AsyncSession,
    model: type[M],
    stmt: Select[Any],
    query: QueryInput = None,
    *,
    policy: QueryPolicy | None = None,
) -> PageResult[dict[str, Any]]:
    """Asynchronous counterpart of :func:`paginate_select` for ``AsyncSession``."""
    return await compile_query_schema(model, policy=policy).apaginate_select(
        session,
        stmt,
        query,
    )
