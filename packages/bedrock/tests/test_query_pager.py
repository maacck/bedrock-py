"""Tests for extensible BasePager, PagePager, LimitOffsetPager, CursorPager, and custom pagers."""

from __future__ import annotations

import base64
import enum
import json
import warnings
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from bedrock.database import (
    BasePageInfo,
    BasePager,
    BedrockModel,
    CursorPageInfo,
    CursorPager,
    CursorParams,
    FieldPolicy,
    LimitOffsetPageInfo,
    LimitOffsetPager,
    LimitOffsetParams,
    PageInfo,
    PagePager,
    PageResult,
    PaginationContext,
    PaginationParams,
    PaginationSpec,
    QueryPolicy,
    QuerySchema,
    QuerySpec,
    apaginate_fields,
    apaginate_models,
    apaginate_select,
    compile_query_schema,
    paginate_fields,
    paginate_models,
    paginate_select,
    search_models,
)
from bedrock.exc import BadFilterFormatError, InvalidCursorError, InvalidQueryLimitError
from pydantic import Field, StrictInt
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    Select,
    String,
    Uuid,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, deferred, load_only, relationship, sessionmaker


class PagerStatus(enum.StrEnum):
    OPEN = "open"
    PAID = "paid"


class PagerAccount(BedrockModel):
    __tablename__ = "pager_accounts"
    __filterable_fields__ = ["name"]

    id = Column(Integer, primary_key=True)
    name = Column(String(60), nullable=False)


