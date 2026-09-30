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

import copy
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


# The only keywords an inferred object sub-schema uses (F23.4e). A schema using
# anything else — a description, an enum, a bound, a pattern — has a person's
# hand in it.
_INFERRED_KEYWORDS = frozenset({"type", "format", "items", "properties", "required"})


def _is_inference_shaped(node: object) -> bool:
    if not isinstance(node, Mapping) or set(node) - _INFERRED_KEYWORDS:
        return False
    schema_type = node.get("type")
    if schema_type not in ("string", "number", "boolean", "array", "object"):
        return False
    if "format" in node and (
        schema_type != "string" or node["format"] not in ("date", "date-time")
    ):
        return False
    if "items" in node and (schema_type != "array" or not _is_inference_shaped(node["items"])):
        return False
    if ("properties" in node or "required" in node) and schema_type != "object":
        return False
    properties = node.get("properties", {})
    if not isinstance(properties, Mapping):
        return False
    return all(_is_inference_shaped(sub) for sub in properties.values())


def is_scan_inferred_schema(schema: Mapping[str, Any] | None) -> bool:
    """Whether *schema* says only what a scan could have inferred.

    Besides the fixed fragments, an object schema built from the grammar
    ``infer_property_type`` writes for sampled objects — types, date formats,
    item types, properties, ``required`` — reads as the scan's. The
    approximation is one-directional, like the fixed fragments: a person who
    only retyped a nested key or edited ``required`` is not told apart, and such
    a property counts as the scan's in the retirement sweep. What a person
    usually adds to a schema (a description, an enum, a bound) marks it theirs.
    """
    if schema is None or dict(schema) in SCAN_INFERRED_SCHEMAS:
        return True
    return schema.get("type") == "object" and _is_inference_shaped(schema)


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
        schema = _object_node([value for value in present if isinstance(value, dict)], depth=1)
        if len(json.dumps(schema, ensure_ascii=False).encode()) > SCHEMA_MAX_BYTES:
            return "json", {"type": "object"}
        return "json", schema
    return None


# --- Object sub-schemas (F23.4e) ----------------------------------------------
#
# A nested object is one property whose sub-schema is inferred from sampled
# objects: every key a sample carried, each typed from its values the way a
# top-level property is, and ``required`` naming the keys EVERY sample carried
# with a value. A key whose values disagree about their kind (or are all null)
# is left out rather than guessed, the rule a top-level property follows too.

_NODE_BY_KIND: dict[str, dict[str, Any]] = {
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "string": {"type": "string"},
}

_PROPERTY_NAME_MAX = 200


def _node_for(values: Sequence[object], depth: int) -> dict[str, Any] | None:
    """The schema node a sample of one key's values supports, or ``None``."""
    present = [value for value in values if value is not None]
    kinds = {_kind(value) for value in present}
    if not kinds or "unknown" in kinds:
        return None
    if len(kinds) == 1 and next(iter(kinds)) in _NODE_BY_KIND:
        return dict(_NODE_BY_KIND[next(iter(kinds))])
    if kinds <= {"date", "datetime"}:
        return dict(_NODE_BY_KIND["datetime"])
    if kinds <= {"string", "date", "datetime"}:
        return dict(_NODE_BY_KIND["string"])
    if kinds == {"array"}:
        items = [item for value in present if isinstance(value, list) for item in value]
        item_node = _node_for(items, depth + 1) if depth < SCHEMA_MAX_DEPTH else None
        return {"type": "array", "items": item_node} if item_node else {"type": "array"}
    if kinds == {"object"}:
        return _object_node([value for value in present if isinstance(value, dict)], depth)
    return None


def _object_node(objects: Sequence[Mapping[str, Any]], depth: int) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "object"}
    if depth >= SCHEMA_MAX_DEPTH:
        return node
    keys = sorted(
        {str(key) for obj in objects for key in obj if 0 < len(str(key)) <= _PROPERTY_NAME_MAX}
    )
    properties: dict[str, Any] = {}
    required: list[str] = []
    for key in keys:
        values = [obj[key] for obj in objects if key in obj]
        sub = _node_for(values, depth + 1)
        if sub is None:
            continue
        properties[key] = sub
        if len(values) == len(objects) and all(value is not None for value in values):
            required.append(key)
    if properties:
        node["properties"] = properties
    if required:
        node["required"] = required
    return node


