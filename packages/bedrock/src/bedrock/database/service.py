from typing import Any

from sqlalchemy import (
    Select,
    and_,
)
from sqlalchemy import (
    func as sa_func,
)
from sqlalchemy import (
    select as sa_select,
)
from sqlalchemy.orm.session import Session

from bedrock.database.base import BedrockModel

from ..logging import get_logger
from .filters import Field, Filter, init_filters

logger = get_logger(__name__)


def build_filters(model, filters: list[Filter]):
    if not filters:
        return and_()
    _filters = []
    for filter_ in filters:
        _filters.append(filter_.format_for_sqlalchemy(model))
    return and_(*_filters)


def build_auto_joins(model, filters: list[Filter]):
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
    sort_dir: str = None,
    q: str | None = None,
    sort_key: str | None = None,
    sqla_filters: list | None = None,
    json_filters=None,
    join_models: list | None = None,
    options: list | None = None,
    show_all: bool = False,
    count_pk: str | None = None,
    group_by: str | None = None,
    auto_join_: bool = True,
):
    sort_by = None
    offset = (page - 1) * limit if page > 1 else 0
    json_filters = json_filters or []
    filters = init_filters(model, json_filters)
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
    count_query = sa_select(sa_func.count(getattr(model, count_pk if count_pk else model.pk_cols()[0])))
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

    elif hasattr(model, "_order_by") and model._order_by is not None:
        query = query.order_by(*model._order_by)
    if limit > 0 and not show_all:
        query = query.offset(offset).limit(limit)

    count_query = count_query.where(and_(*filters))
    query = query.where(and_(*filters))
    logger.debug("Query: %s", query.compile(compile_kwargs={"literal_binds": True}))
    return query, count_query, filters


def search_filter_sort_paginate(
    *,
    db_session: Session,
    model: Any,
    limit: int = 10,
    page: int = 1,
    sort_dir: str = None,
    q: str | None = None,
    sort_key: str | None = None,
    sqla_filters: list | None = None,
    json_filters=None,
    join_models: list | None = None,
    options: list | None = None,
    additional_select: list | None = None,
    show_all: bool = False,
    count_pk: str = None,
    return_raw: bool = False,
    group_by: str | None = None,
    auto_join_: bool = True,
):
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
        json_filters=json_filters,
        join_models=join_models,
        options=options,
        count_pk=count_pk,
        group_by=group_by,
        auto_join_=auto_join_,
        show_all=show_all,
    )

    total_count = db_session.execute(count_query).scalar()
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
            "filters": json_filters or [],
            "paginated": not show_all,
            "has_more": total_count > offset + limit,
        },
    }