class PagerEvent(BedrockModel):
    __tablename__ = "pager_events"
    __filterable_fields__ = [
        "code",
        "score",
        "is_active",
        "weight",
        "amount",
        "status",
        "external_id",
        "created_at",
        "deferred_rank",
        "account",
    ]

    id = Column(Integer, primary_key=True)
    code = Column(String(40), nullable=False)
    score = Column(Integer, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    weight = Column(Float, nullable=True)
    amount = Column(Numeric(10, 2), nullable=False)
    status = Column(SAEnum(PagerStatus), nullable=False)
    external_id = Column(Uuid, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    deferred_rank = deferred(Column(Integer, nullable=False, default=0))
    account_id = Column(Integer, ForeignKey("pager_accounts.id"), nullable=False)
    account = relationship(PagerAccount)


class CompositePkItem(BedrockModel):
    __tablename__ = "pager_composite_items"
    __filterable_fields__ = ["label"]

    tenant_id = Column(Integer, primary_key=True)
    seq_no = Column(Integer, primary_key=True)
    label = Column(String(40), nullable=False)


def _seed_rows() -> tuple[list[PagerAccount], list[PagerEvent], list[CompositePkItem]]:
    acc_a = PagerAccount(id=1, name="Alpha")
    acc_b = PagerAccount(id=2, name="Beta")
    same_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    earlier_ts = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    later_ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

    events = [
        PagerEvent(
            id=1,
            code="E-1",
            score=10,
            is_active=False,
            weight=1.5,
            amount=Decimal("10.50"),
            status=PagerStatus.OPEN,
            external_id=UUID("00000000-0000-0000-0000-000000000001"),
            created_at=same_ts,
            deferred_rank=30,
            account_id=1,
        ),
        PagerEvent(
            id=2,
            code="E-2",
            score=10,
            is_active=True,
            weight=2.5,
            amount=Decimal("20.00"),
            status=PagerStatus.PAID,
            external_id=UUID("00000000-0000-0000-0000-000000000002"),
            created_at=same_ts,
            deferred_rank=10,
            account_id=1,
        ),
        PagerEvent(
            id=3,
            code="E-3",
            score=20,
            is_active=False,
            weight=3.5,
            amount=Decimal("30.25"),
            status=PagerStatus.PAID,
            external_id=UUID("00000000-0000-0000-0000-000000000003"),
            created_at=same_ts,
            deferred_rank=50,
            account_id=2,
        ),
        PagerEvent(
            id=4,
            code="E-4",
            score=None,
            is_active=True,
            weight=None,
            amount=Decimal("40.00"),
            status=PagerStatus.OPEN,
            external_id=UUID("00000000-0000-0000-0000-000000000004"),
            created_at=later_ts,
            deferred_rank=20,
            account_id=2,
        ),
        PagerEvent(
            id=5,
            code="E-5",
            score=None,
            is_active=True,
            weight=5.5,
            amount=Decimal("50.75"),
            status=PagerStatus.PAID,
            external_id=UUID("00000000-0000-0000-0000-000000000005"),
            created_at=earlier_ts,
            deferred_rank=40,
            account_id=1,
        ),
    ]
    composites = [
        CompositePkItem(tenant_id=1, seq_no=1, label="t1-1"),
        CompositePkItem(tenant_id=1, seq_no=2, label="t1-2"),
        CompositePkItem(tenant_id=2, seq_no=1, label="t2-1"),
        CompositePkItem(tenant_id=2, seq_no=2, label="t2-2"),
    ]
    return [acc_a, acc_b], events, composites


@pytest.fixture
def pager_sync_session():
    engine = create_engine("sqlite:///:memory:")
    BedrockModel.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    accounts, events, composites = _seed_rows()
    session.add_all(accounts)
    session.flush()
    session.add_all(events)
    session.add_all(composites)
    session.commit()
    try:
        yield session
    finally:
        session.close()
        BedrockModel.metadata.drop_all(engine)
        engine.dispose()


@pytest_asyncio.fixture
async def pager_async_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(BedrockModel.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        accounts, events, composites = _seed_rows()
        session.add_all(accounts)
        await session.flush()
        session.add_all(events)
        session.add_all(composites)
        await session.commit()
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(BedrockModel.metadata.drop_all)
    await engine.dispose()


class WindowParams(PaginationParams):
    """External custom pagination params inheriting default `limit`."""

    window: StrictInt = Field(default=0, ge=0)


class WindowPageInfo(BasePageInfo):
    """External custom page_info subclass."""

    window: int


class WindowPager(BasePager[WindowParams, WindowPageInfo]):
    """External custom pager subclass using default build_params_schema()."""

    params_model = WindowParams
    page_info_model = WindowPageInfo

    def apply(self, stmt: Select[Any], ctx: PaginationContext[Any, WindowParams]) -> Select[Any]:
        out = stmt.order_by(*ctx.order_clauses) if ctx.order_clauses else stmt
        offset = ctx.params.window * ctx.params.limit
        return out.fetch(None).offset(offset).limit(ctx.params.limit)

    def _assemble[T](self, items: list[T], total: int, params: WindowParams) -> PageResult[T]:
        offset = params.window * params.limit
        return PageResult[T](
            items=items,
            total=total,
            page_info=WindowPageInfo(
                total=total,
                limit=params.limit,
                has_more=total > offset + params.limit,
                window=params.window,
            ),
        )

    def paginate[T](self, session: Session, ctx: PaginationContext[T, WindowParams]) -> PageResult[T]:
        total = ctx.fetch_total(session)
        items = ctx.fetch_rows(session, self.apply(ctx.filtered_stmt, ctx))
        return self._assemble(items, total, ctx.params)

    async def apaginate[T](self, session: AsyncSession, ctx: PaginationContext[T, WindowParams]) -> PageResult[T]:
        total = await ctx.afetch_total(session)
        items = await ctx.afetch_rows(session, self.apply(ctx.filtered_stmt, ctx))
        return self._assemble(items, total, ctx.params)


class TestLimitOffsetPager:
    """Verify LimitOffsetPager schema, total toggle, sync/async execution, and apply()."""

    def test_schema_and_validation(self):
        schema = QuerySchema(
            PagerEvent,
            policy=QueryPolicy(default_limit=5, max_limit=20),
            pager=LimitOffsetPager(),
        )
        exported = json.dumps(schema.json_schema())
        assert '"offset"' in exported
        assert '"limit"' in exported
        assert '"page"' not in exported

        spec = schema.validate({"pagination": {"offset": 2}})
        assert isinstance(spec.pagination, LimitOffsetParams)
        assert spec.pagination.offset == 2
        assert spec.pagination.limit == 5

        with pytest.raises(InvalidQueryLimitError):
            schema.validate({"pagination": {"offset": -1, "limit": 5}})
        with pytest.raises(InvalidQueryLimitError):
            schema.validate({"pagination": {"offset": True, "limit": 5}})
        with pytest.raises(InvalidQueryLimitError):
            schema.validate({"pagination": {"page": 1, "limit": 5}})
        with pytest.raises(InvalidQueryLimitError):
            schema.validate({"pagination": {"offset": 0, "limit": 21}})

    def test_sync_with_and_without_total_and_apply(self, pager_sync_session):
        schema = compile_query_schema(PagerEvent, pager=LimitOffsetPager())
        page1 = schema.paginate(
            pager_sync_session,
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"offset": 1, "limit": 2}},
        )
        assert [e.code for e in page1.items] == ["E-2", "E-3"]
        assert page1.total == 5
        assert isinstance(page1.page_info, LimitOffsetPageInfo)
        assert page1.page_info.total == 5
        assert page1.page_info.offset == 1
        assert page1.page_info.limit == 2
        assert page1.page_info.has_more is True

        # apply() emits OFFSET and LIMIT
        stmt = schema.apply(
            select(PagerEvent),
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"offset": 3, "limit": 2}},
        )
        applied_rows = list(pager_sync_session.scalars(stmt).all())
        assert [e.code for e in applied_rows] == ["E-4", "E-5"]

        # include_total=False skips COUNT(*) completely
        executed_sql: list[str] = []

        def _capture(_conn, _cursor, statement, *_args):
            executed_sql.append(statement)

        event.listen(pager_sync_session.bind, "before_cursor_execute", _capture)
        try:
            fast_page = paginate_models(
                pager_sync_session,
                PagerEvent,
                query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"offset": 3, "limit": 2}},
                pager=LimitOffsetPager(include_total=False),
            )
        finally:
            event.remove(pager_sync_session.bind, "before_cursor_execute", _capture)

        assert [e.code for e in fast_page.items] == ["E-4", "E-5"]
        assert fast_page.total is None
        assert fast_page.page_info.total is None
        assert fast_page.page_info.has_more is False
        assert not any("count(" in sql.lower() for sql in executed_sql)

    @pytest.mark.asyncio
    async def test_async_models_fields_and_select(self, pager_async_session: AsyncSession):
        pager = LimitOffsetPager()
        model_page = await apaginate_models(
            pager_async_session,
            PagerEvent,
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"offset": 2, "limit": 2}},
            pager=pager,
        )
        assert [e.code for e in model_page.items] == ["E-3", "E-4"]
        assert model_page.total == 5
        assert model_page.page_info.has_more is True

        fields_page = await apaginate_fields(
            pager_async_session,
            PagerEvent,
            fields=["code"],
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"offset": 4, "limit": 2}},
            pager=pager,
        )
        assert fields_page.items == [{"code": "E-5"}]
        assert fields_page.total == 5
        assert fields_page.page_info.has_more is False

        join_stmt = (
            select(PagerEvent.code, PagerAccount.name.label("account_name"))
            .select_from(PagerEvent)
            .join(PagerAccount, PagerEvent.account_id == PagerAccount.id)
        )
        select_page = await apaginate_select(
            pager_async_session,
            PagerEvent,
            join_stmt,
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"offset": 0, "limit": 2}},
            pager=LimitOffsetPager(include_total=False),
        )
        assert select_page.total is None
        assert select_page.page_info.has_more is True
        assert select_page.items == [
            {"code": "E-1", "account_name": "Alpha"},
            {"code": "E-2", "account_name": "Alpha"},
        ]


