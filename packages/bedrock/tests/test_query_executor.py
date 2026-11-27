"""Tests for QuerySchema facade and sync/async models, fields, and select executors."""

from __future__ import annotations

import pytest
import pytest_asyncio
from bedrock.database import (
    BedrockModel,
    FieldPolicy,
    PageResult,
    QueryPolicy,
    apaginate_fields,
    apaginate_models,
    apaginate_select,
    asearch_fields,
    asearch_models,
    asearch_select,
    compile_query_schema,
    paginate_fields,
    paginate_models,
    paginate_select,
    search_fields,
    search_models,
    search_select,
)
from bedrock.exc import BadFilterFormatError, InvalidQueryLimitError
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import aliased, relationship, selectinload, sessionmaker


class ExecUser(BedrockModel):
    __tablename__ = "exec_users"
    __filterable_fields__ = ["name", "country", "orders"]

    id = Column(Integer, primary_key=True)
    name = Column(String(60), nullable=False)
    country = Column(String(20), nullable=False)
    password_hash = Column(String(120), nullable=False)
    orders = relationship("ExecOrder", foreign_keys="ExecOrder.creator_id", back_populates="creator")


class ExecOrder(BedrockModel):
    __tablename__ = "exec_orders"
    __searchable_columns__ = ["code"]
    __search_op__ = "like"
    __filterable_fields__ = ["code", "status", "currency", "total", "tenant_id", "creator", "approver"]

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    code = Column(String(50), nullable=False)
    status = Column(String(30), nullable=False)
    currency = Column(String(10), nullable=False)
    total = Column(Integer, nullable=False)
    creator_id = Column(Integer, ForeignKey("exec_users.id"), nullable=False)
    approver_id = Column(Integer, ForeignKey("exec_users.id"), nullable=True)

    creator = relationship(ExecUser, foreign_keys=[creator_id], back_populates="orders")
    approver = relationship(ExecUser, foreign_keys=[approver_id])


def _seed_rows() -> list[BedrockModel]:
    alice = ExecUser(name="Alice", country="US", password_hash="h1")
    bob = ExecUser(name="Bob", country="DE", password_hash="h2")
    carol = ExecUser(name="Carol", country="US", password_hash="h3")
    return [
        alice,
        bob,
        carol,
        ExecOrder(tenant_id=1, code="ORD-1", status="paid", currency="USD", total=100, creator=alice, approver=carol),
        ExecOrder(tenant_id=1, code="ORD-2", status="paid", currency="USD", total=200, creator=alice, approver=None),
        ExecOrder(tenant_id=1, code="ORD-3", status="draft", currency="EUR", total=50, creator=bob, approver=carol),
        ExecOrder(tenant_id=2, code="ORD-4", status="paid", currency="USD", total=999, creator=bob, approver=alice),
    ]


@pytest.fixture
def sync_session():
    engine = create_engine("sqlite:///:memory:")
    BedrockModel.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all(_seed_rows())
    session.commit()
    try:
        yield session
    finally:
        session.close()
        BedrockModel.metadata.drop_all(engine)
        engine.dispose()


@pytest_asyncio.fixture
async def async_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'exec.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(BedrockModel.metadata.create_all)
    session = async_sessionmaker(engine)()
    session.add_all(_seed_rows())
    await session.commit()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


