"""Tests for iterative safety prechecks and SEC-01 error normalization."""

from __future__ import annotations

import pytest
from bedrock.database.query.errors import (
    normalize_validation_error,
    precheck_raw_filter,
    precheck_raw_query,
)
from bedrock.database.query.policy import QueryPolicy, resolve_model_policy
from bedrock.exc import BadFilterFormatError, FilterDepthExceededError, InvalidQueryLimitError
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, relationship


class ErrorBase(DeclarativeBase):
    """Declarative base for error normalization tests."""


class SecretOwner(ErrorBase):
    __tablename__ = "err_owners"
    __filterable_fields__ = ["name"]

    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)
    password_hash = Column(String(100), nullable=False)


class SecretOrder(ErrorBase):
    __tablename__ = "err_orders"
    __filterable_fields__ = ["total", "owner"]

    id = Column(Integer, primary_key=True)
    total = Column(Integer, nullable=False)
    internal_note = Column(String(100), nullable=True)
    owner_id = Column(Integer, ForeignKey("err_owners.id"), nullable=True)
    owner = relationship(SecretOwner)


class TestPrechecks:
    """Verify iterative depth, width, relation hop, and pagination prechecks."""

    def test_deep_nesting_hits_depth_limit_without_recursion_error(self):
        tree: dict[str, object] = {"field": "total", "op": "eq", "value": 1}
        for _ in range(1000):
            tree = {"logic": "not", "condition": tree}

        with pytest.raises(FilterDepthExceededError, match="maximum depth"):
            precheck_raw_filter(tree, max_depth=16, max_nodes=2000, max_relation_depth=3)

    def test_wide_tree_hits_node_budget(self):
        wide = {
            "logic": "or",
            "conditions": [{"field": "total", "op": "eq", "value": i} for i in range(150)],
        }
        with pytest.raises(BadFilterFormatError, match="maximum node count"):
            precheck_raw_filter(wide, max_depth=16, max_nodes=100, max_relation_depth=3)

    def test_relation_and_dotted_hops_share_relation_depth_budget(self):
        nested_rel = {
            "relation": "owner",
            "quantifier": "has",
            "filter": {"field": "a.b.c", "op": "eq", "value": "x"},
        }
        with pytest.raises(FilterDepthExceededError, match="relation depth"):
            precheck_raw_filter(nested_rel, max_depth=16, max_nodes=100, max_relation_depth=2)

    @pytest.mark.parametrize(
        "pagination",
        [
            {"page": 0, "limit": 10},
            {"page": True, "limit": 10},
            {"page": 1, "limit": 0},
            {"page": 1, "limit": True},
            {"page": 1, "limit": 1001},
            "not-an-object",
        ],
    )
    def test_invalid_pagination_raises_invalid_query_limit(self, pagination):
        with pytest.raises(InvalidQueryLimitError):
            precheck_raw_query(
                {"pagination": pagination},
                max_depth=16,
                max_nodes=100,
                max_relation_depth=2,
                max_limit=1000,
            )

    def test_non_mapping_top_level_raises_bad_filter_format(self):
        with pytest.raises(BadFilterFormatError, match="JSON object"):
            precheck_raw_query(
                ["not", "a", "dict"],
                max_depth=16,
                max_nodes=100,
                max_relation_depth=2,
                max_limit=1000,
            )

    def test_tuple_conditions_cannot_bypass_depth_or_node_budgets(self):
        tree: dict[str, object] = {"field": "total", "op": "eq", "value": 1}
        for _ in range(6):
            tree = {"logic": "and", "conditions": (tree,)}

        with pytest.raises(FilterDepthExceededError, match="maximum depth"):
            precheck_raw_filter(tree, max_depth=4, max_nodes=100, max_relation_depth=2)


class TestErrorNormalization:
    """Verify SEC-01 redaction vs reveal_allowed_fields diagnostics."""

    def test_hidden_and_missing_fields_produce_indistinguishable_errors_when_redacted(self):
        class Dummy(BaseModel):
            model_config = ConfigDict(extra="forbid")
            field: str

        resolved = resolve_model_policy(SecretOrder)
        try:
            Dummy.model_validate({"field": 123})
        except ValidationError as exc:
            err = exc

        hidden_exc = normalize_validation_error(
            err,
            resolved=resolved,
            raw_payload={"filter": {"field": "internal_note", "op": "eq", "value": "x"}},
        )
        missing_exc = normalize_validation_error(
            err,
            resolved=resolved,
            raw_payload={"filter": {"field": "does_not_exist", "op": "eq", "value": "x"}},
        )

        assert isinstance(hidden_exc, BadFilterFormatError)
        assert isinstance(missing_exc, BadFilterFormatError)
        assert hidden_exc.detail == "Field `internal_note` is not allowed on SecretOrder."
        assert missing_exc.detail == "Field `does_not_exist` is not allowed on SecretOrder."
        assert "total" not in hidden_exc.detail

    def test_reveal_allowed_fields_surfaces_detailed_pydantic_message(self):
        class Dummy(BaseModel):
            model_config = ConfigDict(extra="forbid")
            total: int

        resolved = resolve_model_policy(SecretOrder, policy=QueryPolicy(reveal_allowed_fields=True))
        try:
            Dummy.model_validate({"total": "bad"})
        except ValidationError as exc:
            err = exc

        out = normalize_validation_error(err, resolved=resolved, raw_payload={"total": "bad"})
        assert isinstance(out, BadFilterFormatError)
        assert "$.total" in out.detail
        assert "integer" in out.detail.lower()

    def test_query_schema_union_errors_report_accurate_paths_not_spurious_extra_key(self):
        from bedrock.database import QuerySchema

        schema = QuerySchema(SecretOrder, policy=QueryPolicy(max_in_items=2))

        # Wrong value type reports $.filter.value, not $.filter.field
        with pytest.raises(BadFilterFormatError) as wrong_type:
            schema.validate({"filter": {"field": "total", "op": "eq", "value": "not-an-int"}})
        assert "$.filter.value" in wrong_type.value.detail

        # IN list overflow in nested relation reports $.filter.filter.value
        with pytest.raises(BadFilterFormatError) as nested_in:
            schema.validate(
                {
                    "filter": {
                        "relation": "owner",
                        "quantifier": "has",
                        "filter": {"field": "name", "op": "in", "value": ["a", "b", "c"]},
                    }
                }
            )
        assert "$.filter.filter.value" in nested_in.value.detail

        # Genuine unexpected key reports $.filter.bogus
        with pytest.raises(BadFilterFormatError) as extra_key:
            schema.validate({"filter": {"field": "total", "op": "eq", "value": 5, "bogus": 1}})
        assert "$.filter.bogus" in extra_key.value.detail

        # Tuple sort list still gets SEC-01 identifier redaction
        with pytest.raises(BadFilterFormatError) as tuple_sort:
            schema.validate({"sort": ({"field": "internal_note", "direction": "asc"},)})
        assert tuple_sort.value.detail == "Field `internal_note` is not allowed on SecretOrder."
