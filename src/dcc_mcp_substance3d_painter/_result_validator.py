"""Private, isolated result validator for materialized Painter scripts."""

from __future__ import annotations

from typing import Any

MAX_SCRIPT_RESULT_DEPTH = 64
MAX_SCRIPT_RESULT_NODES = 10_000
MAX_SCRIPT_RESULT_JSON_BYTES = 256 * 1024


def normalize_result(
    value: Any,
    depth: int = 1,
    *,
    enforce_shape_budget: bool = True,
) -> tuple[Any, int]:
    """Normalize one strict portable result without adapter-module globals."""

    # Keep the safety budget in the validator's call-local state.  The
    # materialized namespace can import this module, but rebinding these
    # exported documentation constants cannot alter an in-flight validation.
    depth_limit = 64
    node_limit = 10_000
    byte_limit = 256 * 1024

    function_type = type
    dict_type = dict
    list_type = list
    string_type = str
    bool_type = bool
    int_type = int
    float_type = float
    length = len
    ordinal = ord
    render_int = str
    render_float = repr

    def finite_number(item: Any) -> bool:
        return item == item and -1.7976931348623157e308 <= item <= 1.7976931348623157e308

    node_count = 0

    def reject_result() -> None:
        raise ValueError("script_result_invalid")

    def json_string_size(item: str) -> int:
        size = 2
        try:
            for character in item:
                codepoint = ordinal(character)
                if character in {'"', "\\"} or character in {"\b", "\t", "\n", "\f", "\r"}:
                    size += 2
                elif codepoint < 0x20:
                    size += 6
                else:
                    size += length(character.encode("utf-8"))
                if size > byte_limit:
                    reject_result()
        except UnicodeEncodeError:
            reject_result()
        return size

    def normalize(item: Any, item_depth: int = 1, *, enforce_budget: bool = True) -> tuple[Any, int]:
        nonlocal node_count
        value_kind = function_type(item)
        if enforce_budget and value_kind in {dict_type, list_type} and item_depth > depth_limit:
            reject_result()
        if enforce_budget:
            node_count += 1
            if node_count > node_limit:
                reject_result()
        if item is None:
            return None, 4
        if value_kind is string_type:
            return item, json_string_size(item)
        if value_kind is bool_type:
            return item, 4 if item else 5
        if value_kind is int_type:
            try:
                return item, length(render_int(item).encode("ascii"))
            except (UnicodeEncodeError, ValueError):
                reject_result()
        if value_kind is float_type:
            if not finite_number(item):
                reject_result()
            return item, length(render_float(item).encode("ascii"))
        if value_kind is list_type:
            normalized_list: list[Any] = []
            byte_size = 2
            for index, child in enumerate(item):
                normalized_item, item_size = normalize(child, item_depth + 1, enforce_budget=enforce_budget)
                normalized_list.append(normalized_item)
                byte_size += item_size + (1 if index else 0)
                if byte_size > byte_limit:
                    reject_result()
            return normalized_list, byte_size
        if value_kind is dict_type:
            normalized_dict: dict[str, Any] = {}
            byte_size = 2
            for index, (key, child) in enumerate(item.items()):
                if function_type(key) is not string_type:
                    reject_result()
                normalized_item, item_size = normalize(child, item_depth + 1, enforce_budget=enforce_budget)
                normalized_dict[key] = normalized_item
                byte_size += json_string_size(key) + 1 + item_size + (1 if index else 0)
                if byte_size > byte_limit:
                    reject_result()
            return normalized_dict, byte_size
        reject_result()

    return normalize(value, depth, enforce_budget=enforce_shape_budget)


__all__ = ["normalize_result"]
