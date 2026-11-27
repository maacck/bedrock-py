"""Iterative safety prechecks and SEC-01 error normalization for query validation."""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from ...exc import BadFilterFormatError, BedrockExc, FilterDepthExceededError, InvalidQueryLimitError
from .policy import ResolvedModelPolicy, resolve_model_policy

_INTERNAL_LOC_PREFIXES = ("typed-dict", "function-", "union", "logic:", "rel:", "field:")


def _is_sequence(value: Any) -> bool:
    """Return ``True`` for list/tuple-like sequences while excluding strings and bytes."""
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def precheck_raw_filter(
    root: Any,
    *,
    max_depth: int,
    max_nodes: int,
    max_relation_depth: int,
) -> None:
    """Iteratively validate filter tree depth, node count, and relation hops.

    Runs before Pydantic validation so maliciously nested payloads cannot
    exhaust the Python or validator call stack.

    Args:
        root: Raw filter node payload (typically a dict).
        max_depth: Maximum permitted nesting depth.
        max_nodes: Maximum permitted total filter nodes.
        max_relation_depth: Maximum permitted relationship traversal hops.

    Raises:
        FilterDepthExceededError: If *max_depth* or *max_relation_depth* is exceeded.
        BadFilterFormatError: If *max_nodes* is exceeded.
    """
    stack: list[tuple[Any, int, int]] = [(root, 1, 0)]
    node_count = 0

    while stack:
        node, depth, rel_depth = stack.pop()
        if depth > max_depth:
            raise FilterDepthExceededError(f"Filter tree exceeds the maximum depth of {max_depth}.")
        node_count += 1
        if node_count > max_nodes:
            raise BadFilterFormatError(f"Filter tree exceeds the maximum node count of {max_nodes}.")
        if not isinstance(node, Mapping):
            continue

        field_val = node.get("field")
        if isinstance(field_val, str) and "." in field_val:
            hops = field_val.count(".")
            if rel_depth + hops > max_relation_depth:
                raise FilterDepthExceededError(f"Filter relation depth exceeds the maximum of {max_relation_depth}.")

        if "relation" in node:
            next_rel_depth = rel_depth + 1
            if next_rel_depth > max_relation_depth:
                raise FilterDepthExceededError(f"Filter relation depth exceeds the maximum of {max_relation_depth}.")
            child_filter = node.get("filter")
            if child_filter is not None:
                stack.append((child_filter, depth + 1, next_rel_depth))

        conditions = node.get("conditions")
        if _is_sequence(conditions):
            for child in reversed(conditions):
                stack.append((child, depth + 1, rel_depth))

        if "condition" in node and node["condition"] is not None:
            stack.append((node["condition"], depth + 1, rel_depth))


def precheck_raw_query(
    raw: Any,
    *,
    max_depth: int,
    max_nodes: int,
    max_relation_depth: int,
    max_limit: int,
    pager: Any = None,
) -> None:
    """Iteratively validate top-level query shape, pagination bounds, and filter budgets.

    Args:
        raw: Untrusted top-level query mapping.
        max_depth: Maximum permitted filter tree depth.
        max_nodes: Maximum permitted filter tree node count.
        max_relation_depth: Maximum permitted relationship hops.
        max_limit: Maximum permitted ``pagination.limit``.
        pager: Optional :class:`BasePager` validating pager-specific pagination keys.

    Raises:
        BadFilterFormatError: If *raw* is not a mapping or exceeds node count.
        InvalidQueryLimitError: If pagination bounds are invalid.
        FilterDepthExceededError: If filter or relation depth is exceeded.
    """
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise BadFilterFormatError("Query payload must be a JSON object.")

    pagination = raw.get("pagination")
    if pagination is not None:
        if pager is not None:
            pager.precheck_raw(pagination, max_limit=max_limit)
        else:
            if not isinstance(pagination, Mapping):
                raise InvalidQueryLimitError("`pagination` must be an object.")
            if "page" in pagination:
                page = pagination["page"]
                if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                    raise InvalidQueryLimitError("`pagination.page` must be an integer >= 1.")
            if "limit" in pagination:
                limit = pagination["limit"]
                if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_limit:
                    raise InvalidQueryLimitError(f"`pagination.limit` must be an integer between 1 and {max_limit}.")

    filter_node = raw.get("filter")
    if filter_node is not None:
        precheck_raw_filter(
            filter_node,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_relation_depth=max_relation_depth,
        )


def _clean_loc_segments(loc: tuple[Any, ...]) -> list[int | str]:
    """Strip internal Pydantic union/branch discriminator segments from *loc*."""
    cleaned: list[int | str] = []
    for segment in loc:
        if isinstance(segment, int):
            cleaned.append(segment)
        else:
            seg_str = str(segment)
            if seg_str.startswith(_INTERNAL_LOC_PREFIXES):
                continue
            cleaned.append(seg_str)
    return cleaned


def _format_loc(loc: tuple[Any, ...]) -> str:
    """Render a Pydantic error location tuple as a concise JSONPath string."""
    parts = ["$"]
    for segment in _clean_loc_segments(loc):
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
        else:
            parts.append(f".{segment}")
    return "".join(parts)


