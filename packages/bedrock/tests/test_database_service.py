"""Tests for database query helpers built around filter_specs."""

from __future__ import annotations

import pytest
from bedrock.database.base import BedrockModel
from bedrock.database.service import search_filter_sort_paginate
from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for query helper tests."""


class InventoryItem(Base):
    """Minimal model used to exercise filter_specs query behavior."""

    __tablename__ = "inventory_items"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    quantity = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False)


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


class SearchableItem(BedrockModel):
    """Model with searchable columns configured via __searchable_columns__."""

    __tablename__ = "searchable_items"
    __searchable_columns__ = ["name", "category"]
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