class TestModelExecutors:
    """Verify search_models / paginate_models and async twins."""

    def test_paginate_models_counts_distinct_parents_on_one_to_many_exists(self, sync_session):
        # Alice has 2 paid orders; EXISTS must count Alice once (not twice)
        page = paginate_models(
            sync_session,
            ExecUser,
            query={
                "filter": {
                    "relation": "orders",
                    "quantifier": "some",
                    "filter": {"field": "status", "op": "eq", "value": "paid"},
                },
                "sort": [{"field": "name", "direction": "asc"}],
                "pagination": {"page": 1, "limit": 1},
            },
        )
        assert isinstance(page, PageResult)
        assert page.total == 2  # Alice and Bob (each has >=1 paid order)
        assert [u.name for u in page.items] == ["Alice"]
        assert page.page_info.has_more is True

    def test_search_models_ignores_valid_pagination_and_rejects_invalid_pagination(self, sync_session):
        # Same client payload with pagination limit=1 returns all rows in unpaginated search_models
        items = search_models(
            sync_session,
            ExecOrder,
            query={
                "filter": {"field": "status", "op": "eq", "value": "paid"},
                "sort": [{"field": "total", "direction": "asc"}],
                "pagination": {"page": 1, "limit": 1},
            },
            base_select=select(ExecOrder).where(ExecOrder.tenant_id == 1),
            options=[selectinload(ExecOrder.creator)],
        )
        assert [o.code for o in items] == ["ORD-1", "ORD-2"]
        assert items[0].creator.name == "Alice"

        # Server-side explicit limit cap works
        capped = search_models(
            sync_session,
            ExecOrder,
            query={"sort": [{"field": "id", "direction": "asc"}]},
            limit=2,
        )
        assert len(capped) == 2

        # Invalid pagination inside query is still rejected
        with pytest.raises(InvalidQueryLimitError):
            search_models(sync_session, ExecOrder, query={"pagination": {"page": 0, "limit": 10}})

    def test_default_schema_cache_reuses_compiled_instance(self):
        s1 = compile_query_schema(ExecOrder)
        s2 = compile_query_schema(ExecOrder)
        custom = compile_query_schema(ExecOrder, policy=QueryPolicy(max_limit=50))
        assert s1 is s2
        assert custom is not s1

    @pytest.mark.asyncio
    async def test_async_model_search_and_paginate_with_selectinload(self, async_session: AsyncSession):
        page = await apaginate_models(
            async_session,
            ExecOrder,
            query={
                "filter": {"field": "creator.name", "op": "eq", "value": "Alice"},
                "sort": [{"field": "total", "direction": "desc"}],
                "pagination": {"page": 1, "limit": 10},
            },
            options=[selectinload(ExecOrder.creator)],
        )
        assert page.total == 2
        assert [o.code for o in page.items] == ["ORD-2", "ORD-1"]
        assert page.items[0].creator.name == "Alice"

        items = await asearch_models(
            async_session,
            ExecOrder,
            query={"search": "ORD-3"},
        )
        assert [o.code for o in items] == ["ORD-3"]


class TestFieldProjectionExecutors:
    """Verify search_fields / paginate_fields with multi-column group_by and dict rows."""

    def test_fields_projection_and_multi_column_group_by_pagination(self, sync_session):
        projected = search_fields(
            sync_session,
            ExecOrder,
            fields=["code", "total"],
            query={
                "filter": {"field": "tenant_id", "op": "eq", "value": 1},
                "sort": [{"field": "total", "direction": "desc"}],
            },
        )
        assert projected == [
            {"code": "ORD-2", "total": 200},
            {"code": "ORD-1", "total": 100},
            {"code": "ORD-3", "total": 50},
        ]

        agg_policy = QueryPolicy(
            sort_expressions={"gmv": func.sum(ExecOrder.total)},
        )
        grouped_page = paginate_fields(
            sync_session,
            ExecOrder,
            fields=[
                ExecOrder.status,
                ExecOrder.currency,
                func.count(ExecOrder.id).label("cnt"),
                func.sum(ExecOrder.total).label("gmv"),
            ],
            group_by=["status", "currency"],
            query={
                "filter": {"field": "tenant_id", "op": "eq", "value": 1},
                "sort": [{"field": "gmv", "direction": "desc"}],
                "pagination": {"page": 1, "limit": 1},
            },
            policy=agg_policy,
        )
        assert grouped_page.total == 2
        assert grouped_page.items == [{"status": "paid", "currency": "USD", "cnt": 2, "gmv": 300}]
        assert grouped_page.page_info.has_more is True

    def test_fields_rejects_empty_and_unallowlisted_string_field(self, sync_session):
        with pytest.raises(BadFilterFormatError, match="at least one"):
            search_fields(sync_session, ExecOrder, fields=[])

        with pytest.raises(BadFilterFormatError, match="not allowed"):
            search_fields(sync_session, ExecUser, fields=["password_hash"])

    @pytest.mark.asyncio
    async def test_async_fields_search_and_paginate(self, async_session: AsyncSession):
        rows = await asearch_fields(
            async_session,
            ExecOrder,
            fields=["code", "status"],
            query={"filter": {"field": "tenant_id", "op": "eq", "value": 2}},
        )
        assert rows == [{"code": "ORD-4", "status": "paid"}]

        page = await apaginate_fields(
            async_session,
            ExecOrder,
            fields=[ExecOrder.status, func.count(ExecOrder.id).label("cnt")],
            group_by=[ExecOrder.status],
            query={"pagination": {"page": 1, "limit": 10}},
        )
        assert page.total == 2