def _node_kind(node: Mapping[str, Any]) -> str:
    """A schema node's type, spelled the way ``_kind`` spells a value's."""
    schema_type = str(node.get("type"))
    if schema_type == "string":
        return {"date": "date", "date-time": "datetime"}.get(str(node.get("format")), "string")
    return "number" if schema_type == "integer" else schema_type


def _node_admits(node: Mapping[str, Any], value_kind: str) -> bool:
    expected = _node_kind(node)
    if expected == value_kind:
        return True
    if expected == "string":
        return value_kind in ("date", "datetime")
    return expected == "datetime" and value_kind == "date"


# A path inside an object property: key names, ``None`` for "an array's items".
_SchemaPath = tuple[str | None, ...]


def _path_text(path: _SchemaPath) -> str:
    text = ""
    for part in path:
        text += "[]" if part is None else (f".{part}" if text else part)
    return text


def _node_at(schema: dict[str, Any], path: _SchemaPath) -> dict[str, Any] | None:
    node: Any = schema
    for part in path:
        child = node.get("items") if part is None else (node.get("properties") or {}).get(part)
        if not isinstance(child, dict):
            return None
        node = child
    return node if isinstance(node, dict) else None


def object_schema_changes(
    schema: Mapping[str, Any], samples: Sequence[object]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Key drift of sampled objects against an object property's sub-schema.

    Returns ``(changes, merged)``. Each change is ``{"path", "change"}`` (plus
    ``expected_type`` and ``observed_type`` for a type change), where
    ``change`` is

    * ``new_key`` — a sample carried a key its object lists no property for (an
      object node without ``properties`` describes nothing, so admits any key);
    * ``missing_required`` — a sample lacked a key its object's ``required``
      names, or carried it as null;
    * ``type_change`` — a value of a kind its node does not admit.

    ``merged`` is *schema* with the changes applied — a new key typed from its
    values, a changed node retyped, a missing key no longer required — and is
    what accepting the finding writes, so a person's other annotations survive.
    Pure; ``samples`` are decoded JSON values.
    """
    found: dict[tuple[_SchemaPath, str], list[object]] = {}
    expected_at: dict[_SchemaPath, str] = {}

    def walk(node: Mapping[str, Any], value: object, path: _SchemaPath) -> None:
        if value is None:
            return
        if not _node_admits(node, _kind(value)):
            found.setdefault((path, "type_change"), []).append(value)
            expected_at[path] = _node_kind(node)
            return
        if isinstance(value, dict):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                for key, sub_value in value.items():
                    sub = properties.get(key)
                    if isinstance(sub, Mapping):
                        walk(sub, sub_value, (*path, str(key)))
                    elif sub_value is not None:
                        found.setdefault(((*path, str(key)), "new_key"), []).append(sub_value)
            for key in node.get("required") or ():
                if value.get(key) is None:
                    found.setdefault(((*path, str(key)), "missing_required"), [])
        elif isinstance(value, list) and isinstance(node.get("items"), Mapping):
            for item in value:
                walk(node["items"], item, (*path, None))

    for sample in samples:
        walk(schema, sample, ())

    changes: list[dict[str, Any]] = []
    merged = copy.deepcopy(dict(schema))
    for (path, change), values in sorted(
        found.items(), key=lambda item: (_path_text(item[0][0]), item[0][1])
    ):
        entry: dict[str, Any] = {"path": _path_text(path), "change": change}
        observed = _node_for(values, len(path) + 1) if values else None
        if change == "new_key" and observed is None:
            # Values of mixed kinds: inference leaves such a key out of the
            # sub-schema rather than guess, so it is no news here either.
            continue
        if change == "type_change":
            entry["expected_type"] = expected_at[path]
            entry["observed_type"] = _node_kind(observed) if observed else "mixed"
        changes.append(entry)
        if not path:
            # Only a type change reaches the root, and a whole object turning
            # into something else is the top-level ``type_change``'s business.
            continue
        parent = _node_at(merged, path[:-1])
        if parent is None:
            continue
        key = path[-1]
        if change == "missing_required":
            required = [name for name in parent.get("required") or () if name != key]
            if required:
                parent["required"] = required
            else:
                parent.pop("required", None)
        elif observed is not None:
            if key is None:
                parent["items"] = observed
            else:
                parent.setdefault("properties", {})[key] = observed
    return changes, merged
