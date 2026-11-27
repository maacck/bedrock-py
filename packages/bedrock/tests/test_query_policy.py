"""Tests for QueryPolicy, FieldPolicy, and SQLAlchemy model metadata resolution."""

from __future__ import annotations

import enum

import pytest
from bedrock.database.base import BedrockModel
from bedrock.database.query.policy import (
    FieldPolicy,
    QueryPolicy,
    resolve_model_policy,
)
from bedrock.exc import BadFilterFormatError
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Time,
    Uuid,
    create_engine,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, column_property, relationship, sessionmaker


class StatusEnum(enum.StrEnum):
    DRAFT = "draft"
    PAID = "paid"


class PolicyBase(DeclarativeBase):
    """Isolated declarative base for policy resolution tests."""


class Company(PolicyBase):
    __tablename__ = "policy_companies"
    __filterable_fields__ = ["name"]

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    tax_secret = Column(String(100), nullable=False)


class Owner(PolicyBase):
    __tablename__ = "policy_owners"
    __filterable_fields__ = ["name", "company"]

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    password_hash = Column(String(100), nullable=False)
    company_id = Column(Integer, ForeignKey("policy_companies.id"), nullable=True)
    company = relationship(Company)


class RichOrder(PolicyBase):
    __tablename__ = "policy_orders"
    __filterable_fields__ = [
        "title",
        "amount",
        "ratio",
        "price",
        "is_paid",
        "status",
        "external_id",
        "created_at",
        "due_date",
        "cutoff_time",
        "owner",
        "items",
    ]
    __query_policy__ = QueryPolicy(
        fields={"title": FieldPolicy(allow_raw_like=True)},
    )

    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False)
    amount = Column(Integer, nullable=False)
    ratio = Column(Float, nullable=True)
    price = Column(Numeric(10, 2), nullable=False)
    is_paid = Column(Boolean, nullable=False)
    status = Column(SAEnum(StatusEnum), nullable=False)
    external_id = Column(Uuid, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    due_date = Column(Date, nullable=True)
    cutoff_time = Column(Time, nullable=True)
    internal_note = Column(String(200), nullable=True)
    owner_id = Column(Integer, ForeignKey("policy_owners.id"), nullable=True)
    owner = relationship(Owner, backref="orders")
    items = relationship("OrderLine", back_populates="order")

    @hybrid_property
    def label_code(self) -> str:
        return f"ORD-{self.id}"


class OrderLine(PolicyBase):
    __tablename__ = "policy_order_lines"
    __filterable_fields__ = ["sku"]

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("policy_orders.id"), nullable=False)
    sku = Column(String(50), nullable=False)
    order = relationship(RichOrder, back_populates="items")


class AllColumnsModel(PolicyBase):
    __tablename__ = "policy_all_cols"
    __filterable_fields__ = "__all__"

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    owner_id = Column(Integer, ForeignKey("policy_owners.id"), nullable=True)
    owner = relationship(Owner)


class CategoryNode(PolicyBase):
    __tablename__ = "policy_categories"
    __filterable_fields__ = ["name", "parent"]

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    parent_id = Column(Integer, ForeignKey("policy_categories.id"), nullable=True)
    parent = relationship("CategoryNode", remote_side=[id])


class ComputedPropertyModel(PolicyBase):
    __tablename__ = "policy_computed_props"
    __filterable_fields__ = "__all__"

    id = Column(Integer, primary_key=True)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    full_name = column_property(first_name + " " + last_name)


class EscapedSearchItem(BedrockModel):
    __tablename__ = "policy_escaped_search_items"
    __searchable_columns__ = ["name"]
    __search_op__ = "like"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)


