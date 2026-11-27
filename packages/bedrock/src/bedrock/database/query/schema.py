"""Low-memory Model-Scoped Pydantic v2 and JSON Schema Draft 2020-12 compiler."""

import enum
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any

import orjson
from pydantic import GetCoreSchemaHandler, TypeAdapter, ValidationError
from pydantic_core import CoreSchema, core_schema

from ...exc import BadFilterFormatError
from .ast import (
    FieldFilter,
    FilterNode,
    LogicalAndFilter,
    LogicalNotFilter,
    LogicalOrFilter,
    PaginationSpec,
    QueryInput,
    QuerySpec,
    RelationFilter,
    SortSpec,
)
from .errors import normalize_validation_error, precheck_raw_filter, precheck_raw_query
from .policy import ResolvedField, ResolvedModelPolicy, resolve_model_policy


def _reject_bool_for_decimal(value: Any) -> Any:
    """Reject booleans before Decimal validation (Python treats bool as int)."""
    if isinstance(value, bool):
        raise ValueError("Input should be a valid decimal number, not a boolean.")
    return value


def _reject_non_datetime_input(value: Any) -> Any:
    """Accept ISO-8601 strings or datetime instances; reject booleans and raw numbers."""
    if isinstance(value, bool | int | float):
        raise ValueError("Input should be an ISO-8601 datetime string or datetime instance.")
    return value


def _reject_non_date_input(value: Any) -> Any:
    """Accept ISO-8601 date strings or date instances; reject booleans, numbers, and datetimes."""
    if isinstance(value, bool | int | float | datetime):
        raise ValueError("Input should be an ISO-8601 date string or date instance.")
    return value


def _reject_non_time_input(value: Any) -> Any:
    """Accept ISO-8601 time strings or time instances; reject booleans and numbers."""
    if isinstance(value, bool | int | float):
        raise ValueError("Input should be an ISO-8601 time string or time instance.")
    return value


def _coerce_enum_member(value: Any) -> Any:
    """Unwrap Python Enum instances to their underlying value before literal check."""
    if isinstance(value, enum.Enum):
        return value.value
    return value


def _scalar_core_schema(field: ResolvedField) -> CoreSchema:
    """Build a strict ``pydantic_core`` schema for one scalar field's values."""
    kind = field.scalar_kind
    if kind == "bool":
        return core_schema.bool_schema(strict=True)
    if kind == "int":
        return core_schema.int_schema(strict=True)
    if kind == "float":
        return core_schema.union_schema([core_schema.int_schema(strict=True), core_schema.float_schema(strict=True)])
    if kind == "decimal":
        return core_schema.no_info_before_validator_function(
            _reject_bool_for_decimal,
            core_schema.decimal_schema(),
        )
    if kind == "str":
        return core_schema.str_schema(strict=True)
    if kind == "datetime":
        return core_schema.no_info_before_validator_function(
            _reject_non_datetime_input,
            core_schema.datetime_schema(),
        )
    if kind == "date":
        return core_schema.no_info_before_validator_function(
            _reject_non_date_input,
            core_schema.date_schema(),
        )
    if kind == "time":
        return core_schema.no_info_before_validator_function(
            _reject_non_time_input,
            core_schema.time_schema(),
        )
    if kind == "uuid":
        return core_schema.uuid_schema()
    if kind == "enum":
        values = list(field.enum_values or ())
        if not values:
            return core_schema.str_schema(strict=True)
        return core_schema.no_info_before_validator_function(
            _coerce_enum_member,
            core_schema.literal_schema(values),
        )
    raise BadFilterFormatError(f"Unsupported scalar kind `{kind}` for field `{field.name}`.")


