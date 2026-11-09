"""Tests for database query helpers built around filter_specs."""

from __future__ import annotations

import pytest
import pytest_asyncio
from bedrock.database.base import BedrockModel
from bedrock.database.filters import MAX_FILTER_DEPTH, init_filters
from bedrock.database.service import MAX_QUERY_LIMIT, asearch_filter_sort_paginate, search_filter_sort_paginate
from bedrock.exc import BadFilterFormatError, FilterDepthExceededError, InvalidQueryLimitError
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for query helper tests."""


class InventoryItem(Base):
    """Minimal model used to exercise filter_specs query behavior."""

    __tablename__ = "inventory_items"
    __filterable_fields__ = ["name", "category", "quantity", "is_active", "created_at"]

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False)
    created_at = Column(DateTime, nullable=True)


class Owner(Base):
    """Related model whose secret column must stay out of client filters."""

    __tablename__ = "owners"
    __filterable_fields__ = ["name"]

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    password_hash = Column(String(100), nullable=False)


class Order(Base):
    """Model exposing one relationship and one column to client filters."""

    __tablename__ = "orders"
    __filterable_fields__ = ["owner", "total"]

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("owners.id"), nullable=False)
    total = Column(Integer, nullable=False)
    internal_note = Column(String(100), nullable=True)
    owner = relationship(Owner, backref="orders")


class Note(Base):
    """Model that declares no ``__filterable_fields__``."""

    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    body = Column(String(100), nullable=False)


class OpenOrder(Base):
    """Model that opts out of the allowlist because its caller validates field names."""

    __tablename__ = "open_orders"
    __filterable_fields__ = "__all__"

    id = Column(Integer, primary_key=True)
    owner_id = Column(Integer, ForeignKey("owners.id"), nullable=False)
    internal_note = Column(String(100), nullable=True)
    owner = relationship(Owner)


@pytest.fixture
def db_engine():
    """Create an in-memory SQLite engine with local test tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session_maker(db_engine):
    """Return a sessionmaker bound to the local SQLite engine."""
    return sessionmaker(bind=db_engine)


@pytest.fixture
def db_session(session_maker):
    """Seed a session with representative rows for query helper tests."""
    session = session_maker()
    session.add_all(
        [
            InventoryItem(name="alpha", category="fruit", quantity=5, is_active=True),
            InventoryItem(name="bravo", category="vegetable", quantity=3, is_active=True),
            InventoryItem(name="charlie", category="fruit", quantity=2, is_active=False),
            InventoryItem(name="delta", category="grain", quantity=9, is_active=True),
            InventoryItem(name="echo", category="fruit", quantity=7, is_active=False),
        ]
    )
    session.commit()

    try:
        yield session
    finally:
        session.close()


class TestSearchFilterSortPaginate:
    """Verify search_filter_sort_paginate behavior exposed through filter_specs."""

    def test_filter_specs_apply_filters_and_sorting(self, db_session):
        """Multiple filter specs should narrow the result set before sorting."""
        filter_specs = [
            {"field": "category", "op": "eq", "value": "fruit"},
            {"field": "quantity", "op": "ge", "value": 5},
        ]

        result = search_filter_sort_paginate(
            db_session=db_session,
            model=InventoryItem,
            filter_specs=filter_specs,
            sort_key="quantity",
            sort_dir="desc",
            limit=10,
            count_pk="id",
        )

        assert [item.name for item in result["items"]] == ["echo", "alpha"]
        assert result["total"] == 2
        assert result["page_info"]["filters"] == filter_specs
        assert result["page_info"]["has_more"] is False

    def test_filter_specs_work_with_pagination(self, db_session):
        """Pagination should be applied after filtering and sorting."""
        filter_specs = [{"field": "category", "op": "eq", "value": "fruit"}]

        result = search_filter_sort_paginate(
            db_session=db_session,
            model=InventoryItem,
            filter_specs=filter_specs,
            sort_key="name",
            sort_dir="asc",
            limit=2,
            page=2,
            count_pk="id",
        )

        assert [item.name for item in result["items"]] == ["echo"]
        assert result["total"] == 3
        assert result["page_info"] == {
            "total": 3,
            "limit": 2,
            "offset": 2,
            "page": 2,
            "query": "",
            "filters": filter_specs,
            "paginated": True,
            "has_more": False,
        }

    def test_filter_specs_support_boolean_logic_and_boolean_values(self, db_session):
        """Boolean filter specs should support OR logic and boolean column comparisons."""
        filter_specs = [
            {
                "or": [
                    {"field": "is_active", "op": "eq", "value": False},
                    {"field": "quantity", "op": "gt", "value": 8},
                ]
            }
        ]

        result = search_filter_sort_paginate(
            db_session=db_session,
            model=InventoryItem,
            filter_specs=filter_specs,
            sort_key="id",
            sort_dir="asc",
            limit=10,
            count_pk="id",
        )

        assert [item.name for item in result["items"]] == ["charlie", "delta", "echo"]
        assert result["total"] == 3

    def test_group_by_total_counts_groups(self, db_session):
        """With group_by, total is the number of groups, and an empty result is 0 rather than None."""
        grouped = search_filter_sort_paginate(
            db_session=db_session,
            model=InventoryItem,
            filter_specs={"field": "is_active", "op": "eq", "value": True},
            group_by="category",
            count_pk="id",
            limit=2,
            return_raw=True,
        )
        empty = search_filter_sort_paginate(
            db_session=db_session,
            model=InventoryItem,
            filter_specs={"field": "category", "op": "eq", "value": "none"},
            group_by="category",
            count_pk="id",
        )

        assert len(grouped["items"]) == 2
        assert grouped["total"] == 3
        assert grouped["page_info"]["has_more"] is True
        assert empty["total"] == 0
        assert empty["page_info"]["has_more"] is False