class TestPolicyResolution:
    """Verify allowlist resolution, type inference, dotted paths, and safety."""

    def test_infers_types_operators_and_respects_allowlist(self):
        resolved = resolve_model_policy(RichOrder)

        assert "internal_note" not in resolved.fields
        assert "id" in resolved.fields
        assert resolved.fields["amount"].scalar_kind == "int"
        assert "is_null" not in resolved.fields["amount"].operators
        assert "is_null" in resolved.fields["ratio"].operators
        assert resolved.fields["price"].scalar_kind == "decimal"
        assert resolved.fields["is_paid"].operators == frozenset({"eq", "ne"})
        assert resolved.fields["status"].scalar_kind == "enum"
        assert resolved.fields["status"].enum_values == ("draft", "paid")
        assert resolved.fields["external_id"].scalar_kind == "uuid"
        assert resolved.fields["created_at"].scalar_kind == "datetime"
        assert resolved.fields["created_at"].datetime_timezone is True
        assert resolved.fields["due_date"].scalar_kind == "date"
        assert resolved.fields["cutoff_time"].scalar_kind == "time"
        # Model-level __query_policy__ enabled raw like on title
        assert "like" in resolved.fields["title"].operators
        assert "ilike" in resolved.fields["title"].operators

    def test_dotted_to_one_paths_collect_safely_and_exclude_secrets_and_collections(self):
        resolved = resolve_model_policy(RichOrder)

        assert "owner.name" in resolved.dotted_fields
        assert "owner.company.name" in resolved.dotted_fields
        assert "owner.password_hash" not in resolved.dotted_fields
        assert "owner.company.tax_secret" not in resolved.dotted_fields
        # One-to-many collections are never flattened into dotted paths
        assert "items.sku" not in resolved.dotted_fields
        assert "items" in resolved.relations
        assert resolved.relations["items"].uselist is True

    def test_all_expands_mapped_columns_only_and_never_relationships(self):
        resolved = resolve_model_policy(AllColumnsModel)

        assert {"id", "name", "owner_id"} <= set(resolved.fields)
        assert resolved.relations == {}
        assert resolved.dotted_fields == {}

    def test_self_referential_model_terminates_without_infinite_recursion(self):
        resolved = resolve_model_policy(CategoryNode)

        assert "parent" in resolved.relations
        assert resolved.dotted_fields == {}  # visited_models prevents CategoryNode -> CategoryNode cycle

    def test_call_site_policy_overrides_model_policy_and_supports_extra_and_hybrid(self):
        resolved = resolve_model_policy(
            RichOrder,
            policy=QueryPolicy(
                fields={
                    "title": FieldPolicy(allow_raw_like=False),
                    "label_code": FieldPolicy(value_type=str, sortable=False),
                },
                extra_fields={"owner_name": FieldPolicy(column=Owner.name, sortable=True)},
                sort_expressions={"total_sum": func.sum(RichOrder.amount)},
                allow_dotted_paths=False,
            ),
        )

        assert "like" not in resolved.fields["title"].operators
        assert resolved.fields["label_code"].scalar_kind == "str"
        assert "label_code" not in resolved.sortable_expressions
        assert "owner_name" in resolved.fields
        assert "owner_name" in resolved.sortable_expressions
        assert "total_sum" in resolved.sortable_expressions
        assert resolved.dotted_fields == {}

    def test_hybrid_without_explicit_value_type_raises_at_compile_time(self):
        with pytest.raises(BadFilterFormatError, match="value_type"):
            resolve_model_policy(RichOrder, policy=QueryPolicy(fields={"label_code": FieldPolicy()}))

    def test_calculated_column_property_infers_nullable_safely(self):
        resolved = resolve_model_policy(ComputedPropertyModel)
        assert resolved.fields["full_name"].scalar_kind == "str"
        assert resolved.fields["full_name"].nullable is True
        assert "is_null" in resolved.fields["full_name"].operators

        non_nullable = resolve_model_policy(
            ComputedPropertyModel,
            policy=QueryPolicy(fields={"full_name": FieldPolicy(nullable=False)}),
        )
        assert non_nullable.fields["full_name"].nullable is False
        assert "is_null" not in non_nullable.fields["full_name"].operators

    def test_partial_call_site_policy_preserves_model_level_budgets_and_flags(self):
        class RestrictedOrder(PolicyBase):
            __tablename__ = "policy_restricted_orders"
            __filterable_fields__ = ["title", "owner"]
            __query_policy__ = QueryPolicy(
                max_limit=50,
                default_limit=25,
                max_depth=4,
                max_nodes=20,
                max_in_items=10,
                allow_dotted_paths=False,
            )

            id = Column(Integer, primary_key=True)
            title = Column(String(100), nullable=False)
            owner_id = Column(Integer, ForeignKey("policy_owners.id"), nullable=True)
            owner = relationship(Owner)

        # Adding extra_fields/sort_expressions at call site must NOT reset model budgets/flags
        merged = resolve_model_policy(
            RestrictedOrder,
            policy=QueryPolicy(extra_fields={"owner_name": FieldPolicy(column=Owner.name)}),
        )
        assert merged.policy.max_limit == 50
        assert merged.policy.default_limit == 25
        assert merged.policy.max_depth == 4
        assert merged.policy.max_nodes == 20
        assert merged.policy.max_in_items == 10
        assert merged.policy.allow_dotted_paths is False
        assert merged.dotted_fields == {}
        assert "owner_name" in merged.fields

        # Explicit call-site scalar overrides still win
        overridden = resolve_model_policy(
            RestrictedOrder,
            policy=QueryPolicy(max_limit=100, allow_dotted_paths=True),
        )
        assert overridden.policy.max_limit == 100
        assert overridden.policy.default_limit == 25
        assert overridden.policy.allow_dotted_paths is True
        assert "owner.name" in overridden.dotted_fields

    def test_association_proxy_is_not_sortable_or_projectable(self):
        from bedrock.database import search_fields

        class ProxyMember(PolicyBase):
            __tablename__ = "policy_proxy_members"
            __filterable_fields__ = ["owner_name"]
            __query_policy__ = QueryPolicy(fields={"owner_name": FieldPolicy(value_type=str)})

            id = Column(Integer, primary_key=True)
            owner_id = Column(Integer, ForeignKey("policy_owners.id"), nullable=True)
            owner = relationship(Owner)
            owner_name = association_proxy("owner", "name")

        resolved = resolve_model_policy(ProxyMember)
        assert resolved.fields["owner_name"].filterable is True
        assert resolved.fields["owner_name"].sortable is False
        assert resolved.fields["owner_name"].projectable is False
        assert "owner_name" not in resolved.sortable_expressions

        engine = create_engine("sqlite:///:memory:")
        PolicyBase.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            with pytest.raises(BadFilterFormatError, match="cannot be projected"):
                search_fields(session, ProxyMember, fields=["owner_name"])
        finally:
            session.close()
            PolicyBase.metadata.drop_all(engine)
            engine.dispose()

    def test_text_search_escapes_like_wildcards(self):
        engine = create_engine("sqlite:///:memory:")
        BedrockModel.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        try:
            session.add_all(
                [
                    EscapedSearchItem(name="100% pure"),
                    EscapedSearchItem(name="1000 pure"),
                    EscapedSearchItem(name="a_b"),
                    EscapedSearchItem(name="axb"),
                ]
            )
            session.commit()

            percent_rows = session.query(EscapedSearchItem).filter(EscapedSearchItem.text_search("100%")).all()
            underscore_rows = session.query(EscapedSearchItem).filter(EscapedSearchItem.text_search("a_b")).all()
            assert [r.name for r in percent_rows] == ["100% pure"]
            assert [r.name for r in underscore_rows] == ["a_b"]
        finally:
            session.close()
            BedrockModel.metadata.drop_all(engine)
            engine.dispose()