def _field_predicate_branches(
    field_name: str,
    field: ResolvedField,
    *,
    max_in_items: int,
) -> list[CoreSchema]:
    """Generate strict typed-dict ``CoreSchema`` branches for one filterable field."""
    branches: list[CoreSchema] = []
    scalar = _scalar_core_schema(field)
    ops = field.operators

    single_ops = sorted(ops - {"in", "between", "is_null", "is_not_null"})
    if single_ops:
        branches.append(
            core_schema.typed_dict_schema(
                {
                    "field": core_schema.typed_dict_field(core_schema.literal_schema([field_name]), required=True),
                    "op": core_schema.typed_dict_field(core_schema.literal_schema(single_ops), required=True),
                    "value": core_schema.typed_dict_field(scalar, required=True),
                },
                extra_behavior="forbid",
            )
        )

    if "in" in ops:
        branches.append(
            core_schema.typed_dict_schema(
                {
                    "field": core_schema.typed_dict_field(core_schema.literal_schema([field_name]), required=True),
                    "op": core_schema.typed_dict_field(core_schema.literal_schema(["in"]), required=True),
                    "value": core_schema.typed_dict_field(
                        core_schema.list_schema(scalar, min_length=0, max_length=max_in_items),
                        required=True,
                    ),
                },
                extra_behavior="forbid",
            )
        )

    if "between" in ops:
        branches.append(
            core_schema.typed_dict_schema(
                {
                    "field": core_schema.typed_dict_field(core_schema.literal_schema([field_name]), required=True),
                    "op": core_schema.typed_dict_field(core_schema.literal_schema(["between"]), required=True),
                    "value": core_schema.typed_dict_field(
                        core_schema.tuple_schema([scalar, scalar]),
                        required=True,
                    ),
                },
                extra_behavior="forbid",
            )
        )

    null_ops = sorted(ops & {"is_null", "is_not_null"})
    if null_ops:
        branches.append(
            core_schema.typed_dict_schema(
                {
                    "field": core_schema.typed_dict_field(core_schema.literal_schema([field_name]), required=True),
                    "op": core_schema.typed_dict_field(core_schema.literal_schema(null_ops), required=True),
                },
                extra_behavior="forbid",
            )
        )

    return branches


def _dict_to_filter_node(data: Any) -> FilterNode:
    """Convert a validated raw filter dict into the canonical ``FilterNode`` AST."""
    if isinstance(data, (FieldFilter, LogicalAndFilter, LogicalOrFilter, LogicalNotFilter, RelationFilter)):
        return data
    if not isinstance(data, Mapping):
        raise ValueError("Filter node must be a mapping.")
    if "logic" in data:
        logic = data["logic"]
        if logic == "and":
            return LogicalAndFilter.model_construct(
                logic="and",
                conditions=[_dict_to_filter_node(item) for item in data["conditions"]],
            )
        if logic == "or":
            return LogicalOrFilter.model_construct(
                logic="or",
                conditions=[_dict_to_filter_node(item) for item in data["conditions"]],
            )
        if logic == "not":
            return LogicalNotFilter.model_construct(
                logic="not",
                condition=_dict_to_filter_node(data["condition"]),
            )
    if "relation" in data:
        raw_child = data.get("filter")
        return RelationFilter.model_construct(
            relation=data["relation"],
            quantifier=data["quantifier"],
            filter=_dict_to_filter_node(raw_child) if raw_child is not None else None,
        )
    return FieldFilter.model_construct(
        field=data["field"],
        op=data["op"],
        value=data.get("value"),
    )


def _dict_to_query_spec(data: Any) -> QuerySpec:
    """Convert a validated raw query dict into the canonical ``QuerySpec`` AST."""
    if isinstance(data, QuerySpec):
        return data
    if not isinstance(data, Mapping):
        raise ValueError("Query payload must be a mapping.")

    raw_filter = data.get("filter")
    raw_sort = data.get("sort") or []
    raw_pagination = data.get("pagination")

    sort_items = [
        item
        if isinstance(item, SortSpec)
        else SortSpec.model_construct(
            field=item["field"],
            direction=item.get("direction", "asc"),
            nulls=item.get("nulls"),
        )
        for item in raw_sort
    ]

    pagination = None
    if raw_pagination is not None:
        if isinstance(raw_pagination, PaginationSpec):
            pagination = raw_pagination
        else:
            pagination = PaginationSpec.model_construct(
                page=raw_pagination.get("page", 1),
                limit=raw_pagination.get("limit", 10),
            )

    return QuerySpec.model_construct(
        filter=_dict_to_filter_node(raw_filter) if raw_filter is not None else None,
        sort=sort_items,
        search=data.get("search"),
        pagination=pagination,
    )


