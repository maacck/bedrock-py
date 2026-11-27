"""Tests for pure SQLAlchemy 2.0 WHERE / EXISTS / ORDER BY clause compilation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from bedrock.database.base import BedrockModel
from bedrock.database.query.compiler import compile_order_by, compile_where
from bedrock.database.query.policy import FieldPolicy, QueryPolicy, resolve_model_policy
from bedrock.database.query.schema import CompiledQueryValidator
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import relationship, sessionmaker


class CompCompany(BedrockModel):
    __tablename__ = "comp_companies"
    __filterable_fields__ = ["name"]

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)


class CompOwner(BedrockModel):
    __tablename__ = "comp_owners"
    __filterable_fields__ = ["name", "company"]

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    company_id = Column(Integer, ForeignKey("comp_companies.id"), nullable=True)
    company = relationship(CompCompany)


class CompOrder(BedrockModel):
    __tablename__ = "comp_orders"
    __searchable_columns__ = ["title"]
    __search_op__ = "like"
    __filterable_fields__ = ["title", "total", "created_at", "aware_at", "owner", "lines"]

    id = Column(Integer, primary_key=True)
    title = Column(String(120), nullable=False)
    total = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=False), nullable=True)
    aware_at = Column(DateTime(timezone=True), nullable=True)
    owner_id = Column(Integer, ForeignKey("comp_owners.id"), nullable=True)
    owner = relationship(CompOwner, backref="orders")
    lines = relationship("CompLine", back_populates="order")


class CompLine(BedrockModel):
    __tablename__ = "comp_lines"
    __filterable_fields__ = ["sku", "qty", "tag"]

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("comp_orders.id"), nullable=False)
    sku = Column(String(50), nullable=False)
    qty = Column(Integer, nullable=False)
    tag = Column(String(50), nullable=True)
    order = relationship(CompOrder, back_populates="lines")


@pytest.fixture
def comp_session():
    engine = create_engine("sqlite:///:memory:")
    BedrockModel.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    acme = CompCompany(name="Acme Corp")
    globex = CompCompany(name="Globex")
    ann = CompOwner(name="Ann", company=acme)
    bob = CompOwner(name="Bob", company=globex)

    t1 = datetime(2026, 3, 1, 10, 0, 0)
    t2 = datetime(2026, 6, 1, 10, 0, 0)

    o1 = CompOrder(title="100% Widget", total=50, created_at=t1, aware_at=t1.replace(tzinfo=UTC), owner=ann)
    o2 = CompOrder(title="a_b Gadget", total=150, created_at=t2, aware_at=t2.replace(tzinfo=UTC), owner=bob)
    o3 = CompOrder(title="Orphan Order", total=None, created_at=None, aware_at=None, owner=None)

    session.add_all(
        [
            acme,
            globex,
            ann,
            bob,
            o1,
            o2,
            o3,
            CompLine(order=o1, sku="A-1", qty=5, tag="ok"),
            CompLine(order=o1, sku="A-2", qty=10, tag="ok"),
            CompLine(order=o2, sku="B-1", qty=1, tag=None),
        ]
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        BedrockModel.metadata.drop_all(engine)
        engine.dispose()


class TestCompilerSqlSemantics:
    """Verify compiled clauses produce accurate SQL with zero automatic JOINs."""

    def test_dotted_to_one_and_cross_relation_or_never_drops_rows_or_emits_join(self, comp_session):
        resolved = resolve_model_policy(CompOrder)
        validator = CompiledQueryValidator(resolved)
        spec = validator.validate_python(
            {
                "filter": {
                    "logic": "or",
                    "conditions": [
                        {"field": "title", "op": "eq", "value": "Orphan Order"},
                        {"field": "owner.company.name", "op": "eq", "value": "Acme Corp"},
                    ],
                },
                "sort": [{"field": "id", "direction": "asc"}],
            }
        )

        where_clause = compile_where(spec, resolved)
        order_clauses = compile_order_by(spec, resolved)
        stmt = select(CompOrder).where(where_clause).order_by(*order_clauses)

        compiled_sql = str(stmt.compile())
        assert " JOIN " not in compiled_sql.upper()
        assert "EXISTS" in compiled_sql.upper()

        rows = comp_session.scalars(stmt).all()
        assert [r.title for r in rows] == ["100% Widget", "Orphan Order"]

    def test_collection_quantifiers_some_none_and_every(self, comp_session):
        resolved = resolve_model_policy(CompOrder)
        validator = CompiledQueryValidator(resolved)

        def _run(filter_payload: dict[str, object]) -> list[str]:
            spec = validator.validate_python({"filter": filter_payload, "sort": [{"field": "id", "direction": "asc"}]})
            stmt = select(CompOrder).where(compile_where(spec, resolved)).order_by(*compile_order_by(spec, resolved))
            return [r.title for r in comp_session.scalars(stmt).all()]

        # some: has a line with qty >= 10 -> only o1
        assert _run(
            {"relation": "lines", "quantifier": "some", "filter": {"field": "qty", "op": "gte", "value": 10}}
        ) == ["100% Widget"]
        # none (without filter): orders with no lines -> o3
        assert _run({"relation": "lines", "quantifier": "none"}) == ["Orphan Order"]
        # every: all lines have qty >= 5 -> o1 (5, 10) and o3 (empty set vacuous truth)
        assert _run(
            {"relation": "lines", "quantifier": "every", "filter": {"field": "qty", "op": "gte", "value": 5}}
        ) == [
            "100% Widget",
            "Orphan Order",
        ]
        # every on nullable column: NULL child value must NOT falsely satisfy predicate (SQL 3VL)
        assert _run(
            {"relation": "lines", "quantifier": "every", "filter": {"field": "tag", "op": "eq", "value": "ok"}}
        ) == [
            "100% Widget",
            "Orphan Order",
        ]
        # scalar missing: orders without owner -> o3
        assert _run({"relation": "owner", "quantifier": "missing"}) == ["Orphan Order"]

    def test_wildcard_escaping_and_datetime_timezone_alignment(self, comp_session):
        resolved = resolve_model_policy(CompOrder)
        validator = CompiledQueryValidator(resolved)

        # Literal "%" and "_" must not act as wildcards
        spec_pct = validator.validate_python({"filter": {"field": "title", "op": "contains", "value": "100%"}})
        assert [r.title for r in comp_session.scalars(select(CompOrder).where(compile_where(spec_pct, resolved)))] == [
            "100% Widget"
        ]

        spec_us = validator.validate_python({"filter": {"field": "title", "op": "starts_with", "value": "a_b"}})
        assert [r.title for r in comp_session.scalars(select(CompOrder).where(compile_where(spec_us, resolved)))] == [
            "a_b Gadget"
        ]

        # Aware ISO input (+02:00) normalized to UTC naive for naive column created_at
        tz_plus2 = timezone(timedelta(hours=2))
        spec_dt = validator.validate_python(
            {
                "filter": {
                    "field": "created_at",
                    "op": "between",
                    "value": [
                        datetime(2026, 3, 1, 11, 0, 0, tzinfo=tz_plus2).isoformat(),  # 09:00Z
                        datetime(2026, 3, 1, 13, 0, 0, tzinfo=tz_plus2).isoformat(),  # 11:00Z
                    ],
                }
            }
        )
        assert [r.title for r in comp_session.scalars(select(CompOrder).where(compile_where(spec_dt, resolved)))] == [
            "100% Widget"
        ]

        # is_null on DateTime column works without crashing
        spec_null = validator.validate_python({"filter": {"field": "created_at", "op": "is_null"}})
        assert [r.title for r in comp_session.scalars(select(CompOrder).where(compile_where(spec_null, resolved)))] == [
            "Orphan Order"
        ]

    def test_multi_field_sort_with_nulls_and_custom_sort_expression(self, comp_session):
        owner_name_subq = (
            select(CompOwner.name).where(CompOwner.id == CompOrder.owner_id).correlate(CompOrder).scalar_subquery()
        )
        resolved = resolve_model_policy(
            CompOrder,
            policy=QueryPolicy(
                fields={"title": FieldPolicy(sortable=True)},
                sort_expressions={"owner_name": owner_name_subq},
            ),
        )
        validator = CompiledQueryValidator(resolved)
        spec = validator.validate_python(
            {
                "sort": [
                    {"field": "total", "direction": "desc", "nulls": "last"},
                    {"field": "id", "direction": "asc"},
                ]
            }
        )
        stmt = select(CompOrder).order_by(*compile_order_by(spec, resolved))
        assert [r.title for r in comp_session.scalars(stmt).all()] == ["a_b Gadget", "100% Widget", "Orphan Order"]

        spec_subq = validator.validate_python({"sort": [{"field": "owner_name", "direction": "asc", "nulls": "first"}]})
        stmt2 = select(CompOrder).order_by(*compile_order_by(spec_subq, resolved))
        assert [r.title for r in comp_session.scalars(stmt2).all()] == ["Orphan Order", "100% Widget", "a_b Gadget"]