class SearchableItem(BedrockModel):
    """Model with searchable columns configured via __searchable_columns__."""

    __tablename__ = "searchable_items"
    __searchable_columns__ = ["name", "category"]
    __filterable_fields__ = ["name", "category", "quantity", "tags"]
    __search_op__ = "like"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)


class CustomOpItem(BedrockModel):
    """Model that overrides the default search operator to ``like``."""

    __tablename__ = "custom_op_items"
    __searchable_columns__ = ["label"]
    __search_op__ = "like"

    id = Column(Integer, primary_key=True)
    label = Column(String(100), nullable=False)


class UnsearchableItem(BedrockModel):
    """Model with no __searchable_columns__ — text_search must be a no-op."""

    __tablename__ = "unsearchable_items"

    id = Column(Integer, primary_key=True)
    value = Column(String(100), nullable=False)


class SearchTag(BedrockModel):
    """Related searchable model used to exercise relationship text search."""

    __tablename__ = "search_tags"
    __searchable_columns__ = ["label"]
    __search_op__ = "like"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("searchable_items.id"), nullable=False)
    label = Column(String(100), nullable=False)


SearchableItem.tags = relationship("SearchTag", backref="item")


@pytest.fixture
def search_engine():
    engine = create_engine("sqlite:///:memory:")
    BedrockModel.metadata.create_all(engine)
    yield engine
    BedrockModel.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def search_session(search_engine):
    session = sessionmaker(bind=search_engine)()
    session.add_all(
        [
            SearchableItem(name="Alpha Widget", category="tools", quantity=10),
            SearchableItem(name="Beta Gadget", category="gadgets", quantity=5),
            SearchableItem(name="Gamma Tool", category="tools", quantity=3),
            CustomOpItem(label="hello world"),
            CustomOpItem(label="goodbye world"),
            UnsearchableItem(value="something"),
        ]
    )
    session.commit()
    alpha = session.query(SearchableItem).filter_by(name="Alpha Widget").one()
    beta = session.query(SearchableItem).filter_by(name="Beta Gadget").one()
    session.add_all([SearchTag(item=alpha, label="featured widget"), SearchTag(item=beta, label="gadget")])
    session.commit()
    try:
        yield session
    finally:
        session.close()


