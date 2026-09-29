"""An event's typed properties, read off its JSON field templates (F23, #306).

A JSON field of an event stores a template such as
``{"amount": "${amount}", "cart": {"items": "${cart_items}"}, "v": 2}``. Each
leaf is a property: a whole ``${token}`` names the variable that types it, any
other leaf is a literal the event always sends. The property's type is the
variable's ``variable_type`` refined by its ``json_schema``, and it is required
when the event's property list marks the variable required.

Pure and shared: the JSON Schema export, the codegen model and ``tripl check``
all read properties through here, so the three can never disagree about what
an event carries.
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from tripl.core.name_template import VARIABLE_TOKEN_PATTERN
from tripl.json_paths import flatten_json_paths

# variable token -> (variable_type, json_schema)
TokenTypes = Mapping[str, tuple[str, Mapping[str, Any] | None]]

# The JSON Schema a ``variable_type`` stands for when the variable has none.
_BASE_SCHEMA: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "json": {},
    "string_array": {"type": "array", "items": {"type": "string"}},
    "number_array": {"type": "array", "items": {"type": "number"}},
}

# The ``variable_type`` whose values are one scalar, and so can be an enum.
_SCALAR_TYPES = frozenset({"string", "number", "boolean", "date", "datetime"})


@dataclass(frozen=True)
class EventProperty:
    field: str
    path: str
    # The variable token a ``${token}`` leaf names; ``None`` for a literal leaf.
    token: str | None = None
    literal: Any = None
    # A leaf mixing text and tokens (``screen_${name}``): a string whose shape
    # the template gives. ``token`` and ``literal`` are both ``None``.
    template: str | None = None
    variable_type: str | None = None
    json_schema: Mapping[str, Any] | None = None
    required: bool = False
    allowed: tuple[str, ...] = ()


def whole_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = VARIABLE_TOKEN_PATTERN.fullmatch(value)
    return match.group(1) if match is not None else None


def parse_template(value: str | None) -> dict[str, Any] | None:
    """The stored JSON field value as an object, or ``None`` when it is not one."""
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except TypeError, ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def event_properties(
    field_values: Mapping[str, str],
    json_fields: Collection[str],
    *,
    token_types: TokenTypes,
    required_tokens: Collection[str],
    allowed_for: Callable[[str], tuple[str, ...]],
) -> list[EventProperty]:
    """Every property of one event, by field then path."""
    properties: list[EventProperty] = []
    for field_name in sorted(json_fields):
        template = parse_template(field_values.get(field_name))
        if template is None:
            continue
        for path, leaf in sorted(flatten_json_paths(template), key=lambda item: item[0]):
            token = whole_token(leaf)
            if token is None and isinstance(leaf, str) and VARIABLE_TOKEN_PATTERN.search(leaf):
                properties.append(EventProperty(field=field_name, path=path, template=leaf))
                continue
            if token is None:
                properties.append(EventProperty(field=field_name, path=path, literal=leaf))
                continue
            variable_type, schema = token_types.get(token, (None, None))
            properties.append(
                EventProperty(
                    field=field_name,
                    path=path,
                    token=token,
                    variable_type=variable_type,
                    json_schema=schema,
                    required=token in required_tokens,
                    allowed=allowed_for(token),
                )
            )
    return properties


def typed_allowed(variable_type: str | None, values: Collection[str]) -> list[Any] | None:
    """The documented values as the JSON a payload carries; ``None`` if one cannot be."""
    out: list[Any] = []
    for value in values:
        if variable_type == "number":
            try:
                number = float(value)
            except ValueError:
                return None
            if not math.isfinite(number):
                return None
            out.append(int(number) if number.is_integer() and "." not in value else number)
        elif variable_type == "boolean":
            if value not in ("true", "false"):
                return None
            out.append(value == "true")
        else:
            out.append(value)
    return list(dict.fromkeys(out))


def leaf_schema(prop: EventProperty) -> dict[str, Any]:
    """The JSON Schema of one property."""
    if prop.template is not None:
        return {"type": "string"}
    if prop.token is None:
        return {"const": prop.literal}
    if prop.variable_type is None:
        return {}
    schema = copy.deepcopy(dict(prop.json_schema or _BASE_SCHEMA.get(prop.variable_type, {})))
    if prop.allowed and prop.variable_type in _SCALAR_TYPES:
        typed = typed_allowed(prop.variable_type, prop.allowed)
        if typed:
            schema["enum"] = typed
    return schema


def object_schema(properties: Collection[EventProperty]) -> dict[str, Any]:
    """One JSON field's properties as a nested object schema with ``required`` lists."""
    root: dict[str, Any] = {"type": "object", "properties": {}}
    for prop in properties:
        node = root
        parts = prop.path.split(".")
        for part in parts[:-1]:
            child = node["properties"].setdefault(part, {"type": "object", "properties": {}})
            if prop.required and part not in node.setdefault("required", []):
                node["required"].append(part)
            node = child
        node["properties"][parts[-1]] = leaf_schema(prop)
        if prop.required:
            node.setdefault("required", []).append(parts[-1])
    return root


def kind_of(value: object) -> str:
    """The payload kind of a JSON value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


# ``variable_type`` -> the payload kinds that satisfy it.
_ACCEPTS: dict[str, frozenset[str]] = {
    "string": frozenset({"string"}),
    "number": frozenset({"number"}),
    "boolean": frozenset({"boolean"}),
    "date": frozenset({"string"}),
    "datetime": frozenset({"string"}),
    "json": frozenset({"object", "array"}),
    "string_array": frozenset({"array"}),
    "number_array": frozenset({"array"}),
}


def type_accepts(variable_type: str, value: object) -> bool:
    accepted = _ACCEPTS.get(variable_type)
    return accepted is None or kind_of(value) in accepted
