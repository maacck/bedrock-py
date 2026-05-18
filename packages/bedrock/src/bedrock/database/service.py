"""Database query helpers for filtering, sorting, and pagination."""

from typing import Any

from sqlalchemy import (
    Select,
    and_,
    true,
)
from sqlalchemy import (
    func as sa_func,
)
from sqlalchemy import (
    select as sa_select,
)
from sqlalchemy.orm.session import Session
from sqlalchemy.sql.elements import ColumnElement

from bedrock.database.base import BedrockModel

from ..logging import get_logger
from .filters import Field, Filter, init_filters

logger = get_logger(__name__)

FilterSpec = dict[str, Any]
FilterSpecs = FilterSpec | list[FilterSpec]


def build_filters(model: type[BedrockModel], filters: list[Filter]) -> ColumnElement[bool]:
    """Combine a list of Filter objects into a single SQLAlchemy AND clause.

    Args:
        model: The SQLAlchemy model class used to resolve field names.
        filters: Pre-built :class:`Filter` instances to combine.

    Returns:
        A SQLAlchemy ``AND`` clause, or a no-op ``TRUE`` when *filters* is empty.
    """
    if not filters:
        return true()
    _filters = []
    for filter_ in filters:
        _filters.append(filter_.format_for_sqlalchemy(model))
    return and_(*_filters)


def build_auto_joins(model: type[BedrockModel], filters: list[Filter]) -> list[tuple[Any, ...]]:
    """Collect distinct join arguments required by the given filters.

    Iterates over each filter, extracts the join arguments needed to
    resolve dotted field paths, and deduplicates them while preserving
    insertion order.

    Args:
        model: The SQLAlchemy model class used to resolve relationships.
        filters: Pre-built :class:`Filter` instances to inspect.

    Returns:
        A list of unique join argument tuples suitable for
        ``Query.join(*join_arg)``.
    """
    auto_joins = []
    for filter_ in filters:
        join_args = filter_.get_join_args(model=model)
        for join_arg in join_args:
            if join_arg is not None and join_arg not in auto_joins:
                auto_joins.append(join_arg)
    return auto_joins


def build_query(
    *,
    select: Select,
    model: type[BedrockModel],
    limit: int = 10,
    page: int = 1,
    sort_dir: str | None = None,
    q: str | None = None,
    sort_key: str | None = None,
    sqla_filters: list[ColumnElement[bool]] | None = None,
    filter_specs: FilterSpecs | None = None,
    join_models: list[tuple[Any, ...]] | None = None,
    options: list[Any] | None = None,
    show_all: bool = False,
    count_pk: str | None = None,
    group_by: str | None = None,
    auto_join_: bool = True,
) -> tuple[Select, Select, list[ColumnElement[bool]]]:
    """Build a filtered, sorted, paginated SQLAlchemy query pair.

    Constructs both a data query and a corresponding count query from the
    provided filter specifications, sort configuration, and pagination
    parameters.

    Args:
        select: Base :class:`Select` statement to build upon.
        model: The SQLAlchemy model class being queried.
        limit: Maximum number of results per page.
        page: 1-based page number.
        sort_dir: Sort direction — ``"asc"`` or ``"desc"``.
        q: Optional free-text search string.
        sort_key: Column name or list of ``(column, direction)`` tuples
            for ordering.
        sqla_filters: Additional raw SQLAlchemy filter expressions.
        filter_specs: Declarative filter specifications processed by
            :func:`init_filters`.
        join_models: Explicit join specifications (tuples of model classes
            and optional ``isouter`` flag).
        options: SQLAlchemy loader options (e.g. ``selectinload``).
        show_all: When ``True``, ignore *limit* and *page*.
        count_pk: Primary-key column name for the count query. Defaults
            to the model's first primary key.
        group_by: Column name or expression to group results by.
        auto_join_: When ``True``, automatically join related models
            required by filter field paths.

    Returns:
        A tuple of ``(data_query, count_query, filters)`` where *filters*
        is the combined list of active filter clauses.
    """
    sort_by = None
    offset = (page - 1) * limit if page > 1 else 0
    filter_specs = filter_specs or []
    filters = init_filters(model, filter_specs)
    auto_joins = []
    if auto_join_:
        auto_joins = build_auto_joins(model, filters)
    join_models = join_models or []
    filters = [build_filters(model, filters)]
    if q is not None and q:
        filters.append(model.text_search(q))
    if sqla_filters is not None:
        filters.extend(sqla_filters)
    if sort_key is not None:
        if isinstance(sort_key, str):
            sort_key = [(sort_key, sort_dir)]
        sort_by = []
        for _sort_key, _sort_dir in sort_key:
            sort_field = Field(model, _sort_key).get_sqlalchemy_field()
            if _sort_dir == "desc":
                sort_by.append(sort_field.desc())
            else:
                sort_by.append(sort_field.asc())
    query = select
    count_query = sa_select(sa_func.count(getattr(model, count_pk if count_pk else model.get_primary_keys()[0])))
    for j in join_models:
        if isinstance(j, tuple):
            if len(j) > 2:
                # get isouter as index 2
                query = query.join(*j[:2], isouter=j[2])
                count_query = count_query.join(*j[:2], isouter=j[2])
            else:
                query = query.join(*j)
                count_query = count_query.join(*j)
    for j in auto_joins:
        if j not in join_models:
            query = query.join(*j)
            count_query = count_query.join(*j)
    if options is not None:
        for option in options:
            query = query.options(option)
    if sort_by is not None:
        query = query.order_by(*sort_by)
    if group_by is not None:
        if isinstance(group_by, str):
            group_by_clause = Field(model, group_by).get_sqlalchemy_field()
        else:
            group_by_clause = group_by
        query = query.group_by(group_by_clause)
        count_query = count_query.group_by(group_by_clause)

    if limit > 0 and not show_all:
        query = query.offset(offset).limit(limit)

    count_query = count_query.where(and_(*filters))
    query = query.where(and_(*filters))
    return query, count_query, filters