class TestCustomSelectExecutors:
    """Verify search_select / paginate_select with explicit JOIN, aliased(), and extra_fields."""

    def test_cross_table_join_with_aliased_and_report_schema(self, sync_session):
        Approver = aliased(ExecUser)
        stmt = (
            select(
                ExecOrder.code,
                ExecOrder.total,
                ExecUser.name.label("creator_name"),
                Approver.name.label("approver_name"),
            )
            .select_from(ExecOrder)
            .join(ExecUser, ExecOrder.creator_id == ExecUser.id)
            .outerjoin(Approver, ExecOrder.approver_id == Approver.id)
            .where(ExecOrder.tenant_id == 1)
        )
        report_policy = QueryPolicy(
            extra_fields={
                "creator_name": FieldPolicy(column=ExecUser.name, sortable=True),
                "approver_name": FieldPolicy(column=Approver.name, nullable=True, sortable=True),
            }
        )

        page = paginate_select(
            sync_session,
            ExecOrder,
            stmt,
            query={
                "filter": {"field": "approver_name", "op": "is_not_null"},
                "sort": [{"field": "creator_name", "direction": "asc"}, {"field": "total", "direction": "desc"}],
                "pagination": {"page": 1, "limit": 10},
            },
            policy=report_policy,
        )
        assert page.total == 2
        assert page.items == [
            {"code": "ORD-1", "total": 100, "creator_name": "Alice", "approver_name": "Carol"},
            {"code": "ORD-3", "total": 50, "creator_name": "Bob", "approver_name": "Carol"},
        ]

        all_rows = search_select(
            sync_session,
            ExecOrder,
            stmt,
            query={
                "filter": {"field": "creator_name", "op": "eq", "value": "Alice"},
                "sort": [{"field": "total", "direction": "asc"}],
                "pagination": {"page": 1, "limit": 1},  # ignored in search_select
            },
            policy=report_policy,
        )
        assert len(all_rows) == 2

    @pytest.mark.asyncio
    async def test_async_select_cross_table_group_by_aggregation(self, async_session: AsyncSession):
        agg_stmt = (
            select(
                ExecUser.country,
                ExecOrder.status,
                func.sum(ExecOrder.total).label("gmv"),
            )
            .select_from(ExecOrder)
            .join(ExecUser, ExecOrder.creator_id == ExecUser.id)
            .group_by(ExecUser.country, ExecOrder.status)
        )
        report_policy = QueryPolicy(
            extra_fields={"country": FieldPolicy(column=ExecUser.country, sortable=True)},
            sort_expressions={"gmv": func.sum(ExecOrder.total)},
        )

        page = await apaginate_select(
            async_session,
            ExecOrder,
            agg_stmt,
            query={
                "filter": {"field": "status", "op": "eq", "value": "paid"},
                "sort": [{"field": "gmv", "direction": "desc"}],
                "pagination": {"page": 1, "limit": 10},
            },
            policy=report_policy,
        )
        assert page.total == 2
        assert page.items == [
            {"country": "DE", "status": "paid", "gmv": 999},
            {"country": "US", "status": "paid", "gmv": 300},
        ]

        rows = await asearch_select(
            async_session,
            ExecOrder,
            agg_stmt,
            query={"filter": {"field": "country", "op": "eq", "value": "US"}},
            policy=report_policy,
        )
        assert rows == [{"country": "US", "status": "paid", "gmv": 300}]