def _filter_node_to_dict(node: FilterNode) -> dict[str, Any]:
    """Serialize a ``FilterNode`` AST back to a raw dict for re-validation."""
    if isinstance(node, FieldFilter):
        if node.op in ("is_null", "is_not_null"):
            return {"field": node.field, "op": node.op}
        return {"field": node.field, "op": node.op, "value": node.value}
    if isinstance(node, LogicalAndFilter):
        return {"logic": "and", "conditions": [_filter_node_to_dict(c) for c in node.conditions]}
    if isinstance(node, LogicalOrFilter):
        return {"logic": "or", "conditions": [_filter_node_to_dict(c) for c in node.conditions]}
    if isinstance(node, LogicalNotFilter):
        return {"logic": "not", "condition": _filter_node_to_dict(node.condition)}
    payload: dict[str, Any] = {"relation": node.relation, "quantifier": node.quantifier}
    if node.filter is not None:
        payload["filter"] = _filter_node_to_dict(node.filter)
    return payload


def _query_spec_to_dict(spec: QuerySpec) -> dict[str, Any]:
    """Serialize a ``QuerySpec`` AST back to a raw dict for model-scoped validation."""
    payload: dict[str, Any] = {}
    if spec.filter is not None:
        payload["filter"] = _filter_node_to_dict(spec.filter)
    if spec.sort:
        payload["sort"] = [
            {
                "field": s.field,
                "direction": s.direction,
                **({"nulls": s.nulls} if s.nulls is not None else {}),
            }
            for s in spec.sort
        ]
    if spec.search is not None:
        payload["search"] = spec.search
    if spec.pagination is not None:
        payload["pagination"] = {"page": spec.pagination.page, "limit": spec.pagination.limit}
    return payload


def _filter_ref(model: type[Any], depth: int, schema_tag: str) -> str:
    """Return a unique, collision-free Pydantic ref for *(model, depth, schema_tag)*."""
    return f"{model.__module__}.{model.__qualname__}:Filter:d{depth}:{schema_tag}"


