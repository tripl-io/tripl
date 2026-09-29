"""JSON Schema fragments on variables (F23, #306).

A variable's ``variable_type`` is the coarse kind every reader already knows;
``json_schema`` refines it the way JSON Schema spells a property: an integer
instead of a number, a date-time format, the item type of an array, the
sub-schema of an object. Nested objects are one variable with a sub-schema,
never dotted variables (owner decision 5).

Only a subset of JSON Schema is accepted, and anything outside it is refused
by name rather than stored and ignored: no ``$ref``, no combinators, one
``type`` per node (one type per property, owner decision 4). Documented values
are not part of the fragment at the top level — they stay in
``allowed_values`` and the per-event overrides, and an export emits them as
``enum``, so the list is never kept twice. A nested node has no such list of
its own and may carry ``enum``.

Pure functions, no I/O: the request schemas validate the shape, the variable
service checks the fragment against the type the row will end up with.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SCHEMA_MAX_BYTES = 16_384
SCHEMA_MAX_DEPTH = 8
PATTERN_MAX_LENGTH = 500
ENUM_MAX_ITEMS = 500

SCHEMA_TYPES = ("string", "number", "integer", "boolean", "array", "object")

STRING_FORMATS = frozenset(
    {"date", "date-time", "time", "duration", "email", "uri", "uuid", "hostname", "ipv4", "ipv6"}
)

_COMMON_KEYWORDS = frozenset({"type", "description"})
_KEYWORDS_BY_TYPE: dict[str, frozenset[str]] = {
    "string": frozenset({"format", "pattern", "minLength", "maxLength", "enum"}),
    "number": frozenset(
        {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "enum"}
    ),
    "boolean": frozenset(),
    "array": frozenset({"items", "minItems", "maxItems", "uniqueItems"}),
    "object": frozenset({"properties", "required", "additionalProperties"}),
}
_KEYWORDS_BY_TYPE["integer"] = _KEYWORDS_BY_TYPE["number"]

# The ``format`` a date-like variable type pins, and the reason a plain string
# may not use it: one spelling per type.
_DATE_FORMATS = {"date": "date", "datetime": "date-time"}


class PropertySchemaError(ValueError):
    """The fragment is not one tripl accepts; the message names the node."""


def _fail(path: str, message: str) -> PropertySchemaError:
    return PropertySchemaError(f"json_schema{path}: {message}")


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _matches_type(value: object, schema_type: str) -> bool:
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return _is_number(value)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return False


def _check_bounds(node: Mapping[str, Any], path: str, low: str, high: str, *, count: bool) -> None:
    for key in (low, high):
        if key in node:
            valid = _is_non_negative_int(node[key]) if count else _is_number(node[key])
            if not valid:
                kind = "a non-negative integer" if count else "a number"
                raise _fail(path, f"'{key}' must be {kind}")
    if low in node and high in node and node[low] > node[high]:
        raise _fail(path, f"'{low}' is greater than '{high}'")


def _check_node(node: object, path: str, depth: int, *, top_level: bool) -> None:
    if depth > SCHEMA_MAX_DEPTH:
        raise _fail(path, f"nested deeper than {SCHEMA_MAX_DEPTH} levels")
    if not isinstance(node, dict):
        raise _fail(path, "must be an object")
    schema_type = node.get("type")
    if schema_type not in SCHEMA_TYPES:
        raise _fail(path, f"'type' must be one of {', '.join(SCHEMA_TYPES)}")
    allowed = _COMMON_KEYWORDS | _KEYWORDS_BY_TYPE[schema_type]
    unknown = sorted(set(node) - allowed)
    if unknown:
        raise _fail(path, f"unsupported keyword(s) for type {schema_type}: {', '.join(unknown)}")
    if "description" in node and not isinstance(node["description"], str):
        raise _fail(path, "'description' must be a string")

    if "enum" in node:
        if top_level:
            raise _fail(
                path, "document allowed values in 'allowed_values', not in the schema's 'enum'"
            )
        members = node["enum"]
        if not isinstance(members, list) or not members or len(members) > ENUM_MAX_ITEMS:
            raise _fail(path, f"'enum' must be a list of 1 to {ENUM_MAX_ITEMS} values")
        if any(not _matches_type(member, schema_type) for member in members):
            raise _fail(path, f"every 'enum' value must be a {schema_type}")

    if schema_type == "string":
        if "format" in node and node["format"] not in STRING_FORMATS:
            raise _fail(path, f"'format' must be one of {', '.join(sorted(STRING_FORMATS))}")
        if "pattern" in node:
            pattern = node["pattern"]
            if not isinstance(pattern, str) or not pattern or len(pattern) > PATTERN_MAX_LENGTH:
                raise _fail(path, f"'pattern' must be a regex of 1 to {PATTERN_MAX_LENGTH} chars")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise _fail(path, f"'pattern' is not a valid regex: {exc}") from None
        _check_bounds(node, path, "minLength", "maxLength", count=True)
    elif schema_type in ("number", "integer"):
        _check_bounds(node, path, "minimum", "maximum", count=False)
        _check_bounds(node, path, "exclusiveMinimum", "exclusiveMaximum", count=False)
        if "multipleOf" in node and not (_is_number(node["multipleOf"]) and node["multipleOf"] > 0):
            raise _fail(path, "'multipleOf' must be a positive number")
    elif schema_type == "array":
        _check_bounds(node, path, "minItems", "maxItems", count=True)
        if "uniqueItems" in node and not isinstance(node["uniqueItems"], bool):
            raise _fail(path, "'uniqueItems' must be a boolean")
        if "items" in node:
            _check_node(node["items"], f"{path}.items", depth + 1, top_level=False)
    elif schema_type == "object":
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            raise _fail(path, "'properties' must be an object")
        for key, sub in properties.items():
            if not key or len(key) > 200:
                raise _fail(path, "property names must be 1 to 200 characters")
            _check_node(sub, f"{path}.properties.{key}", depth + 1, top_level=False)
        if "required" in node:
            required = node["required"]
            if (
                not isinstance(required, list)
                or any(not isinstance(key, str) for key in required)
                or len(set(required)) != len(required)
            ):
                raise _fail(path, "'required' must be a list of distinct property names")
            undeclared = sorted(set(required) - set(properties))
            if undeclared:
                raise _fail(
                    path, f"'required' names undeclared properties: {', '.join(undeclared)}"
                )
        if "additionalProperties" in node and not isinstance(node["additionalProperties"], bool):
            raise _fail(path, "'additionalProperties' must be a boolean")


def validate_property_schema(schema: object) -> dict[str, Any]:
    """Check the fragment's shape and return it unchanged.

    Raises ``PropertySchemaError`` naming the offending node. Says nothing
    about ``variable_type``; ``check_schema_matches_type`` does that.
    """
    try:
        # ``allow_nan=False``: the request parser admits NaN and Infinity, and
        # PostgreSQL's json type does not.
        size = len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode())
    except TypeError, ValueError:
        raise _fail("", "must be plain JSON") from None
    if size > SCHEMA_MAX_BYTES:
        raise _fail("", f"larger than {SCHEMA_MAX_BYTES} bytes")
    _check_node(schema, "", 1, top_level=True)
    assert isinstance(schema, dict)  # _check_node refused anything else
    return schema


def check_schema_matches_type(variable_type: str, schema: Mapping[str, Any] | None) -> None:
    """Refuse a fragment that says something other than ``variable_type``.

    ``variable_type`` stays the coarse kind, so the pair must agree: a
    ``number`` may narrow to ``integer``, a ``json`` is an object or an array,
    the array types pin their item type, and the date types pin their format.
    A plain ``string`` may not use a date format — that is what the date types
    are for, and two spellings of one type would split every reader.
    """
    if schema is None:
        return
    schema_type = schema.get("type")
    fmt = schema.get("format")
    items = schema.get("items")
    item_type = items.get("type") if isinstance(items, Mapping) else None

    if variable_type == "string":
        ok = schema_type == "string" and fmt not in ("date", "date-time")
        expected = "type string (use variable_type date or datetime for a date format)"
    elif variable_type in _DATE_FORMATS:
        ok = schema_type == "string" and fmt == _DATE_FORMATS[variable_type]
        expected = f"type string with format {_DATE_FORMATS[variable_type]}"
    elif variable_type == "number":
        ok = schema_type in ("number", "integer")
        expected = "type number or integer"
    elif variable_type == "boolean":
        ok = schema_type == "boolean"
        expected = "type boolean"
    elif variable_type == "json":
        ok = schema_type in ("object", "array")
        expected = "type object or array"
    elif variable_type == "string_array":
        ok = schema_type == "array" and item_type == "string"
        expected = "type array with items of type string"
    elif variable_type == "number_array":
        ok = schema_type == "array" and item_type in ("number", "integer")
        expected = "type array with items of type number or integer"
    else:
        raise PropertySchemaError(f"Unknown variable_type {variable_type!r}")
    if not ok:
        raise PropertySchemaError(
            f"json_schema does not match variable_type {variable_type}: expected {expected}"
        )


# --- Scan-time inference (F23.4) ---------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$"
)

# Every fragment ``infer_property_type`` can write. A scan-minted variable whose
# schema is one of these still carries only what the scan said, so the
# retirement sweep and diff housekeeping do not read it as a person's edit. A
# person who types exactly one of them by hand is indistinguishable — the same
# one-directional approximation the display-name check accepts.
SCAN_INFERRED_SCHEMAS: tuple[dict[str, Any], ...] = (
    {"type": "array"},
    {"type": "object"},
)


def is_scan_inferred_schema(schema: Mapping[str, Any] | None) -> bool:
    return schema is None or dict(schema) in SCAN_INFERRED_SCHEMAS


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        # Never ``integer``: ClickHouse renders a Float64 ``10.0`` as ``10``, so
        # a price sampled as 10, 20, 5 would be pinned to integer and its next
        # 9.99 read as drift. Narrowing to integer is left to a person.
        return "number"
    if isinstance(value, str):
        if _DATE_RE.match(value):
            return "date"
        if _DATETIME_RE.match(value):
            return "datetime"
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _array_type(arrays: Sequence[list[Any]]) -> tuple[str, dict[str, Any] | None]:
    """Called with at least one non-null item, so ``item_kinds`` is not empty."""
    item_kinds = {_kind(item) for array in arrays for item in array if item is not None}
    if item_kinds <= {"string", "date", "datetime"}:
        return "string_array", None
    if item_kinds == {"number"}:
        return "number_array", None
    return "json", {"type": "array"}


def infer_property_type(values: Sequence[object]) -> tuple[str, dict[str, Any] | None] | None:
    """The ``(variable_type, json_schema)`` a sample of JSON values supports.

    ``values`` are decoded JSON (``decode_json_path_value``), so a number is
    still a number here — the one place the kind survives, since a context
    stores every value as text. Nulls say nothing and are skipped. ``None``
    when nothing typed was seen or the kinds disagree (a string beside a
    number): one type per property, so a mixed sample is a conflict to report,
    never something to guess (owner decision 4).

    Dates are recognised by their ISO shape; a sample mixing dates and
    date-times reads as ``datetime``, and either beside free text as ``string``.
    """
    present = [value for value in values if value is not None]
    kinds = {_kind(value) for value in present}
    if not kinds or "unknown" in kinds:
        return None
    if kinds == {"number"}:
        return "number", None
    if kinds == {"boolean"}:
        return "boolean", None
    if kinds == {"date"}:
        return "date", None
    if kinds <= {"date", "datetime"}:
        return "datetime", None
    if kinds <= {"string", "date", "datetime"}:
        return "string", None
    if kinds == {"array"}:
        arrays = [value for value in present if isinstance(value, list)]
        if not any(item is not None for array in arrays for item in array):
            # Only empty arrays: no item type to go on, and the type is set once.
            return None
        return _array_type(arrays)
    if kinds == {"object"}:
        return "json", {"type": "object"}
    return None