def search_filter_sort_paginate(
    *,
    db_session: Session,
    model: type[BedrockModel],
    limit: int = 10,
    page: int = 1,
    sort_dir: str | None = None,
    q: str | None = None,
    sort_key: str | None = None,
    sqla_filters: list[ColumnElement[bool]] | None = None,
    filter_specs: FilterSpecs | None = None,
    join_models: list[tuple[Any, ...]] | None = None,
    options: list[Any] | None = None,
    additional_select: list[Any] | None = None,
    show_all: bool = False,
    count_pk: str | None = None,
    return_raw: bool = False,
    group_by: str | None = None,
    auto_join_: bool = True,
) -> dict[str, Any]:
    """Execute a search with filtering, sorting, and pagination in one call.

    High-level convenience that delegates to :func:`build_query` and runs
    the resulting statements against the provided session.

    Args:
        db_session: Active SQLAlchemy session.
        model: The SQLAlchemy model class (or selectable) to query.
        limit: Maximum number of results per page.
        page: 1-based page number.
        sort_dir: Sort direction — ``"asc"`` or ``"desc"``.
        q: Optional free-text search string.
        sort_key: Column name or list of ``(column, direction)`` tuples.
        sqla_filters: Additional raw SQLAlchemy filter expressions.
        filter_specs: Declarative filter specifications.
        join_models: Explicit join specifications.
        options: SQLAlchemy loader options.
        additional_select: Extra selectables to include alongside the
            model (e.g. computed columns).
        show_all: When ``True``, ignore *limit* and *page*.
        count_pk: Primary-key column name for the count query.
        return_raw: When ``True``, return full row objects instead of
            scalar results.
        group_by: Column name or expression to group results by.
        auto_join_: When ``True``, automatically join related models.

    Returns:
        A dictionary with keys ``"items"``, ``"total"``, and
        ``"page_info"`` containing the query results and pagination
        metadata.
    """
    select_fields = [model]
    if additional_select is not None:
        select_fields.extend(additional_select)
    offset = (page - 1) * limit if page > 1 else 0
    query, count_query, filters = build_query(
        select=sa_select(*select_fields),
        model=model,
        limit=limit,
        page=page,
        sort_dir=sort_dir,
        q=q,
        sort_key=sort_key,
        sqla_filters=sqla_filters,
        filter_specs=filter_specs,
        join_models=join_models,
        options=options,
        count_pk=count_pk,
        group_by=group_by,
        auto_join_=auto_join_,
        show_all=show_all,
    )

    total_count: int = db_session.execute(count_query).scalar()
    items = db_session.execute(query)
    if return_raw:
        items = items.all()
    else:
        items = items.scalars().all()

    return {
        "items": items,
        "total": total_count,
        "page_info": {
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "page": page,
            "query": q or "",
            "filters": filter_specs or [],
            "paginated": not show_all,
            "has_more": total_count > offset + limit,
        },
    }
