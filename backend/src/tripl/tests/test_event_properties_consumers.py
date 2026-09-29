"""Typed event properties read by export, codegen and ``tripl check`` (F23.7, #306)."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient

from tripl.core.event_properties import (
    EventProperty,
    event_properties,
    leaf_schema,
    object_schema,
    type_accepts,
)
from tripl.core.plan_validation import (
    EventContext,
    PlanEvent,
    PlanEventType,
    PlanField,
    PlanSnapshot,
    ValidationItem,
    check_item,
    resolve_item,
)

TEMPLATE = json.dumps(
    {
        "plan": "${plan}",
        "amount": "${amount}",
        "cart": {"items": "${items}"},
        "schema_version": 2,
        "meta": "${meta}",
        "screen": "screen_${plan}",
    }
)
TYPES = {
    "plan": ("string", None),
    "amount": ("number", {"type": "integer", "minimum": 0}),
    "items": ("string_array", None),
    "meta": ("json", {"type": "object"}),
}


def _props() -> list[EventProperty]:
    return event_properties(
        {"properties": TEMPLATE, "screen": "home"},
        ["properties"],
        token_types=TYPES,
        required_tokens={"plan", "items", "meta"},
        allowed_for=lambda token: ("free", "pro") if token == "plan" else (),
    )


def test_every_leaf_of_a_json_template_is_a_property() -> None:
    props = {p.path: p for p in _props()}
    assert list(props) == ["amount", "cart.items", "meta", "plan", "schema_version", "screen"]
    assert props["screen"].template == "screen_${plan}" and props["screen"].token is None
    assert props["plan"].required and props["cart.items"].required
    assert not props["amount"].required
    assert props["schema_version"].token is None and props["schema_version"].literal == 2
    assert props["amount"].json_schema == {"type": "integer", "minimum": 0}


def test_the_object_schema_nests_and_lists_what_is_required() -> None:
    schema = object_schema(_props())
    assert schema == {
        "type": "object",
        "properties": {
            "amount": {"type": "integer", "minimum": 0},
            "cart": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                "required": ["items"],
            },
            "meta": {"type": "object"},
            "plan": {"type": "string", "enum": ["free", "pro"]},
            "schema_version": {"const": 2},
            "screen": {"type": "string"},
        },
        "required": ["cart", "meta", "plan"],
    }


def test_numbers_and_booleans_become_typed_enums() -> None:
    number = EventProperty("p", "n", token="n", variable_type="number", allowed=("1", "2.5"))
    boolean = EventProperty("p", "b", token="b", variable_type="boolean", allowed=("true",))
    untyped = EventProperty("p", "u", token="u", variable_type="number", allowed=("x",))
    assert leaf_schema(number) == {"type": "number", "enum": [1, 2.5]}
    assert leaf_schema(boolean) == {"type": "boolean", "enum": [True]}
    # A documented value the type cannot carry leaves the enum out rather than lie.
    assert leaf_schema(untyped) == {"type": "number"}


@pytest.mark.parametrize(
    ("variable_type", "value", "ok"),
    [
        ("number", 3, True),
        ("number", "3", False),
        ("boolean", True, True),
        ("boolean", 1, False),
        ("string_array", ["a"], True),
        ("json", {"a": 1}, True),
        ("string", 1, False),
    ],
)
def test_type_accepts(variable_type: str, value: object, ok: bool) -> None:
    assert type_accepts(variable_type, value) is ok


# --- tripl check ---------------------------------------------------------------

TYPE_ID = uuid.uuid4()
EVENT = PlanEvent(
    id=uuid.uuid4(), event_type_id=TYPE_ID, name="purchase", identity="purchase", status="live"
)
PLAN = PlanSnapshot(
    types_by_name={
        "track": PlanEventType(
            id=TYPE_ID,
            name="track",
            name_format="{event}",
            fields={
                "event": PlanField("event", "string", is_required=True),
                "properties": PlanField("properties", "json"),
            },
        )
    },
    events=[EVENT],
    variable_allowed={"plan": ("free", "pro"), "amount": (), "items": (), "meta": ()},
    variable_types=TYPES,
)
CONTEXT = EventContext(
    field_values={"properties": TEMPLATE}, required_tokens=frozenset({"plan", "items", "meta"})
)


def _check(
    properties: dict | None, *, complete: bool = False, fields: dict | None = None
) -> list[tuple[str, str, str | None]]:
    item = ValidationItem(
        ref="a",
        event_type="track",
        fields={"event": "purchase", **(fields or {})},
        properties=properties,
        complete=complete,
    )
    res = resolve_item(item, PLAN)
    assert res.event is not None
    return [(f.code, f.severity, f.field) for f in check_item(res, PLAN, CONTEXT)]


def test_property_keys_are_not_unknown_fields() -> None:
    assert _check({"plan": "pro", "amount": 3, "cart": {"items": ["a"]}}) == []
    # Sent under the JSON field's own name, the keys are the same properties.
    assert _check({"properties": {"plan": "pro", "amount": 3}}) == []


def test_a_property_of_the_wrong_type_or_value_is_an_error() -> None:
    assert _check({"amount": "3"}) == [("wrong_type", "error", "amount")]
    assert _check({"plan": "gold"}) == [("value_not_allowed", "error", "plan")]
    # A json property holds an object, checked whole.
    assert _check({"meta": {"a": 1}}) == []
    assert _check({"meta": 3}) == [("wrong_type", "error", "meta")]
    # A mixed template checks its shape's variables; a stored literal is a
    # sample, not a rule.
    assert _check({"screen": "screen_pro"}) == []
    assert _check({"screen": "screen_gold"}) == [("value_not_allowed", "error", "screen")]
    assert _check({"schema_version": 3}) == []


def test_a_complete_payload_must_carry_the_required_properties() -> None:
    assert _check({"plan": "pro", "meta": {}}, complete=True) == [
        ("missing_required_field", "error", "cart.items")
    ]
    # Properties sent as null (a size limit) say nothing about what is missing.
    assert _check(None, complete=True) == []


def test_a_property_sent_as_a_field_is_checked_as_text() -> None:
    assert _check({}, fields={"amount": "3", "plan": "gold"}) == [
        ("value_not_allowed", "error", "plan")
    ]


def test_a_key_that_is_neither_field_nor_property_is_still_unknown() -> None:
    assert _check({"coupon": "x"}) == [("unknown_field", "warning", "coupon")]


# --- API: export ---------------------------------------------------------------


async def _seed(client: AsyncClient, slug: str) -> None:
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    field = await client.post(
        f"/api/v1/projects/{slug}/event-types/{et.json()['id']}/fields",
        json={"name": "properties", "display_name": "Properties", "field_type": "json"},
    )
    plan = await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "plan", "allowed_values": ["free", "pro"]},
    )
    await client.post(
        f"/api/v1/projects/{slug}/variables",
        json={"name": "amount", "variable_type": "number", "json_schema": {"type": "integer"}},
    )
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": et.json()["id"],
            "name": "purchase",
            "field_values": [
                {
                    "field_definition_id": field.json()["id"],
                    "value": json.dumps({"plan": "${plan}", "amount": "${amount}"}),
                }
            ],
        },
    )
    assert event.status_code == 201, event.text
    await client.put(
        f"/api/v1/projects/{slug}/variables/{plan.json()['id']}"
        f"/event-overrides/{event.json()['id']}",
        json={"required": True},
    )


@pytest.mark.asyncio
async def test_the_json_schema_export_types_the_properties(client: AsyncClient) -> None:
    slug = "props-export"
    await _seed(client, slug)
    resp = await client.get(f"/api/v1/projects/{slug}/plan/export?format=jsonschema")
    assert resp.status_code == 200, resp.text
    [schema] = resp.json()["schemas"].values()
    assert schema["properties"]["properties"] == {
        "title": "Properties",
        "type": "object",
        "properties": {
            "amount": {"type": "integer"},
            "plan": {"type": "string", "enum": ["free", "pro"]},
        },
        "required": ["plan"],
    }


@pytest.mark.asyncio
async def test_the_codegen_model_lists_typed_properties(client: AsyncClient) -> None:
    slug = "props-codegen"
    await _seed(client, slug)
    resp = await client.get(f"/api/v1/projects/{slug}/plan/export?format=codegen_model")
    assert resp.status_code == 200, resp.text
    [event] = resp.json()["event_types"][0]["events"]
    props = {p["path"]: p for p in event["properties"]}
    assert props["plan"] == {
        "field": "properties",
        "path": "plan",
        "variable": "plan",
        "type": "string",
        "json_schema": {"type": "string", "enum": ["free", "pro"]},
        "required": True,
        "values": ["free", "pro"],
        "literal": None,
    }
    assert props["amount"]["type"] == "number" and not props["amount"]["required"]
    variables = {v["name"]: v for v in resp.json()["variables"]}
    assert variables["amount"]["json_schema"] == {"type": "integer"}
