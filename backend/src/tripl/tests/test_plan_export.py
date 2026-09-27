"""Plan export (GH #262, F09): the pure shaping, then ``GET /plan/export``.

The first half runs ``services.plan_export_service`` builders on plain data:
typed literals, variable enums with per-event overrides, templated values as
anchored patterns and as expanded value lists, contracts next to plan values,
archived events left out and deprecated ones flagged. The second half drives
the route: the demo project (every non-archived demo event gets a schema, and
every schema round-trips through a validator), the codegen model, a branch,
determinism, the membership 404 and a viewer member.

The backend has no JSON Schema library among its dependencies, so the
round-trip uses ``_validate`` below: a small validator for exactly the
draft 2020-12 keywords the export emits (``type``, ``const``, ``enum``,
``pattern``, ``minimum``, ``maximum``, ``required``, ``properties``,
``allOf``; ``format`` and the annotations are ignored, as a 2020-12
validator ignores them by default). ``test_schemas_use_only_known_keywords``
pins that the export emits nothing outside that set.

Every name here is synthetic.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import update as sql_update

from tripl.core.plan_validation import (
    EventContext,
    PlanEvent,
    PlanEventType,
    PlanField,
    PlanSnapshot,
)
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.services.plan_export_service import (
    JSON_SCHEMA_DIALECT,
    ExportPlan,
    VariableInfo,
    build_codegen_model,
    build_json_schemas,
    expand_template,
    regex_escape,
    template_pattern,
    typed_value,
)
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_plan_validation import seed_validation_plan

PASSWORD = "Password123!"

# ---------------------------------------------------------------------------
# A minimal validator for the keywords the export emits
# ---------------------------------------------------------------------------

_KNOWN_KEYWORDS = frozenset(
    {
        "$schema",
        "title",
        "description",
        "deprecated",
        "type",
        "properties",
        "required",
        "const",
        "enum",
        "pattern",
        "minimum",
        "maximum",
        "format",
        "allOf",
        "x-tripl",
    }
)
_TYPES: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def _validate(schema: dict[str, Any], instance: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None and not _TYPES[expected](instance):
        return [f"{path}: not {expected}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: != const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum")
    if (
        "pattern" in schema
        and isinstance(instance, str)
        and re.search(schema["pattern"], instance) is None
    ):
        errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
    if isinstance(instance, int | float) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(instance, dict):
        errors.extend(
            f"{path}: missing {key}" for key in schema.get("required", []) if key not in instance
        )
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors.extend(_validate(sub, instance[key], f"{path}.{key}"))
    for sub in schema.get("allOf", []):
        errors.extend(_validate(sub, instance, path))
    return errors


def _walk_keywords(schema: dict[str, Any]) -> set[str]:
    keys = set(schema)
    for sub in schema.get("properties", {}).values():
        keys |= _walk_keywords(sub)
    for sub in schema.get("allOf", []):
        keys |= _walk_keywords(sub)
    return keys


def _sample(schema: dict[str, Any]) -> dict[str, Any] | None:
    """A payload the schema should accept, built from its own constraints.

    Every field with a ``const`` or an ``enum`` gets a value (the first enum
    value every ``allOf`` branch also allows); others are left out. ``None``
    when a required field has nothing to build a value from.
    """
    payload: dict[str, Any] = {}
    for key, sub in schema["properties"].items():
        if "const" in sub:
            payload[key] = sub["const"]
            continue
        if "enum" in sub:
            candidates = [
                v
                for v in sub["enum"]
                if all(v in branch.get("enum", [v]) for branch in sub.get("allOf", []))
            ]
            if candidates:
                payload[key] = candidates[0]
                continue
        if key in schema["required"]:
            return None
    return payload


# ---------------------------------------------------------------------------
# Pure shaping
# ---------------------------------------------------------------------------

SE = uuid.uuid4()
KIND = uuid.uuid4()


def _ev(identity: str, status: str = "live") -> PlanEvent:
    return PlanEvent(
        id=uuid.uuid4(), event_type_id=SE, name=identity.title(), identity=identity, status=status
    )


START = _ev("checkout:start")
ITEM = _ev("checkout:item_${kind}", status="deprecated")
GONE = _ev("checkout:gone", status="archived")
FIELDS = {
    "category": PlanField("category", "string", is_required=True),
    "action": PlanField("action", "string", regex="^[a-z_]+$"),
    "platform": PlanField("platform", "enum", enum_options=("ios", "android")),
    "amount": PlanField("amount", "number", min_value=0, max_value=100),
    "note": PlanField("note", "string"),
}


def _plan() -> ExportPlan:
    snapshot = PlanSnapshot(
        types_by_name={
            "se": PlanEventType(id=SE, name="se", name_format="{category}:{action}", fields=FIELDS)
        },
        events=[START, ITEM, GONE],
        variable_allowed={"kind": ("hat", "car"), "platform": ("ios", "web")},
        variable_tokens={KIND: ("kind",)},
    )
    contexts = {
        START.id: EventContext(
            field_values={
                "category": "checkout",
                "action": "start",
                "platform": "${platform}",
                "amount": "9.99",
            }
        ),
        ITEM.id: EventContext(
            field_values={
                "category": "checkout",
                "action": "item_${kind}",
                "platform": "${platform}",
                "note": "x",
            },
            overrides={"platform": ("android",)},
        ),
        GONE.id: EventContext(field_values={"category": "checkout", "action": "gone"}),
    }
    return ExportPlan(
        snapshot=snapshot,
        contexts=contexts,
        variables=[VariableInfo(id=KIND, name="Kind", allowed_values=("hat", "car"))],
    )


def test_typed_values_follow_the_field_type() -> None:
    assert typed_value("number", "9.99") == (True, 9.99)
    assert typed_value("number", "3") == (True, 3)
    assert typed_value("number", "abc") == (False, None)
    assert typed_value("number", "nan") == (False, None)
    assert typed_value("boolean", "True") == (True, True)
    assert typed_value("boolean", "yes") == (False, None)
    assert typed_value("json", '{"a": 1}') == (True, {"a": 1})
    assert typed_value("string", "Home Screen View") == (True, "Home Screen View")


def test_templates_become_anchored_patterns_and_expansions() -> None:
    assert template_pattern("item_${kind}", {"kind": ("hat", "c.r")}) == r"^item_(?:hat|c\.r)$"
    assert template_pattern("a+${free}", {}) == r"^a\+.*$"
    assert regex_escape("checkout:start (v2)") == r"checkout:start \(v2\)"
    assert expand_template("${a}-${b}", {"a": ("1", "2"), "b": ("x",)}) == ["1-x", "2-x"]
    assert expand_template("item_${free}", {}) is None
    assert expand_template("${a}${a}", {"a": ("1", "2")}, limit=3) is None


def test_schemas_exclude_archived_and_flag_deprecated() -> None:
    schemas = build_json_schemas(_plan())
    assert list(schemas) == ["se/checkout:start", "se/checkout:item_${kind}"]
    assert "deprecated" not in schemas["se/checkout:start"]
    assert schemas["se/checkout:item_${kind}"]["deprecated"] is True
    for schema in schemas.values():
        assert schema["$schema"] == JSON_SCHEMA_DIALECT
        assert schema["required"] == ["category"]


def test_schema_values_and_contracts() -> None:
    schemas = build_json_schemas(_plan())
    start = schemas["se/checkout:start"]["properties"]
    assert start["category"] == {"type": "string", "const": "checkout"}
    # Plan literal and contract regex both hold: the contract's copy is its own keyword.
    assert start["action"] == {"type": "string", "const": "start", "pattern": "^[a-z_]+$"}
    assert start["amount"] == {"type": "number", "const": 9.99, "minimum": 0, "maximum": 100}
    # The variable's list, and the field's enum options alongside it.
    assert start["platform"] == {
        "type": "string",
        "enum": ["ios", "web"],
        "allOf": [{"enum": ["ios", "android"]}],
    }
    assert start["note"] == {"type": "string"}

    item = schemas["se/checkout:item_${kind}"]["properties"]
    assert item["action"] == {
        "type": "string",
        "pattern": "^item_(?:hat|car)$",
        "allOf": [{"pattern": "^[a-z_]+$"}],
    }
    # The event's override REPLACES the global allowed values.
    assert item["platform"]["enum"] == ["android"]
    assert item["amount"] == {"type": "number", "minimum": 0, "maximum": 100}


def test_schemas_round_trip_through_the_validator() -> None:
    schemas = build_json_schemas(_plan())
    start = schemas["se/checkout:start"]
    good = {"category": "checkout", "action": "start", "platform": "ios", "amount": 9.99}
    assert _validate(start, good) == []
    assert _validate(start, {**good, "platform": "web"})  # not an enum option
    assert _validate(start, {**good, "amount": "9.99"})  # wrong type
    assert _validate(start, {"action": "start"})  # missing required
    item = schemas["se/checkout:item_${kind}"]
    assert _validate(item, {"category": "checkout", "action": "item_car"}) == []
    assert _validate(item, {"category": "checkout", "action": "item_boat"})


def test_codegen_model_closes_only_fully_known_string_fields() -> None:
    plan = _plan()
    event_types, variables = build_codegen_model(plan)
    (se,) = event_types
    assert se.name_rule == "{category}:{action}"
    assert [ev.identity for ev in se.events] == ["checkout:start", "checkout:item_${kind}"]
    assert [ev.deprecated for ev in se.events] == [False, True]
    assert se.events[1].field_values["action"] == "item_${kind}"
    # Per-event overrides ride on the event; global lists stay on the variable.
    assert se.events[0].overrides == {}
    assert se.events[1].overrides == {"platform": ["android"]}
    fields = {fd.name: fd for fd in se.fields}
    assert fields["category"].values == ["checkout"]
    assert fields["category"].required is True
    assert fields["action"].values == ["start", "item_hat", "item_car"]
    # Variable union narrowed to the enum options; the variable is named.
    assert fields["platform"].values == ["ios", "android"]
    assert fields["platform"].variable == "platform"
    # Number fields and fields some event leaves unset are free.
    assert fields["amount"].values is None
    assert fields["note"].values is None
    assert [(v.name, v.allowed_values, v.tokens) for v in variables] == [
        ("Kind", ["hat", "car"], ["kind"])
    ]


def test_duplicate_identities_get_distinct_keys() -> None:
    twin = PlanEvent(
        id=uuid.uuid4(), event_type_id=SE, name="Twin", identity="checkout:start", status="live"
    )
    base = _plan()
    plan = ExportPlan(
        snapshot=PlanSnapshot(
            types_by_name=base.snapshot.types_by_name,
            events=[START, twin],
            variable_allowed=base.snapshot.variable_allowed,
        ),
        contexts=base.contexts,
    )
    assert list(build_json_schemas(plan)) == ["se/checkout:start", "se/checkout:start#2"]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/plan/export"


async def _demo(client: AsyncClient) -> str:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 201, resp.text
    slug = resp.json()["slug"]
    assert isinstance(slug, str)
    return slug


@pytest.mark.asyncio
async def test_demo_export_has_a_schema_per_event_and_round_trips(client: AsyncClient) -> None:
    """The issue's Done-when: every demo event's schema round-trips."""
    slug = await _demo(client)
    resp = await client.get(_url(slug), params={"format": "jsonschema"})
    assert resp.status_code == 200, resp.text
    bundle = resp.json()
    assert bundle["format"] == "jsonschema"
    assert bundle["branch"]
    assert bundle["plan_hash"].startswith("sha256:")
    schemas: dict[str, dict[str, Any]] = bundle["schemas"]

    async with TestSessionLocal() as session:
        project_id = (
            await session.execute(select(Project.id).where(Project.slug == slug))
        ).scalar_one()
        main_id = (
            await session.execute(
                select(PlanBranch.id).where(
                    PlanBranch.project_id == project_id,
                    PlanBranch.kind == BranchKind.main.value,
                )
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(EventType.name, Event.name, Event.source_name, Event.status)
                .join(EventType, EventType.id == Event.event_type_id)
                .where(Event.project_id == project_id, Event.branch_id == main_id)
            )
        ).all()
    expected = sorted(
        f"{type_name}/{source_name or name}"
        for type_name, name, source_name, status in rows
        if status != "archived"
    )
    assert expected
    assert sorted(key.split("#")[0] for key in schemas) == expected
    assert "click/Legacy CTA Click" not in schemas
    assert any(status == "archived" for *_, status in rows)

    for key, schema in schemas.items():
        assert schema["$schema"] == JSON_SCHEMA_DIALECT, key
        assert schema["type"] == "object", key
        sample = _sample(schema)
        assert sample is not None, key
        assert _validate(schema, sample) == [], (key, sample)
        for required in schema["required"]:
            assert _validate(schema, {k: v for k, v in sample.items() if k != required}), key

    home = schemas["screen_view/Home Screen View"]
    assert home["required"] == ["screen_name"]
    assert home["properties"]["screen_name"]["const"] == "home"
    # ``${platform}`` -> the variable's allowed values.
    assert home["properties"]["platform"]["enum"] == ["ios", "android", "web"]
    assert _validate(home, {"screen_name": "home", "platform": "tv"})
    purchase = schemas["purchase/Purchase Completed"]["properties"]
    assert purchase["product_id"]["enum"] == ["prod_monthly", "prod_annual", "prod_lifetime"]
    assert (purchase["amount"]["type"], purchase["amount"]["const"]) == ("number", 9.99)
    assert schemas["purchase/Promo Applied"]["deprecated"] is True


@pytest.mark.asyncio
async def test_schemas_use_only_known_keywords(client: AsyncClient) -> None:
    slug = await _demo(client)
    schemas = (await client.get(_url(slug))).json()["schemas"]
    for key, schema in schemas.items():
        assert _walk_keywords(schema) - _KNOWN_KEYWORDS == set(), key


@pytest.mark.asyncio
async def test_demo_codegen_model(client: AsyncClient) -> None:
    slug = await _demo(client)
    resp = await client.get(_url(slug), params={"format": "codegen_model"})
    assert resp.status_code == 200, resp.text
    model = resp.json()
    assert model["format"] == "codegen_model"
    types = {et["name"]: et for et in model["event_types"]}
    assert set(types) >= {"screen_view", "click", "purchase"}
    screen = types["screen_view"]
    assert screen["display_name"] == "Screen View"
    platform = next(fd for fd in screen["fields"] if fd["name"] == "platform")
    assert platform["values"] == ["ios", "android", "web"]
    assert platform["variable"] == "platform"
    home = next(ev for ev in screen["events"] if ev["name"] == "Home Screen View")
    assert home["field_values"] == {"screen_name": "home", "platform": "${platform}"}
    assert home["deprecated"] is False
    assert home["overrides"] == {}
    # The demo's authored per-event override rides on its event; the variable
    # keeps its global list.
    trial = next(
        ev for et in model["event_types"] for ev in et["events"] if ev["name"] == "Trial Started"
    )
    assert trial["overrides"]["product_id"] == ["prod_monthly", "prod_annual"]
    click_names = {ev["name"] for ev in types["click"]["events"]}
    assert "Legacy CTA Click" not in click_names
    promo = next(ev for ev in types["purchase"]["events"] if ev["name"] == "Promo Applied")
    assert promo["deprecated"] is True and promo["status"] == "deprecated"
    variables = {v["name"]: v for v in model["variables"]}
    assert variables["product_id"]["allowed_values"] == [
        "prod_monthly",
        "prod_annual",
        "prod_lifetime",
    ]


@pytest.mark.asyncio
async def test_export_is_deterministic_and_rejects_unknown_formats(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "px-determinism")
    first = await client.get(_url(s.slug), params={"format": "codegen_model"})
    second = await client.get(_url(s.slug), params={"format": "codegen_model"})
    assert first.content == second.content
    bad = await client.get(_url(s.slug), params={"format": "yaml"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_name_rule_contracts_and_templates(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "px-contracts")
    async with TestSessionLocal() as session, session.begin():
        type_ids = (
            select(EventType.id)
            .join(Project, Project.id == EventType.project_id)
            .where(Project.slug == s.slug)
        )
        await session.execute(
            sql_update(FieldDefinition)
            .where(FieldDefinition.name == "screen", FieldDefinition.event_type_id.in_(type_ids))
            .values(contract_regex="^[a-z]+$")
        )
    bundle = (await client.get(_url(s.slug))).json()
    item = bundle["schemas"]["se/shop:item_${item_kind}:view"]
    props = item["properties"]
    assert props["action"]["pattern"] == "^item_(?:hat)$"
    assert props["label"]["const"] == "view"
    assert props["value"] == {"title": "V", "type": "number", "minimum": 0, "maximum": 10}
    assert props["screen"]["pattern"] == "^[a-z]+$"
    assert item["required"] == ["screen"]
    assert _validate(item, {"screen": "home", "action": "item_hat", "value": 11})

    model = (await client.get(_url(s.slug), params={"format": "codegen_model"})).json()
    (se,) = model["event_types"]
    assert se["name_rule"] == "{category}:{action}:{label}"
    fields = {fd["name"]: fd for fd in se["fields"]}
    assert fields["category"]["values"] == ["shop"]
    assert sorted(fields["action"]["values"]) == ["item_hat", "open"]
    assert sorted(fields["label"]["values"]) == ["swipe", "tap", "view"]
    assert fields["value"]["values"] is None
    assert fields["screen"]["values"] is None


@pytest.mark.asyncio
async def test_archived_excluded_and_branch_export(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "px-branch")
    created = await client.post(f"/api/v1/projects/{s.slug}/branches", json={"name": "cleanup"})
    assert created.status_code == 201, created.text
    branch_id = created.json()["id"]
    async with TestSessionLocal() as session, session.begin():
        await session.execute(
            sql_update(Event)
            .where(Event.branch_id == uuid.UUID(branch_id), Event.source_name == "shop:open:tap")
            .values(status="archived")
        )

    main = (await client.get(_url(s.slug))).json()
    assert "se/shop:open:tap" in main["schemas"]
    assert main["schemas"]["se/shop:open:swipe"]["deprecated"] is True
    assert main["revision"] is not None  # opening the branch captured a base revision

    on_branch = await client.get(_url(s.slug), params={"branch": branch_id})
    assert on_branch.status_code == 200, on_branch.text
    bundle = on_branch.json()
    assert bundle["branch"] == "cleanup"
    assert bundle["branch_id"] == branch_id
    assert "se/shop:open:tap" not in bundle["schemas"]
    assert "se/shop:open:swipe" in bundle["schemas"]
    assert bundle["plan_hash"] != main["plan_hash"]
    async with TestSessionLocal() as session:
        base_revision = await session.scalar(
            select(PlanBranch.base_revision_id).where(PlanBranch.id == uuid.UUID(branch_id))
        )
    assert bundle["revision"] == (str(base_revision) if base_revision else None)

    unknown = await client.get(_url(s.slug), params={"branch": str(uuid.uuid4())})
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_non_member_gets_404_and_viewer_can_export(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "px-members")
    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "px-outsider@example.com", "password": PASSWORD, "name": "Outsider"},
    )
    assert registered.status_code == 201, registered.text

    denied = await client.get(_url(s.slug))
    assert denied.status_code == 404
    assert denied.json()["detail"] == "Project not found"

    await add_member_by_slug(s.slug, "px-outsider@example.com", "viewer")
    allowed = await client.get(_url(s.slug), params={"format": "codegen_model"})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["event_types"][0]["name"] == "se"