class TestCursorPager:
    """Verify forward CursorPager PK tie-breakers, NULL handling, total toggle, and error checks."""

    def test_schema_and_validation(self):
        schema = QuerySchema(
            PagerEvent,
            policy=QueryPolicy(default_limit=3, max_limit=10),
            pager=CursorPager(),
        )
        exported = json.dumps(schema.json_schema())
        assert '"cursor"' in exported
        assert '"limit"' in exported
        assert '"page"' not in exported

        spec = schema.validate({"pagination": {"limit": 2}})
        assert isinstance(spec.pagination, CursorParams)
        assert spec.pagination.cursor is None
        assert spec.pagination.limit == 2

        with pytest.raises(InvalidCursorError):
            schema.validate({"pagination": {"cursor": 123, "limit": 2}})
        with pytest.raises(InvalidQueryLimitError):
            schema.validate({"pagination": {"limit": 11}})

    def test_duplicate_created_at_sort_appends_pk_tie_breaker_without_skips_or_dupes(self, pager_sync_session):
        schema = QuerySchema(PagerEvent, pager=CursorPager())
        seen_codes: list[str] = []
        cursor: str | None = None
        pages = 0

        while True:
            page = schema.paginate(
                pager_sync_session,
                query={
                    "sort": [{"field": "created_at", "direction": "desc"}],
                    "pagination": {"cursor": cursor, "limit": 2},
                },
            )
            pages += 1
            assert page.total == 5
            assert isinstance(page.page_info, CursorPageInfo)
            assert page.page_info.total == 5
            seen_codes.extend(e.code for e in page.items)
            if not page.page_info.has_more:
                assert page.page_info.next_cursor is None
                break
            assert page.page_info.next_cursor is not None
            cursor = page.page_info.next_cursor

        # E-4 (June), then E-1/E-2/E-3 all share May 1 (ordered by PK asc: 1, 2, 3), then E-5 (April)
        assert seen_codes == ["E-4", "E-1", "E-2", "E-3", "E-5"]
        assert pages == 3

    def test_nullable_sort_column_transitions_cleanly_with_nulls_last_and_nulls_first(self, pager_sync_session):
        schema = QuerySchema(PagerEvent, pager=CursorPager())

        # Default NULLS LAST for nullable column `score` ascending
        asc_codes: list[str] = []
        cursor: str | None = None
        while True:
            page = schema.paginate(
                pager_sync_session,
                query={
                    "sort": [{"field": "score", "direction": "asc"}],
                    "pagination": {"cursor": cursor, "limit": 2},
                },
            )
            asc_codes.extend(e.code for e in page.items)
            if not page.page_info.has_more:
                break
            cursor = page.page_info.next_cursor  # type: ignore[attr-defined]
        assert asc_codes == ["E-1", "E-2", "E-3", "E-4", "E-5"]

        # Explicit NULLS FIRST with DESC
        desc_first_codes: list[str] = []
        cursor = None
        while True:
            page = schema.paginate(
                pager_sync_session,
                query={
                    "sort": [{"field": "score", "direction": "desc", "nulls": "first"}],
                    "pagination": {"cursor": cursor, "limit": 2},
                },
            )
            desc_first_codes.extend(e.code for e in page.items)
            if not page.page_info.has_more:
                break
            cursor = page.page_info.next_cursor  # type: ignore[attr-defined]
        assert desc_first_codes == ["E-4", "E-5", "E-3", "E-1", "E-2"]

    def test_composite_pk_default_sort_and_scalar_types(self, pager_sync_session):
        comp_schema = QuerySchema(CompositePkItem, pager=CursorPager())
        labels: list[str] = []
        cursor: str | None = None
        while True:
            page = comp_schema.paginate(
                pager_sync_session,
                query={"pagination": {"cursor": cursor, "limit": 2}},
            )
            labels.extend(item.label for item in page.items)
            if not page.page_info.has_more:
                break
            cursor = page.page_info.next_cursor  # type: ignore[attr-defined]
        assert labels == ["t1-1", "t1-2", "t2-1", "t2-2"]

        # Decimal, Enum, and UUID sort keys round-trip through cursor tokens cleanly
        for sort_field in ("amount", "status", "external_id"):
            ev_schema = QuerySchema(PagerEvent, pager=CursorPager())
            collected: list[int] = []
            cursor = None
            while True:
                p = ev_schema.paginate(
                    pager_sync_session,
                    query={
                        "sort": [{"field": sort_field, "direction": "asc"}],
                        "pagination": {"cursor": cursor, "limit": 2},
                    },
                )
                collected.extend(item.id for item in p.items)
                if not p.page_info.has_more:
                    break
                cursor = p.page_info.next_cursor  # type: ignore[attr-defined]
            assert len(collected) == 5
            assert set(collected) == {1, 2, 3, 4, 5}

    def test_include_total_false_skips_count_and_apply_filters_keyset(self, pager_sync_session):
        schema = QuerySchema(PagerEvent, pager=CursorPager(include_total=False))
        executed_sql: list[str] = []

        def _capture(_conn, _cursor, statement, *_args):
            executed_sql.append(statement)

        event.listen(pager_sync_session.bind, "before_cursor_execute", _capture)
        try:
            page1 = schema.paginate(
                pager_sync_session,
                query={"pagination": {"limit": 2}},
            )
            assert page1.total is None
            assert page1.page_info.total is None
            assert page1.page_info.has_more is True
            assert [e.code for e in page1.items] == ["E-1", "E-2"]
        finally:
            event.remove(pager_sync_session.bind, "before_cursor_execute", _capture)

        assert not any("count(" in sql.lower() for sql in executed_sql)

        # QuerySchema.apply() applies keyset WHERE + PK ORDER BY + LIMIT
        next_cursor = page1.page_info.next_cursor  # type: ignore[attr-defined]
        stmt = schema.apply(select(PagerEvent), query={"pagination": {"cursor": next_cursor, "limit": 2}})
        applied = list(pager_sync_session.scalars(stmt).all())
        assert [e.code for e in applied] == ["E-3", "E-4"]

        # search_models reuses cursor query payload and ignores pagination
        exported = search_models(
            pager_sync_session,
            PagerEvent,
            query={"pagination": {"cursor": next_cursor, "limit": 2}},
            pager=CursorPager(),
        )
        assert len(exported) == 5

    def test_fields_and_select_strip_hidden_cursor_columns_and_reject_group_by(self, pager_sync_session):
        pager = CursorPager()
        page1 = paginate_fields(
            pager_sync_session,
            PagerEvent,
            fields=["code"],
            query={"sort": [{"field": "created_at", "direction": "desc"}], "pagination": {"limit": 2}},
            pager=pager,
        )
        assert page1.total == 5
        assert page1.items == [{"code": "E-4"}, {"code": "E-1"}]
        assert all(set(row.keys()) == {"code"} for row in page1.items)
        assert page1.page_info.has_more is True

        page2 = paginate_fields(
            pager_sync_session,
            PagerEvent,
            fields=["code"],
            query={
                "sort": [{"field": "created_at", "direction": "desc"}],
                "pagination": {"cursor": page1.page_info.next_cursor, "limit": 2},  # type: ignore[attr-defined]
            },
            pager=pager,
        )
        assert page2.items == [{"code": "E-2"}, {"code": "E-3"}]

        # Cross-table paginate_select with extra_fields sort
        join_stmt = (
            select(PagerEvent.code, PagerAccount.name.label("account_name"))
            .select_from(PagerEvent)
            .join(PagerAccount, PagerEvent.account_id == PagerAccount.id)
        )
        join_policy = QueryPolicy(extra_fields={"account_name": FieldPolicy(column=PagerAccount.name, sortable=True)})
        sel1 = paginate_select(
            pager_sync_session,
            PagerEvent,
            join_stmt,
            query={"sort": [{"field": "account_name", "direction": "desc"}], "pagination": {"limit": 2}},
            policy=join_policy,
            pager=pager,
        )
        assert sel1.items == [
            {"code": "E-3", "account_name": "Beta"},
            {"code": "E-4", "account_name": "Beta"},
        ]
        sel2 = paginate_select(
            pager_sync_session,
            PagerEvent,
            join_stmt,
            query={
                "sort": [{"field": "account_name", "direction": "desc"}],
                "pagination": {"cursor": sel1.page_info.next_cursor, "limit": 2},  # type: ignore[attr-defined]
            },
            policy=join_policy,
            pager=pager,
        )
        assert sel2.items == [
            {"code": "E-1", "account_name": "Alpha"},
            {"code": "E-2", "account_name": "Alpha"},
        ]

        # group_by is explicitly rejected by CursorPager
        with pytest.raises(BadFilterFormatError, match="group_by"):
            paginate_fields(
                pager_sync_session,
                PagerEvent,
                fields=["status"],
                group_by=["status"],
                pager=pager,
            )

    def test_invalid_tampered_and_mismatched_sort_cursors_raise_invalid_cursor_error(self, pager_sync_session):
        schema = QuerySchema(PagerEvent, pager=CursorPager())
        page1 = schema.paginate(
            pager_sync_session,
            query={"sort": [{"field": "created_at", "direction": "desc"}], "pagination": {"limit": 2}},
        )
        valid_cursor = page1.page_info.next_cursor  # type: ignore[attr-defined]
        assert valid_cursor is not None

        # Mismatched sort direction
        with pytest.raises(InvalidCursorError, match="sort order"):
            schema.paginate(
                pager_sync_session,
                query={
                    "sort": [{"field": "created_at", "direction": "asc"}],
                    "pagination": {"cursor": valid_cursor, "limit": 2},
                },
            )

        # Malformed tokens
        bad_tokens = [
            "",
            "!!!not-base64!!!",
            base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode("ascii"),
            base64.urlsafe_b64encode(b'{"wrong": 1}').rstrip(b"=").decode("ascii"),
            base64.urlsafe_b64encode(b'{"v": [1], "s": "wrong"}').rstrip(b"=").decode("ascii"),
        ]
        for token in bad_tokens:
            with pytest.raises(InvalidCursorError):
                schema.paginate(pager_sync_session, query={"pagination": {"cursor": token, "limit": 2}})

    @pytest.mark.asyncio
    async def test_async_cursor_pagination(self, pager_async_session: AsyncSession):
        pager = CursorPager()
        collected: list[str] = []
        cursor: str | None = None
        while True:
            page = await apaginate_models(
                pager_async_session,
                PagerEvent,
                query={
                    "sort": [{"field": "created_at", "direction": "desc"}],
                    "pagination": {"cursor": cursor, "limit": 2},
                },
                pager=pager,
            )
            assert page.total == 5
            collected.extend(e.code for e in page.items)
            if not page.page_info.has_more:
                break
            cursor = page.page_info.next_cursor  # type: ignore[attr-defined]
        assert collected == ["E-4", "E-1", "E-2", "E-3", "E-5"]

        fields_page = await apaginate_fields(
            pager_async_session,
            PagerEvent,
            fields=["code"],
            query={"pagination": {"limit": 2}},
            pager=CursorPager(include_total=False),
        )
        assert fields_page.total is None
        assert fields_page.items == [{"code": "E-1"}, {"code": "E-2"}]
        assert fields_page.page_info.has_more is True