class _FilterNodeSource:
    """Pydantic metadata source building a recursive model filter ``CoreSchema``."""

    __slots__ = ("_include_dotted", "_ref", "_remaining_rel_depth", "_resolved", "_schema_tag")

    def __init__(
        self,
        resolved: ResolvedModelPolicy,
        *,
        remaining_rel_depth: int,
        include_dotted: bool,
        ref: str,
        schema_tag: str,
    ) -> None:
        self._resolved = resolved
        self._remaining_rel_depth = remaining_rel_depth
        self._include_dotted = include_dotted
        self._ref = ref
        self._schema_tag = schema_tag

    def __get_pydantic_core_schema__(self, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        resolved = self._resolved
        ref = self._ref
        branches: list[CoreSchema] = []
        max_in = resolved.policy.max_in_items

        # 1. Logical combinators referencing self via definition_reference_schema
        self_ref_schema = core_schema.definition_reference_schema(ref)
        branches.append(
            core_schema.typed_dict_schema(
                {
                    "logic": core_schema.typed_dict_field(core_schema.literal_schema(["and"]), required=True),
                    "conditions": core_schema.typed_dict_field(
                        core_schema.list_schema(self_ref_schema, min_length=1),
                        required=True,
                    ),
                },
                extra_behavior="forbid",
            )
        )
        branches.append(
            core_schema.typed_dict_schema(
                {
                    "logic": core_schema.typed_dict_field(core_schema.literal_schema(["or"]), required=True),
                    "conditions": core_schema.typed_dict_field(
                        core_schema.list_schema(self_ref_schema, min_length=1),
                        required=True,
                    ),
                },
                extra_behavior="forbid",
            )
        )
        branches.append(
            core_schema.typed_dict_schema(
                {
                    "logic": core_schema.typed_dict_field(core_schema.literal_schema(["not"]), required=True),
                    "condition": core_schema.typed_dict_field(self_ref_schema, required=True),
                },
                extra_behavior="forbid",
            )
        )

        # 2. Direct allowlisted scalar fields
        for field_name, field in resolved.fields.items():
            if not field.filterable:
                continue
            branches.extend(_field_predicate_branches(field_name, field, max_in_items=max_in))

        # 3. Allowlisted to-one dotted path syntax sugar (root level only; expanded to has() EXISTS)
        if self._include_dotted:
            for dotted_path, dotted_field in resolved.dotted_fields.items():
                branches.extend(_field_predicate_branches(dotted_path, dotted_field.target_field, max_in_items=max_in))

        # 4. Allowlisted explicit relation quantifier branches
        if self._remaining_rel_depth > 0:
            for rel_name, rel in resolved.relations.items():
                child_resolved = resolve_model_policy(rel.target_model, _collect_dotted=False)
                child_ref = _filter_ref(
                    child_resolved.model,
                    self._remaining_rel_depth - 1,
                    self._schema_tag,
                )
                child_source = _FilterNodeSource(
                    child_resolved,
                    remaining_rel_depth=self._remaining_rel_depth - 1,
                    include_dotted=False,
                    ref=child_ref,
                    schema_tag=self._schema_tag,
                )
                child_schema = handler.generate_schema(Annotated[Any, child_source])
                child_ref_only = core_schema.definition_reference_schema(child_ref)

                if rel.uselist:
                    branches.append(
                        core_schema.typed_dict_schema(
                            {
                                "relation": core_schema.typed_dict_field(
                                    core_schema.literal_schema([rel_name]), required=True
                                ),
                                "quantifier": core_schema.typed_dict_field(
                                    core_schema.literal_schema(["some", "none"]), required=True
                                ),
                                "filter": core_schema.typed_dict_field(
                                    core_schema.nullable_schema(child_schema),
                                    required=False,
                                ),
                            },
                            extra_behavior="forbid",
                        )
                    )
                    branches.append(
                        core_schema.typed_dict_schema(
                            {
                                "relation": core_schema.typed_dict_field(
                                    core_schema.literal_schema([rel_name]), required=True
                                ),
                                "quantifier": core_schema.typed_dict_field(
                                    core_schema.literal_schema(["every"]), required=True
                                ),
                                "filter": core_schema.typed_dict_field(child_ref_only, required=True),
                            },
                            extra_behavior="forbid",
                        )
                    )
                else:
                    branches.append(
                        core_schema.typed_dict_schema(
                            {
                                "relation": core_schema.typed_dict_field(
                                    core_schema.literal_schema([rel_name]), required=True
                                ),
                                "quantifier": core_schema.typed_dict_field(
                                    core_schema.literal_schema(["has", "missing"]), required=True
                                ),
                                "filter": core_schema.typed_dict_field(
                                    core_schema.nullable_schema(child_schema),
                                    required=False,
                                ),
                            },
                            extra_behavior="forbid",
                        )
                    )

        return core_schema.union_schema(branches, ref=ref)


class _QueryDictSource:
    """Pydantic metadata source building the full model-scoped query ``CoreSchema``."""

    __slots__ = ("_filter_source", "_resolved")

    def __init__(self, resolved: ResolvedModelPolicy, filter_source: _FilterNodeSource) -> None:
        self._resolved = resolved
        self._filter_source = filter_source

    def __get_pydantic_core_schema__(self, source_type: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        resolved = self._resolved
        filter_schema = handler.generate_schema(Annotated[Any, self._filter_source])
        sortable_names = sorted(resolved.sortable_expressions)
        if sortable_names:
            sort_item_schema: CoreSchema = core_schema.typed_dict_schema(
                {
                    "field": core_schema.typed_dict_field(
                        core_schema.literal_schema(sortable_names),
                        required=True,
                    ),
                    "direction": core_schema.typed_dict_field(
                        core_schema.with_default_schema(
                            core_schema.literal_schema(["asc", "desc"]),
                            default="asc",
                        ),
                        required=False,
                    ),
                    "nulls": core_schema.typed_dict_field(
                        core_schema.with_default_schema(
                            core_schema.nullable_schema(core_schema.literal_schema(["first", "last"])),
                            default=None,
                        ),
                        required=False,
                    ),
                },
                extra_behavior="forbid",
            )
            sort_list_schema = core_schema.list_schema(sort_item_schema)
        else:
            sort_list_schema = core_schema.list_schema(core_schema.any_schema(), max_length=0)

        pagination_schema = core_schema.typed_dict_schema(
            {
                "page": core_schema.typed_dict_field(
                    core_schema.with_default_schema(core_schema.int_schema(strict=True, ge=1), default=1),
                    required=False,
                ),
                "limit": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.int_schema(strict=True, ge=1, le=resolved.policy.max_limit),
                        default=resolved.policy.default_limit,
                    ),
                    required=False,
                ),
            },
            extra_behavior="forbid",
        )

        return core_schema.typed_dict_schema(
            {
                "filter": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.nullable_schema(filter_schema),
                        default=None,
                    ),
                    required=False,
                ),
                "sort": core_schema.typed_dict_field(
                    core_schema.with_default_schema(sort_list_schema, default_factory=list),
                    required=False,
                ),
                "search": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.nullable_schema(core_schema.str_schema(strict=True)),
                        default=None,
                    ),
                    required=False,
                ),
                "pagination": core_schema.typed_dict_field(
                    core_schema.with_default_schema(
                        core_schema.nullable_schema(pagination_schema),
                        default=None,
                    ),
                    required=False,
                ),
            },
            extra_behavior="forbid",
        )