class TestTextSearch:
    """Verify BedrockModel.text_search and q-based filtering."""

    def test_q_filters_searchable_columns(self, search_session):
        result = search_filter_sort_paginate(
            db_session=search_session,
            model=SearchableItem,
            q="Widget",
            limit=10,
            count_pk="id",
        )
        assert [item.name for item in result["items"]] == ["Alpha Widget"]
        assert result["total"] == 1

    def test_q_searches_across_multiple_columns(self, search_session):
        result = search_filter_sort_paginate(
            db_session=search_session,
            model=SearchableItem,
            q="gadgets",
            limit=10,
            count_pk="id",
        )
        assert [item.name for item in result["items"]] == ["Beta Gadget"]
        assert result["total"] == 1

    def test_q_noop_when_no_searchable_columns(self, search_session):
        result = search_filter_sort_paginate(
            db_session=search_session,
            model=UnsearchableItem,
            q="something",
            limit=10,
            count_pk="id",
        )
        assert result["total"] == 1
        assert result["items"][0].value == "something"

    def test_q_with_custom_operator(self, search_session):
        result = search_filter_sort_paginate(
            db_session=search_session,
            model=CustomOpItem,
            q="hello",
            limit=10,
            count_pk="id",
        )
        assert [item.label for item in result["items"]] == ["hello world"]
        assert result["total"] == 1

    def test_q_no_match_returns_empty(self, search_session):
        result = search_filter_sort_paginate(
            db_session=search_session,
            model=SearchableItem,
            q="nonexistent",
            limit=10,
            count_pk="id",
        )
        assert result["total"] == 0
        assert result["items"] == []

    def test_relationship_text_search_supports_positive_and_negative_filters(self, search_session):
        """Relationship text search must retain its special handling under negation."""
        positive = search_filter_sort_paginate(
            db_session=search_session,
            model=SearchableItem,
            filter_specs={"field": "tags", "op": "text_search", "value": "widget"},
            sort_key="name",
            limit=10,
            count_pk="id",
        )
        negative = search_filter_sort_paginate(
            db_session=search_session,
            model=SearchableItem,
            filter_specs={"field": "!tags", "op": "text_search", "value": "widget"},
            sort_key="name",
            limit=10,
            count_pk="id",
        )

        assert [item.name for item in positive["items"]] == ["Alpha Widget"]
        assert [item.name for item in negative["items"]] == ["Beta Gadget", "Gamma Tool"]


class TestFilterInputValidation:
    """Verify untrusted filter and pagination inputs raise public errors."""

    @pytest.mark.parametrize(
        "filter_specs",
        [
            {"field": 1, "op": "eq", "value": "fruit"},
            {"field": "name", "op": "fuzzy_search", "value": 1},
            {"field": "created_at", "op": "eq", "value": "not-a-date"},
            {"field": "created_at", "op": "eq", "value": 10**18},
        ],
    )
    def test_malformed_filter_values_are_normalized(self, db_session, filter_specs):
        with pytest.raises(BadFilterFormatError):
            search_filter_sort_paginate(
                db_session=db_session,
                model=InventoryItem,
                filter_specs=filter_specs,
                limit=10,
                count_pk="id",
            )

    def test_text_search_requires_string_value(self, search_session):
        with pytest.raises(BadFilterFormatError):
            search_filter_sort_paginate(
                db_session=search_session,
                model=SearchableItem,
                filter_specs={"field": "tags", "op": "text_search", "value": 1},
                limit=10,
                count_pk="id",
            )

    def test_filter_tree_depth_is_bounded(self):
        filter_spec = {"field": "name", "op": "eq", "value": "alpha"}
        for _ in range(MAX_FILTER_DEPTH + 10):
            filter_spec = {"and": [filter_spec]}

        with pytest.raises(FilterDepthExceededError):
            init_filters(InventoryItem, filter_spec)

    def test_filter_tree_list_nesting_is_bounded(self):
        filter_spec: object = {"field": "name", "op": "eq", "value": "alpha"}
        for _ in range(MAX_FILTER_DEPTH + 10):
            filter_spec = [filter_spec]

        with pytest.raises(FilterDepthExceededError):
            init_filters(InventoryItem, filter_spec)

    @pytest.mark.parametrize("limit", [-1, 0, True, "10", MAX_QUERY_LIMIT + 1])
    def test_limit_must_be_a_bounded_positive_integer(self, db_session, limit):
        with pytest.raises(InvalidQueryLimitError):
            search_filter_sort_paginate(
                db_session=db_session,
                model=InventoryItem,
                limit=limit,
                count_pk="id",
            )

    def test_maximum_limit_is_accepted(self, db_session):
        result = search_filter_sort_paginate(
            db_session=db_session,
            model=InventoryItem,
            limit=MAX_QUERY_LIMIT,
            count_pk="id",
        )

        assert result["page_info"]["limit"] == MAX_QUERY_LIMIT