class TestCustomExternalPager:
    """Verify external BasePager subclasses work seamlessly with JSON Schema, serialization, and FastAPI."""

    def test_custom_pager_schema_execution_and_serialization(self, pager_sync_session):
        schema = QuerySchema(
            PagerEvent,
            policy=QueryPolicy(default_limit=2, max_limit=10),
            pager=WindowPager(),
        )
        assert isinstance(schema.pager, WindowPager)
        assert isinstance(compile_query_schema(PagerEvent).pager, PagePager)

        exported = json.dumps(schema.json_schema())
        assert '"window"' in exported
        assert '"limit"' in exported
        assert '"page"' not in exported

        page = schema.paginate(
            pager_sync_session,
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"window": 1}},
        )
        assert [e.code for e in page.items] == ["E-3", "E-4"]
        assert page.total == 5
        assert isinstance(page.page_info, WindowPageInfo)
        assert page.page_info.window == 1
        assert page.page_info.limit == 2
        assert page.page_info.has_more is True

        # SerializeAsAny preserves custom page_info fields with zero warnings
        fields_page = schema.paginate_fields(
            pager_sync_session,
            fields=["code"],
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"window": 2, "limit": 2}},
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            dumped = fields_page.model_dump()
            dumped_json = fields_page.model_dump_json()

        assert caught == []
        assert dumped["page_info"] == {"total": 5, "limit": 2, "has_more": False, "window": 2}
        assert '"window":2' in dumped_json

        # Policy max_limit is enforced automatically on inherited `limit`
        with pytest.raises(InvalidQueryLimitError):
            schema.validate({"pagination": {"window": 0, "limit": 11}})

    @pytest.mark.asyncio
    async def test_custom_pager_async(self, pager_async_session: AsyncSession):
        page = await apaginate_models(
            pager_async_session,
            PagerEvent,
            query={"sort": [{"field": "id", "direction": "asc"}], "pagination": {"window": 1, "limit": 2}},
            pager=WindowPager(),
        )
        assert [e.code for e in page.items] == ["E-3", "E-4"]
        assert isinstance(page.page_info, WindowPageInfo)
        assert page.page_info.window == 1

    def test_fastapi_distinct_pagers_on_same_model_coexist_in_openapi(self):
        fastapi = pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        cursor_schema = QuerySchema(PagerEvent, policy=QueryPolicy(default_limit=2, max_limit=5), pager=CursorPager())
        window_schema = QuerySchema(PagerEvent, policy=QueryPolicy(default_limit=2, max_limit=5), pager=WindowPager())

        async def cursor_route(query: Any) -> dict[str, Any]:
            assert isinstance(query, QuerySpec)
            assert query.pagination is None or isinstance(query.pagination, CursorParams)
            return {"mode": "cursor", "limit": query.pagination.limit if query.pagination else None}

        async def window_route(query: Any) -> dict[str, Any]:
            assert isinstance(query, QuerySpec)
            assert query.pagination is None or isinstance(query.pagination, WindowParams)
            return {"mode": "window", "window": query.pagination.window if query.pagination else None}

        cursor_route.__annotations__["query"] = Annotated[QuerySpec, cursor_schema]
        window_route.__annotations__["query"] = Annotated[QuerySpec, window_schema]

        app = fastapi.FastAPI()
        app.post("/events/cursor")(cursor_route)
        app.post("/events/window")(window_route)

        openapi_text = json.dumps(app.openapi())
        assert '"cursor"' in openapi_text
        assert '"window"' in openapi_text

        client = TestClient(app, raise_server_exceptions=False)
        r1 = client.post("/events/cursor", json={"pagination": {"cursor": None, "limit": 3}})
        assert r1.status_code == 200
        assert r1.json() == {"mode": "cursor", "limit": 3}

        r2 = client.post("/events/window", json={"pagination": {"window": 2, "limit": 4}})
        assert r2.status_code == 200
        assert r2.json() == {"mode": "window", "window": 2}

        # Invalid pagination for cursor route returns 422 (never 500)
        bad_cursor = client.post("/events/cursor", json={"pagination": {"cursor": 123, "limit": 2}})
        assert bad_cursor.status_code == 422

        bad_window = client.post("/events/window", json={"pagination": {"window": 0, "limit": 99}})
        assert bad_window.status_code == 422

        # Silence unused import warning for uuid4/PageInfo
        assert uuid4() != UUID("00000000-0000-0000-0000-000000000001")
        assert PageInfo(total=1, page=1, limit=1, offset=0, has_more=False).page == 1
        assert PaginationSpec(page=1, limit=1).limit == 1

    def test_cursor_audit_regressions_boolean_decimal_distinct_aggregates_and_custom_params(self, pager_sync_session):
        # 1. Boolean column cursor pagination (ASC and DESC)
        bool_schema = QuerySchema(PagerEvent, pager=CursorPager())
        for direction, expected in [
            ("asc", ["E-1", "E-3", "E-2", "E-4", "E-5"]),
            ("desc", ["E-2", "E-4", "E-5", "E-1", "E-3"]),
        ]:
            codes: list[str] = []
            cursor: str | None = None
            while True:
                p = bool_schema.paginate(
                    pager_sync_session,
                    query={
                        "sort": [{"field": "is_active", "direction": direction}],
                        "pagination": {"cursor": cursor, "limit": 2},
                    },
                )
                codes.extend(e.code for e in p.items)
                if not p.page_info.has_more:
                    break
                cursor = p.page_info.next_cursor  # type: ignore[attr-defined]
            assert codes == expected

        # 2. Typed sort_expressions (Decimal arithmetic) + extra column in base_select
        expr_policy = QueryPolicy(sort_expressions={"adjusted": PagerEvent.amount + Decimal("0.50")})
        expr_schema = QuerySchema(PagerEvent, policy=expr_policy, pager=CursorPager())
        expr_codes: list[str] = []
        cursor = None
        while True:
            p = expr_schema.paginate(
                pager_sync_session,
                base_select=select(PagerEvent, PagerEvent.code.label("extra_c")),
                query={
                    "sort": [{"field": "adjusted", "direction": "asc"}],
                    "pagination": {"cursor": cursor, "limit": 2},
                },
            )
            expr_codes.extend(e.code for e in p.items)
            if not p.page_info.has_more:
                break
            cursor = p.page_info.next_cursor  # type: ignore[attr-defined]
        assert expr_codes == ["E-1", "E-2", "E-3", "E-4", "E-5"]

        # 3. DISTINCT and ungrouped aggregate projections rejected cleanly
        with pytest.raises(BadFilterFormatError, match="DISTINCT"):
            paginate_select(
                pager_sync_session,
                PagerEvent,
                select(PagerEvent.status).distinct(),
                pager=CursorPager(),
            )
        with pytest.raises(BadFilterFormatError, match="aggregate"):
            paginate_fields(
                pager_sync_session,
                PagerEvent,
                fields=[func.count(PagerEvent.id).label("cnt")],
                pager=CursorPager(),
            )

        # 4. Tampered Decimal cursors raise InvalidCursorError (never InvalidOperation)
        p1 = bool_schema.paginate(
            pager_sync_session,
            query={"sort": [{"field": "amount", "direction": "asc"}], "pagination": {"limit": 1}},
        )
        raw_p1 = json.loads(
            base64.urlsafe_b64decode(p1.page_info.next_cursor + "=" * (-len(p1.page_info.next_cursor) % 4))  # type: ignore[attr-defined]
        )
        fp = raw_p1["s"]
        for bad_dec in ("not-a-decimal", "NaN", "Infinity", {"x": 1}):
            tampered = base64.urlsafe_b64encode(json.dumps({"v": [bad_dec, 1], "s": fp}).encode()).rstrip(b"=").decode()
            with pytest.raises(InvalidCursorError):
                bool_schema.paginate(
                    pager_sync_session,
                    query={
                        "sort": [{"field": "amount", "direction": "asc"}],
                        "pagination": {"cursor": tampered, "limit": 1},
                    },
                )

        # 5. MySQL dialect emulates NULLS LAST without unsupported SQL syntax
        pager = CursorPager()
        ctx = bool_schema._model_pagination_ctx(
            {"sort": [{"field": "score", "direction": "asc"}]},
            base_select=None,
            options=None,
        )
        keys = pager._resolve_sort_keys(ctx)
        mysql_clauses = pager._compile_order_by(keys, dialect_name="mysql")
        compiled_mysql = ", ".join(str(c.compile(dialect=mysql.dialect())) for c in mysql_clauses)
        assert "NULLS LAST" not in compiled_mysql.upper()
        assert "IS NULL" in compiled_mysql.upper()

        # 6. Custom pager with required field and validated-data default_factory
        class RequiredShardParams(PaginationParams):
            shard: str
            computed_tag: str = Field(default_factory=lambda data: f"tag-{data.get('shard', 'none')}")

        class RequiredShardPager(BasePager[RequiredShardParams, BasePageInfo]):
            params_model = RequiredShardParams
            page_info_model = BasePageInfo

            def apply(self, stmt: Select[Any], ctx: PaginationContext[Any, RequiredShardParams]) -> Select[Any]:
                return stmt.limit(ctx.params.limit)

            def paginate[T](self, session: Session, ctx: PaginationContext[T, RequiredShardParams]) -> PageResult[T]:
                items = ctx.fetch_rows(session, self.apply(ctx.filtered_stmt, ctx))
                return PageResult[T](
                    items=items,
                    total=len(items),
                    page_info=BasePageInfo(total=len(items), limit=ctx.params.limit, has_more=False),
                )

            async def apaginate[T](
                self, session: AsyncSession, ctx: PaginationContext[T, RequiredShardParams]
            ) -> PageResult[T]:
                items = await ctx.afetch_rows(session, self.apply(ctx.filtered_stmt, ctx))
                return PageResult[T](
                    items=items,
                    total=len(items),
                    page_info=BasePageInfo(total=len(items), limit=ctx.params.limit, has_more=False),
                )

        shard_schema = QuerySchema(PagerEvent, pager=RequiredShardPager())
        parsed_shard = shard_schema.validate({"pagination": {"shard": "eu", "limit": 2}})
        assert isinstance(parsed_shard.pagination, RequiredShardParams)
        assert parsed_shard.pagination.computed_tag == "tag-eu"

        # Omitting pagination with a required field raises InvalidQueryLimitError (not raw ValidationError)
        with pytest.raises(InvalidQueryLimitError):
            shard_schema.paginate(pager_sync_session, query={})

    @pytest.mark.asyncio
    async def test_async_cursor_with_deferred_column_and_load_only_avoids_missing_greenlet(
        self, pager_async_session: AsyncSession
    ):
        schema = QuerySchema(PagerEvent, pager=CursorPager())
        ranks_seen: list[str] = []
        cursor: str | None = None
        while True:
            page = await schema.apaginate(
                pager_async_session,
                options=[load_only(PagerEvent.id, PagerEvent.code)],
                query={
                    "sort": [{"field": "deferred_rank", "direction": "asc"}],
                    "pagination": {"cursor": cursor, "limit": 2},
                },
            )
            ranks_seen.extend(e.code for e in page.items)
            if not page.page_info.has_more:
                break
            cursor = page.page_info.next_cursor  # type: ignore[attr-defined]
        assert ranks_seen == ["E-2", "E-4", "E-1", "E-5", "E-3"]
