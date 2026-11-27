"""Tests for strongly typed query AST and pagination result entities."""

from __future__ import annotations

import pytest
from bedrock.database.query.ast import (
    FieldFilter,
    LogicalAndFilter,
    LogicalNotFilter,
    LogicalOrFilter,
    PageInfo,
    PageResult,
    PaginationSpec,
    QuerySpec,
    RelationFilter,
    SortSpec,
)
from pydantic import ValidationError


class TestQueryAst:
    """Verify AST validation, recursion, and strict unknown-key rejection."""

    def test_recursive_query_spec_construction_and_serialization(self):
        spec = QuerySpec(
            filter=LogicalAndFilter(
                conditions=[
                    FieldFilter(field="status", op="in", value=["paid", "shipped"]),
                    LogicalNotFilter(
                        condition=LogicalOrFilter(
                            conditions=[
                                FieldFilter(field="deleted_at", op="is_not_null"),
                                RelationFilter(
                                    relation="items",
                                    quantifier="some",
                                    filter=FieldFilter(field="sku", op="starts_with", value="TEST_"),
                                ),
                            ]
                        )
                    ),
                ]
            ),
            sort=[
                SortSpec(field="created_at", direction="desc", nulls="last"),
                SortSpec(field="id", direction="asc"),
            ],
            search="widget",
            pagination=PaginationSpec(page=2, limit=25),
        )

        dumped = spec.model_dump()
        assert dumped["search"] == "widget"
        assert dumped["pagination"] == {"page": 2, "limit": 25}
        assert len(dumped["sort"]) == 2
        assert QuerySpec.model_validate(dumped) == spec

    def test_unknown_keys_are_forbidden(self):
        with pytest.raises(ValidationError):
            FieldFilter.model_validate({"field": "name", "operator": "eq", "value": "ann"})

        with pytest.raises(ValidationError):
            QuerySpec.model_validate({"unknown": True})

    def test_unary_operator_rejects_value(self):
        assert FieldFilter(field="deleted_at", op="is_null").value is None
        with pytest.raises(ValidationError, match="does not accept a `value`"):
            FieldFilter(field="deleted_at", op="is_null", value=False)

    def test_pagination_rejects_bool_and_non_positive_values(self):
        with pytest.raises(ValidationError):
            PaginationSpec.model_validate({"page": True, "limit": 10})
        with pytest.raises(ValidationError):
            PaginationSpec.model_validate({"page": 0, "limit": 10})
        with pytest.raises(ValidationError):
            PaginationSpec.model_validate({"page": 1, "limit": 0})

    def test_page_result_serializes_dict_rows(self):
        result = PageResult[dict[str, object]](
            items=[{"id": 1, "name": "alpha"}],
            total=1,
            page_info=PageInfo(total=1, page=1, limit=10, offset=0, has_more=False),
        )
        raw = result.model_dump_json()
        assert '"alpha"' in raw
        assert '"has_more":false' in raw