class CompiledQueryValidator:
    """Memory-light compiled Pydantic v2 validator and lazy JSON Schema generator."""

    def __init__(self, resolved: ResolvedModelPolicy) -> None:
        self.resolved = resolved
        self._schema_tag = f"{id(self):x}"
        root_ref = _filter_ref(resolved.model, resolved.policy.max_relation_depth, self._schema_tag)
        self._filter_source = _FilterNodeSource(
            resolved,
            remaining_rel_depth=resolved.policy.max_relation_depth,
            include_dotted=True,
            ref=root_ref,
            schema_tag=self._schema_tag,
        )
        self._query_source = _QueryDictSource(resolved, self._filter_source)
        self._query_adapter: TypeAdapter[Any] = TypeAdapter(Annotated[Any, self._query_source])
        self._filter_adapter: TypeAdapter[Any] = TypeAdapter(Annotated[Any, self._filter_source])

    def build_pydantic_core_schema(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build a ``CoreSchema`` for ``Annotated[QuerySpec | FilterNode, schema]``."""
        policy = self.resolved.policy
        if source_type is QuerySpec:
            inner = handler.generate_schema(Annotated[Any, self._query_source])

            def _precheck_query(value: Any) -> Any:
                raw = _query_spec_to_dict(value) if isinstance(value, QuerySpec) else value
                if isinstance(raw, Mapping):
                    precheck_raw_query(
                        raw,
                        max_depth=policy.max_depth,
                        max_nodes=policy.max_nodes,
                        max_relation_depth=policy.max_relation_depth,
                        max_limit=policy.max_limit,
                    )
                return raw

            return core_schema.no_info_after_validator_function(
                _dict_to_query_spec,
                core_schema.no_info_before_validator_function(_precheck_query, inner),
            )

        inner = handler.generate_schema(Annotated[Any, self._filter_source])

        def _precheck_filter(value: Any) -> Any:
            raw = (
                _filter_node_to_dict(value)
                if isinstance(value, (FieldFilter, LogicalAndFilter, LogicalOrFilter, LogicalNotFilter, RelationFilter))
                else value
            )
            if isinstance(raw, Mapping):
                precheck_raw_filter(
                    raw,
                    max_depth=policy.max_depth,
                    max_nodes=policy.max_nodes,
                    max_relation_depth=policy.max_relation_depth,
                )
            return raw

        return core_schema.no_info_after_validator_function(
            _dict_to_filter_node,
            core_schema.no_info_before_validator_function(_precheck_filter, inner),
        )

    def json_schema(self) -> dict[str, Any]:
        """Lazily generate the Draft 2020-12 JSON Schema for the full query payload."""
        return self._query_adapter.json_schema(mode="validation")

    def filter_json_schema(self) -> dict[str, Any]:
        """Lazily generate the Draft 2020-12 JSON Schema for standalone filter payloads."""
        return self._filter_adapter.json_schema(mode="validation")

    def validate_json(self, raw: str | bytes) -> QuerySpec:
        """Parse and validate a JSON string/bytes query payload into a ``QuerySpec``."""
        try:
            parsed = orjson.loads(raw)
        except (orjson.JSONDecodeError, TypeError, ValueError) as err:
            raise BadFilterFormatError("Invalid JSON query payload.") from err
        return self.validate_python(parsed)

    def validate_python(self, raw: QueryInput) -> QuerySpec:
        """Validate a Python dict, ``QuerySpec``, JSON string/bytes, or ``None``."""
        if raw is None:
            return QuerySpec()
        if isinstance(raw, str | bytes):
            return self.validate_json(raw)
        if isinstance(raw, QuerySpec):
            payload: Any = _query_spec_to_dict(raw)
        else:
            payload = raw

        policy = self.resolved.policy
        precheck_raw_query(
            payload,
            max_depth=policy.max_depth,
            max_nodes=policy.max_nodes,
            max_relation_depth=policy.max_relation_depth,
            max_limit=policy.max_limit,
        )
        try:
            validated = self._query_adapter.validate_python(payload)
        except ValidationError as err:
            raise normalize_validation_error(err, resolved=self.resolved, raw_payload=payload) from err
        return _dict_to_query_spec(validated)

    def validate_filter(self, raw: str | bytes | Mapping[str, Any] | FilterNode) -> FilterNode:
        """Validate a standalone filter payload into a canonical ``FilterNode``."""
        if isinstance(raw, str | bytes):
            try:
                payload: Any = orjson.loads(raw)
            except (orjson.JSONDecodeError, TypeError, ValueError) as err:
                raise BadFilterFormatError("Invalid JSON filter payload.") from err
        elif isinstance(raw, (FieldFilter, LogicalAndFilter, LogicalOrFilter, LogicalNotFilter, RelationFilter)):
            payload = _filter_node_to_dict(raw)
        else:
            payload = raw

        if not isinstance(payload, Mapping):
            raise BadFilterFormatError("Filter payload must be a JSON object.")

        policy = self.resolved.policy
        precheck_raw_filter(
            payload,
            max_depth=policy.max_depth,
            max_nodes=policy.max_nodes,
            max_relation_depth=policy.max_relation_depth,
        )
        try:
            validated = self._filter_adapter.validate_python(payload)
        except ValidationError as err:
            raise normalize_validation_error(
                err,
                resolved=self.resolved,
                raw_payload=payload,
                is_filter_only=True,
            ) from err
        return _dict_to_filter_node(validated)