def _find_disallowed_identifier(
    raw_payload: Any,
    resolved: ResolvedModelPolicy,
    *,
    is_filter_only: bool = False,
) -> tuple[str, str, str] | None:
    """Walk *raw_payload* to locate the first disallowed field, relation, or sort key.

    Returns ``(kind, name, model_name)`` where *kind* is ``"field"`` or ``"relation"``,
    or ``None`` if every referenced identifier is allowlisted.
    """
    if not isinstance(raw_payload, Mapping):
        return None

    if not is_filter_only:
        sort_list = raw_payload.get("sort")
        if _is_sequence(sort_list):
            for entry in sort_list:
                if isinstance(entry, Mapping):
                    sort_field = entry.get("field")
                    if isinstance(sort_field, str) and sort_field not in resolved.sortable_expressions:
                        return ("field", sort_field, resolved.model.__name__)
        root_filter = raw_payload.get("filter")
    else:
        root_filter = raw_payload

    if root_filter is None:
        return None

    stack: list[tuple[Any, ResolvedModelPolicy]] = [(root_filter, resolved)]
    while stack:
        node, current = stack.pop()
        if not isinstance(node, Mapping):
            continue

        if "field" in node:
            field_name = node.get("field")
            if isinstance(field_name, str):
                allowed_scalar = {name for name, f in current.fields.items() if f.filterable} | set(
                    current.dotted_fields
                )
                if field_name not in allowed_scalar:
                    return ("field", field_name, current.model.__name__)

        if "relation" in node:
            rel_name = node.get("relation")
            if isinstance(rel_name, str):
                if rel_name not in current.relations:
                    return ("relation", rel_name, current.model.__name__)
                child_filter = node.get("filter")
                if child_filter is not None:
                    child_policy = resolve_model_policy(
                        current.relations[rel_name].target_model,
                        _collect_dotted=False,
                    )
                    stack.append((child_filter, child_policy))

        conditions = node.get("conditions")
        if _is_sequence(conditions):
            for child in reversed(conditions):
                stack.append((child, current))

        if "condition" in node and node["condition"] is not None:
            stack.append((node["condition"], current))

    return None


def _select_most_relevant_error(err: ValidationError) -> dict[str, Any]:
    """Select the most informative Pydantic error from a union validation failure."""
    errors = err.errors()
    if not errors:
        return {"loc": (), "msg": "Invalid query specification.", "type": "value_error"}

    priority_rank = {
        "too_long": 5,
        "too_short": 5,
        "greater_than_equal": 5,
        "less_than_equal": 5,
        "extra_forbidden": 4,
        "literal_error": 3,
    }
    concrete = [
        item for item in errors if item.get("type") not in ("missing", "union_tag_invalid", "union_tag_not_found")
    ]
    candidates = concrete or errors

    def _score(item: Any) -> tuple[int, int]:
        depth = len(_clean_loc_segments(item.get("loc", ())))
        rank = priority_rank.get(str(item.get("type", "")), 2)
        return (depth, rank)

    return dict(max(candidates, key=_score))


def normalize_validation_error(
    err: ValidationError,
    *,
    resolved: ResolvedModelPolicy,
    raw_payload: Any = None,
    is_filter_only: bool = False,
) -> BedrockExc:
    """Translate a Pydantic ``ValidationError`` into a Bedrock query exception.

    Preserves SEC-01 by default (``reveal_allowed_fields=False``): hidden and
    non-existent fields produce identical ``BadFilterFormatError`` messages
    without enumerating allowed field names.

    Args:
        err: The ``ValidationError`` raised by Pydantic.
        resolved: The resolved model policy used for validation.
        raw_payload: Original unvalidated dict payload (used for SEC-01 redaction).
        is_filter_only: ``True`` when validating a standalone ``FilterNode``.

    Returns:
        An appropriate :class:`~bedrock.exc.BedrockExc` subclass instance.
    """
    for item in err.errors():
        loc = item.get("loc", ())
        if loc and loc[0] == "pagination":
            path = _format_loc(loc)
            return InvalidQueryLimitError(f"Invalid pagination at {path}: {item.get('msg', 'invalid value')}.")

    if not resolved.policy.reveal_allowed_fields:
        disallowed = _find_disallowed_identifier(raw_payload, resolved, is_filter_only=is_filter_only)
        if disallowed is not None:
            kind, bad_name, model_name = disallowed
            label = "Relation" if kind == "relation" else "Field"
            return BadFilterFormatError(f"{label} `{bad_name}` is not allowed on {model_name}.")

        selected = _select_most_relevant_error(err)
        path = _format_loc(selected.get("loc", ()))
        err_type = selected.get("type")
        if err_type in ("literal_error", "union_tag_invalid", "union_tag_not_found"):
            return BadFilterFormatError(f"Invalid query value at {path}.")
        if err_type == "extra_forbidden":
            return BadFilterFormatError(f"Unexpected key at {path}.")
        return BadFilterFormatError(f"Invalid query specification at {path}: {selected.get('msg', 'invalid value')}.")

    selected = _select_most_relevant_error(err)
    path = _format_loc(selected.get("loc", ()))
    return BadFilterFormatError(f"Invalid query at {path}: {selected.get('msg', 'invalid value')}.")