class TestFilterableFields:
    """Client-controlled field names only reach primary keys and ``__filterable_fields__``."""

    def test_allowlisted_relationship_path_filters(self, db_session):
        db_session.add(Order(owner=Owner(name="ann", password_hash="s3cr"), total=5))
        db_session.commit()

        result = search_filter_sort_paginate(
            db_session=db_session,
            model=Order,
            filter_specs={"field": "owner.name", "op": "eq", "value": "ann"},
            sort_key="total",
            count_pk="id",
        )

        assert result["total"] == 1

    @pytest.mark.parametrize(
        ("model", "kwargs"),
        [
            (Order, {"filter_specs": {"field": "owner.password_hash", "op": "like", "value": "s%"}}),
            (Order, {"filter_specs": {"field": "owner:password_hash", "op": "eq", "value": "s3cr"}}),
            (Order, {"filter_specs": {"field": "internal_note", "op": "eq", "value": "x"}}),
            (Order, {"sort_key": "internal_note"}),
            (Order, {"group_by": "internal_note"}),
            (Order, {"count_pk": "internal_note"}),
            (Owner, {"filter_specs": {"field": "orders.total", "op": "eq", "value": 5}}),
            (Note, {"filter_specs": {"field": "body", "op": "eq", "value": "x"}}),
        ],
    )
    def test_fields_outside_allowlist_are_rejected(self, db_session, model, kwargs):
        with pytest.raises(BadFilterFormatError, match="not filterable"):
            search_filter_sort_paginate(db_session=db_session, model=model, **{"count_pk": "id", **kwargs})

    def test_primary_key_is_filterable_without_declaration(self, db_session):
        db_session.add(Note(body="hidden"))
        db_session.commit()

        result = search_filter_sort_paginate(
            db_session=db_session,
            model=Note,
            filter_specs={"field": "id", "op": "eq", "value": 1},
            count_pk="id",
        )

        assert result["total"] == 1

    def test_missing_field_is_indistinguishable_from_hidden_field(self, db_session):
        with pytest.raises(BadFilterFormatError, match="not filterable"):
            search_filter_sort_paginate(
                db_session=db_session,
                model=Order,
                filter_specs={"field": "no_such_column", "op": "eq", "value": "x"},
                count_pk="id",
            )

    def test_all_opts_the_model_out_of_the_allowlist(self, db_session):
        db_session.add(OpenOrder(owner=Owner(name="ann", password_hash="s3cr"), internal_note="rush"))
        db_session.commit()

        result = search_filter_sort_paginate(
            db_session=db_session,
            model=OpenOrder,
            filter_specs={"field": "internal_note", "op": "eq", "value": "rush"},
            sort_key="owner_id",
            count_pk="id",
        )

        assert result["total"] == 1

    def test_all_does_not_open_related_models(self, db_session):
        with pytest.raises(BadFilterFormatError, match="not filterable on Owner"):
            search_filter_sort_paginate(
                db_session=db_session,
                model=OpenOrder,
                filter_specs={"field": "owner.password_hash", "op": "like", "value": "s%"},
                count_pk="id",
            )


@pytest_asyncio.fixture
async def async_session(tmp_path):
    """Seed an aiosqlite session with the same rows as ``db_session``."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'paginate.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine)()
    session.add_all(
        [
            InventoryItem(name="alpha", category="fruit", quantity=5, is_active=True),
            InventoryItem(name="bravo", category="vegetable", quantity=3, is_active=True),
            InventoryItem(name="charlie", category="fruit", quantity=2, is_active=False),
            InventoryItem(name="delta", category="grain", quantity=9, is_active=True),
            InventoryItem(name="echo", category="fruit", quantity=7, is_active=False),
        ]
    )
    await session.commit()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


class TestAsyncSearchFilterSortPaginate:
    """Verify the AsyncSession variant returns the same shape as the sync helper."""

    @pytest.mark.asyncio
    async def test_filters_sorts_and_paginates(self, async_session: AsyncSession):
        filter_specs = [{"field": "category", "op": "eq", "value": "fruit"}]

        result = await asearch_filter_sort_paginate(
            db_session=async_session,
            model=InventoryItem,
            filter_specs=filter_specs,
            sort_key="name",
            sort_dir="asc",
            limit=2,
            page=1,
            count_pk="id",
        )

        assert [item.name for item in result["items"]] == ["alpha", "charlie"]
        assert result["total"] == 3
        assert result["page_info"] == {
            "total": 3,
            "limit": 2,
            "offset": 0,
            "page": 1,
            "query": "",
            "filters": filter_specs,
            "paginated": True,
            "has_more": True,
        }

    @pytest.mark.asyncio
    async def test_return_raw_includes_additional_select(self, async_session: AsyncSession):
        result = await asearch_filter_sort_paginate(
            db_session=async_session,
            model=InventoryItem,
            additional_select=[InventoryItem.quantity * 10],
            filter_specs={"field": "name", "op": "eq", "value": "delta"},
            return_raw=True,
            count_pk="id",
        )

        [(item, scaled)] = result["items"]
        assert (item.name, scaled) == ("delta", 90)
