"""Tests for Model-Scoped JSON Schema generation, validation, and Annotated integration."""

from __future__ import annotations

import enum
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import pytest
from bedrock.database.query.ast import FieldFilter, FilterNode, LogicalAndFilter, QuerySpec, RelationFilter
from bedrock.database.query.policy import FieldPolicy, QueryPolicy, resolve_model_policy
from bedrock.database.query.schema import CompiledQueryValidator
from bedrock.entities import BedrockEntity
from bedrock.exc import BadFilterFormatError, FilterDepthExceededError, InvalidQueryLimitError
from pydantic import TypeAdapter, ValidationError
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
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class OrderStatus(enum.StrEnum):
    DRAFT = "draft"
    PAID = "paid"


class SchemaBase(DeclarativeBase):
    """Declarative base for schema compiler tests."""


class SchemaOwner(SchemaBase):
    __tablename__ = "schema_owners"
    __filterable_fields__ = ["name"]

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    password_hash = Column(String(120), nullable=False)


class SchemaOrder(SchemaBase):
    __tablename__ = "schema_orders"
    __filterable_fields__ = [
        "title",
        "quantity",
        "price",
        "ratio",
        "is_active",
        "status",
        "external_id",
        "created_at",
        "due_date",
        "cutoff_time",
        "owner",
        "items",
    ]

    id = Column(Integer, primary_key=True)
    title = Column(String(100), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    ratio = Column(Float, nullable=True)
    is_active = Column(Boolean, nullable=False)
    status = Column(SAEnum(OrderStatus), nullable=False)
    external_id = Column(Uuid, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    due_date = Column(Date, nullable=True)
    cutoff_time = Column(Time, nullable=True)
    internal_note = Column(String(100), nullable=True)
    owner_id = Column(Integer, ForeignKey("schema_owners.id"), nullable=True)
    owner = relationship(SchemaOwner)
    items = relationship("SchemaOrderItem", back_populates="order")


class SchemaOrderItem(SchemaBase):
    __tablename__ = "schema_order_items"
    __filterable_fields__ = ["sku"]

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("schema_orders.id"), nullable=False)
    sku = Column(String(50), nullable=False)
    order = relationship(SchemaOrder, back_populates="items")


class SelfRefCategory(SchemaBase):
    __tablename__ = "schema_self_categories"
    __filterable_fields__ = ["name", "parent"]

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    parent_id = Column(Integer, ForeignKey("schema_self_categories.id"), nullable=True)
    parent = relationship("SelfRefCategory", remote_side=[id])


class TestJsonSchemaGeneration:
    """Verify exported Draft 2020-12 JSON Schema structure and allowlist isolation."""

    def test_exported_json_schema_excludes_secrets_and_includes_search_and_dotted_paths(self):
        validator = CompiledQueryValidator(resolve_model_policy(SchemaOrder))
        schema = validator.json_schema()
        serialized = json.dumps(schema)

        assert "internal_note" not in serialized
        assert "password_hash" not in serialized
        assert "search" in schema["properties"]
        assert "filter" in schema["properties"]
        assert "sort" in schema["properties"]
        assert "pagination" in schema["properties"]
        assert '"owner.name"' in serialized
        assert '"items"' in serialized
        assert '"$defs"' in serialized

        filter_schema = validator.filter_json_schema()
        assert "$ref" in filter_schema or "anyOf" in filter_schema

    def test_self_referential_model_exports_bounded_json_schema(self):
        validator = CompiledQueryValidator(resolve_model_policy(SelfRefCategory))
        schema = validator.json_schema()
        assert "$defs" in schema
        assert "search" in schema["properties"]


class TestSchemaValidationMatrix:
    """Verify strict validation across all column types, operators, and security rules."""

    def test_validates_all_supported_types_and_to_one_dotted_path(self):
        validator = CompiledQueryValidator(resolve_model_policy(SchemaOrder))
        uid = "12345678-1234-5678-1234-567812345678"
        raw = json.dumps(
            {
                "filter": {
                    "logic": "and",
                    "conditions": [
                        {"field": "title", "op": "icontains", "value": "widget"},
                        {"field": "quantity", "op": "between", "value": [1, 10]},
                        {"field": "price", "op": "gte", "value": "19.95"},
                        {"field": "ratio", "op": "lt", "value": 0.5},
                        {"field": "is_active", "op": "eq", "value": True},
                        {"field": "status", "op": "in", "value": ["draft", "paid"]},
                        {"field": "external_id", "op": "eq", "value": uid},
                        {
                            "field": "created_at",
                            "op": "between",
                            "value": ["2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z"],
                        },
                        {"field": "created_at", "op": "is_null"},
                        {"field": "due_date", "op": "eq", "value": "2026-05-01"},
                        {"field": "cutoff_time", "op": "lte", "value": "14:30:00"},
                        {"field": "owner.name", "op": "starts_with", "value": "ann"},
                        {
                            "relation": "items",
                            "quantifier": "some",
                            "filter": {"field": "sku", "op": "eq", "value": "SKU-1"},
                        },
                    ],
                },
                "sort": [{"field": "created_at", "direction": "desc", "nulls": "last"}],
                "search": "abc",
                "pagination": {"page": 2, "limit": 20},
            }
        )

        spec = validator.validate_json(raw)
        assert isinstance(spec, QuerySpec)
        assert isinstance(spec.filter, LogicalAndFilter)
        conds = spec.filter.conditions
        assert isinstance(conds[2], FieldFilter) and conds[2].value == Decimal("19.95")
        assert isinstance(conds[6], FieldFilter) and conds[6].value == UUID(uid)
        assert isinstance(conds[7], FieldFilter) and isinstance(conds[7].value[0], datetime)
        assert isinstance(conds[8], FieldFilter) and conds[8].value is None
        assert isinstance(conds[9], FieldFilter) and conds[9].value == date(2026, 5, 1)
        assert isinstance(conds[10], FieldFilter) and conds[10].value == time(14, 30, 0)
        assert isinstance(conds[11], FieldFilter) and conds[11].field == "owner.name"
        assert isinstance(conds[12], RelationFilter) and conds[12].quantifier == "some"

    @pytest.mark.parametrize(
        "bad_payload",
        [
            {"filter": {"field": "title", "operator": "eq", "value": "a"}},  # typo key
            {"filter": {"field": "created_at", "op": "is_null", "value": False}},  # unary with value
            {"filter": {"field": "quantity", "op": "between", "value": "2026"}},  # string instead of 2-tuple
            {"filter": {"field": "quantity", "op": "between", "value": [1, 2, 3]}},  # 3 items
            {"filter": {"field": "quantity", "op": "eq", "value": True}},  # bool as int
            {"filter": {"field": "created_at", "op": "eq", "value": True}},  # bool as datetime
            {"filter": {"field": "created_at", "op": "eq", "value": 1700000000}},  # raw int as datetime
            {"filter": {"field": "title", "op": "like", "value": "%a%"}},  # raw like off by default
            {"filter": {"field": "quantity", "op": "in", "value": list(range(201))}},  # exceeds max_in_items
            {"filter": {"relation": "items", "quantifier": "every"}},  # every requires child filter
            {"unknown_top_key": 1},
        ],
    )
    def test_rejects_malformed_and_disallowed_payloads(self, bad_payload):
        validator = CompiledQueryValidator(resolve_model_policy(SchemaOrder))
        with pytest.raises(BadFilterFormatError):
            validator.validate_python(bad_payload)

    def test_raw_like_allowed_when_opted_in_via_field_policy(self):
        resolved = resolve_model_policy(
            SchemaOrder,
            policy=QueryPolicy(fields={"title": FieldPolicy(allow_raw_like=True)}),
        )
        validator = CompiledQueryValidator(resolved)
        spec = validator.validate_python({"filter": {"field": "title", "op": "like", "value": "foo%"}})
        assert isinstance(spec.filter, FieldFilter)
        assert spec.filter.op == "like"

    def test_sec01_hidden_and_missing_fields_are_indistinguishable(self):
        validator = CompiledQueryValidator(resolve_model_policy(SchemaOrder))
        with pytest.raises(BadFilterFormatError) as hidden_info:
            validator.validate_python({"filter": {"field": "internal_note", "op": "eq", "value": "x"}})
        with pytest.raises(BadFilterFormatError) as missing_info:
            validator.validate_python({"filter": {"field": "no_such_field", "op": "eq", "value": "x"}})

        assert hidden_info.value.detail == "Field `internal_note` is not allowed on SchemaOrder."
        assert missing_info.value.detail == "Field `no_such_field` is not allowed on SchemaOrder."

    def test_invalid_pagination_raises_invalid_query_limit(self):
        validator = CompiledQueryValidator(resolve_model_policy(SchemaOrder, policy=QueryPolicy(max_limit=50)))
        with pytest.raises(InvalidQueryLimitError):
            validator.validate_python({"pagination": {"page": 1, "limit": 51}})

    def test_deep_filter_raises_filter_depth_exceeded(self):
        validator = CompiledQueryValidator(resolve_model_policy(SchemaOrder, policy=QueryPolicy(max_depth=4)))
        node: dict[str, object] = {"field": "quantity", "op": "eq", "value": 1}
        for _ in range(6):
            node = {"logic": "not", "condition": node}
        with pytest.raises(FilterDepthExceededError):
            validator.validate_python({"filter": node})

    def test_pydantic_annotated_protocol_works_in_entity_and_openapi_schema(self):
        validator = CompiledQueryValidator(resolve_model_policy(SchemaOrder))

        class _SchemaHook:
            def __get_pydantic_core_schema__(self, source_type, handler):
                return validator.build_pydantic_core_schema(source_type, handler)

        hook = _SchemaHook()

        class ExportTaskInput(BedrockEntity):
            query: Annotated[QuerySpec, hook]
            only_filter: Annotated[FilterNode, hook] | None = None

        ExportTaskInput.model_rebuild(_types_namespace={"hook": hook})
        json_schema = ExportTaskInput.model_json_schema()
        assert "internal_note" not in json.dumps(json_schema)
        assert "owner.name" in json.dumps(json_schema)

        parsed = ExportTaskInput.model_validate(
            {
                "query": {"filter": {"field": "quantity", "op": "gte", "value": 5}},
                "only_filter": {"field": "owner.name", "op": "eq", "value": "ann"},
            }
        )
        assert isinstance(parsed.query, QuerySpec)
        assert isinstance(parsed.only_filter, FieldFilter)
        assert parsed.only_filter.field == "owner.name"

        with pytest.raises(ValidationError):
            TypeAdapter(Annotated[QuerySpec, hook]).validate_python(
                {"filter": {"field": "internal_note", "op": "eq", "value": "x"}}
            )
