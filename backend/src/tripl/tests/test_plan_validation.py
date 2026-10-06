"""Plan validation (GH #261, F08): the rules, the service and the route.

The first half exercises ``core.plan_validation`` on plain data, one rule per
test: identity built from the naming rule, name-only resolution, holes (a
value unknown at scan time) never being an error, deprecated and archived
events, unknown fields, required fields in payload mode only, and the value
contracts (enum, regex, min/max, variable allowed values). The second half
drives ``POST /projects/{slug}/plan/validate``: a branch, the demo project's
missing required field (the issue's "Done when"), the membership 404, a viewer
member, and the 5000-item cap.

Every name here is synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import update as sql_update

from tripl.core.plan_validation import (
    MAX_HOLES_PER_QUERY,
    EventContext,
    Finding,
    IdentityIndex,
    PlanEvent,
    PlanEventType,
    PlanField,
    PlanSnapshot,
    ValidationItem,
    build_identity,
    check_field_contract,
    check_item,
    has_literal_content,
    item_status,
    resolve_item,
    scalar_text,
)
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.schemas.plan_validation import MAX_VALIDATION_ITEMS
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"

# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

SE = uuid.uuid4()
PAGE = uuid.uuid4()
LEGACY = uuid.uuid4()


def _event(
    type_id: uuid.UUID, identity: str, status: str = "live", name: str | None = None
) -> PlanEvent:
    return PlanEvent(
        id=uuid.uuid4(),
        event_type_id=type_id,
        name=name or identity,
        identity=identity,
        status=status,
    )


TAP = _event(SE, "shop:open:tap")
SWIPE = _event(SE, "shop:open:swipe", status="deprecated")
OLD = _event(SE, "shop:close:tap", status="archived")
ITEM_VIEW = _event(SE, "shop:item_${item_kind}:view")
HOME = _event(PAGE, "home")
PROMO = _event(LEGACY, "promo_${promo_id}_shown")
LOGIN = _event(LEGACY, "login_done")
# A fully non-ASCII identity: the plan names events in any script.
SCREEN_RU = _event(LEGACY, "экран_главная")


def _plan(**variables: tuple[str, ...]) -> PlanSnapshot:
    return PlanSnapshot(
        types_by_name={
            "se": PlanEventType(
                id=SE,
                name="se",
                name_format="{category}:{action}:{label}",
                fields={
                    "category": PlanField("category", "string", is_required=True),
                    "action": PlanField("action", "string", is_required=True),
                    "label": PlanField("label", "string"),
                    "value": PlanField("value", "number", min_value=0, max_value=100),
                    "platform": PlanField("platform", "enum", enum_options=("ios", "android")),
                    "sku": PlanField("sku", "string", regex=r"^SKU-\d+$"),
                },
            ),
            "page": PlanEventType(
                id=PAGE,
                name="page",
                name_format="{screen}",
                fields={"screen": PlanField("screen", "string", is_required=True)},
            ),
            "legacy": PlanEventType(id=LEGACY, name="legacy", name_format=None, fields={}),
        },
        events=[TAP, SWIPE, OLD, ITEM_VIEW, HOME, PROMO, LOGIN, SCREEN_RU],
        variable_allowed=variables,
    )


def _verdict(
    item: ValidationItem,
    plan: PlanSnapshot | None = None,
    *,
    contexts: Mapping[uuid.UUID, EventContext] | None = None,
    strict: bool = False,
) -> tuple[list[Finding], uuid.UUID | None, str | None]:
    plan = plan or _plan()
    res = resolve_item(item, plan)
    ctx = (contexts or {}).get(res.event.id) if res.event is not None else None
    event_id = res.event.id if res.event is not None else None
    return check_item(res, plan, ctx, strict=strict), event_id, res.identity


def _codes(findings: list[Finding]) -> list[tuple[str, str, str | None]]:
    return [(f.code, f.severity, f.field) for f in findings]


def test_identity_is_built_from_the_types_naming_rule() -> None:
    item = ValidationItem(
        ref="a", event_type="se", fields={"category": "shop", "action": "open", "label": "tap"}
    )
    findings, event_id, identity = _verdict(item)
    assert identity == "shop:open:tap"
    assert event_id == TAP.id
    assert findings == []


def test_build_identity_turns_unknown_values_into_holes() -> None:
    fmt = "{category}:{action}:{label}"
    assert build_identity(fmt, {"category": "shop", "action": None}) == ("shop:${action}:${label}")
    # Dotted keys are walked out of a literal JSON value, the way the scan does.
    assert build_identity("{payload.screen}", {"payload": '{"screen": "home"}'}) == "home"
    assert not has_literal_content("${category}:${action}")
    assert has_literal_content("shop:${action}")


def test_unknown_event_type_is_an_error_and_stops() -> None:
    findings, event_id, _ = _verdict(ValidationItem(ref="a", event_type="nope", name="x"))
    assert _codes(findings) == [("unknown_event_type", "error", None)]
    assert event_id is None
    assert item_status(findings) == "error"


def test_unknown_literal_event_is_an_error() -> None:
    item = ValidationItem(
        ref="a", event_type="se", fields={"category": "shop", "action": "open", "label": "typo"}
    )
    findings, event_id, _ = _verdict(item)
    assert _codes(findings) == [("unknown_event", "error", None)]
    assert event_id is None


def test_name_only_resolution_matches_across_types() -> None:
    findings, event_id, _ = _verdict(ValidationItem(ref="a", name="login_done"))
    assert (findings, event_id) == ([], LOGIN.id)
    findings, event_id, _ = _verdict(ValidationItem(ref="b", name="home"))
    assert (findings, event_id) == ([], HOME.id)
    findings, _, _ = _verdict(ValidationItem(ref="c", name="logn_done"))
    assert _codes(findings) == [("unknown_event", "error", None)]


def test_name_with_nothing_to_identify_is_an_error() -> None:
    findings, _, _ = _verdict(ValidationItem(ref="a", fields={"x": "1"}))
    assert _codes(findings) == [("unknown_event", "error", None)]


def test_dynamic_values_never_error() -> None:
    # Label unknown at scan time: matches the planned events of shop/open.
    item = ValidationItem(
        ref="a", event_type="se", fields={"category": "shop", "action": "open", "label": None}
    )
    findings, event_id, identity = _verdict(item)
    assert identity == "shop:open:${label}"
    assert event_id is None  # two planned events fit; neither is wrong
    assert [f for f in findings if f.severity == "error"] == []
    # Dynamic values that match nothing: a warning, not an error.
    item = ValidationItem(
        ref="b", event_type="se", fields={"category": "nowhere", "action": None, "label": None}
    )
    findings, _, _ = _verdict(item)
    assert _codes(findings) == [("unknown_event", "warning", None)]
    # All holes identify nothing and are left alone.
    item = ValidationItem(ref="c", event_type="se", fields={"category": None})
    assert _verdict(item)[0] == []


def test_dynamic_value_is_reported_only_when_strict() -> None:
    item = ValidationItem(
        ref="a",
        event_type="se",
        fields={"category": "shop", "action": "open", "label": "tap", "value": None},
    )
    assert _verdict(item)[0] == []
    strict = _verdict(item, strict=True)[0]
    assert _codes(strict) == [("dynamic_value", "info", "value")]
    assert item_status(strict) == "ok"


def test_interpolated_name_matches_a_templated_plan_event() -> None:
    findings, event_id, _ = _verdict(
        ValidationItem(ref="a", event_type="legacy", name="promo_${id}_shown")
    )
    assert (findings, event_id) == ([], PROMO.id)
    # A literal name is captured by the plan's variable hole...
    findings, event_id, _ = _verdict(ValidationItem(ref="b", name="promo_spring_shown"))
    assert (findings, event_id) == ([], PROMO.id)


def test_variable_allowed_values_apply_to_identity_captures() -> None:
    plan = _plan(promo_id=("spring", "summer"))
    findings, _, _ = _verdict(ValidationItem(ref="a", name="promo_winter_shown"), plan)
    assert _codes(findings) == [("value_not_allowed", "error", None)]
    assert "promo_id" in findings[0].message
    assert _verdict(ValidationItem(ref="b", name="promo_spring_shown"), plan)[0] == []


def test_event_override_replaces_the_global_allowed_values() -> None:
    plan = _plan(promo_id=("spring",))
    contexts = {PROMO.id: EventContext(overrides={"promo_id": ("winter",)})}
    item = ValidationItem(ref="a", name="promo_winter_shown")
    assert _verdict(item, plan, contexts=contexts)[0] == []
    item = ValidationItem(ref="b", name="promo_spring_shown")
    assert _codes(_verdict(item, plan, contexts=contexts)[0]) == [
        ("value_not_allowed", "error", None)
    ]


def test_stored_field_template_checks_variable_values_per_field() -> None:
    plan = _plan(item_kind=("hat", "shoe"))
    contexts = {ITEM_VIEW.id: EventContext(field_values={"action": "item_${item_kind}"})}
    ok = ValidationItem(
        ref="a", event_type="se", fields={"category": "shop", "action": "item_hat", "label": "view"}
    )
    assert _verdict(ok, plan, contexts=contexts)[0] == []
    bad = ValidationItem(
        ref="b", event_type="se", fields={"category": "shop", "action": "item_car", "label": "view"}
    )
    findings, event_id, _ = _verdict(bad, plan, contexts=contexts)
    assert event_id == ITEM_VIEW.id
    # Reported once, against the field, not again for the identity capture.
    assert _codes(findings) == [("value_not_allowed", "error", "action")]


def test_deprecated_is_a_warning_and_archived_an_error() -> None:
    swipe = ValidationItem(
        ref="a", event_type="se", fields={"category": "shop", "action": "open", "label": "swipe"}
    )
    findings, _, _ = _verdict(swipe)
    assert _codes(findings) == [("deprecated_event", "warning", None)]
    assert item_status(findings) == "warning"
    old = ValidationItem(
        ref="b", event_type="se", fields={"category": "shop", "action": "close", "label": "tap"}
    )
    findings, _, _ = _verdict(old)
    assert _codes(findings) == [("deprecated_event", "error", None)]


def test_unknown_field_is_a_warning_and_properties_count_as_fields() -> None:
    item = ValidationItem(
        ref="a",
        event_type="se",
        fields={"category": "shop", "action": "open", "label": "tap"},
        properties={"colour": "red", "platform": "ios"},
    )
    findings, _, _ = _verdict(item)
    assert _codes(findings) == [("unknown_field", "warning", "colour")]


def test_missing_required_field_only_in_payload_mode() -> None:
    fields = {"category": "shop", "action": "open"}
    static = ValidationItem(ref="a", event_type="se", name="shop:open:tap", fields=fields)
    assert [f for f in _verdict(static)[0] if f.code == "missing_required_field"] == []
    payload = ValidationItem(
        ref="b", event_type="page", fields={}, properties={"other": 1}, complete=True
    )
    findings, _, _ = _verdict(payload)
    assert ("missing_required_field", "error", "screen") in _codes(findings)
    # A present-but-null value is dynamic, not missing.
    payload = ValidationItem(ref="c", event_type="page", fields={"screen": None}, complete=True)
    assert [f for f in _verdict(payload)[0] if f.code == "missing_required_field"] == []


def test_enum_contract() -> None:
    fd = PlanField("platform", "enum", enum_options=("ios", "android"))
    assert check_field_contract(fd, "ios") == []
    assert _codes(check_field_contract(fd, "web")) == [("value_not_allowed", "error", "platform")]


def test_regex_contract_is_a_partial_match_and_bad_patterns_are_skipped() -> None:
    fd = PlanField("sku", "string", regex=r"\d{3}")
    assert check_field_contract(fd, "abc123def") == []
    assert _codes(check_field_contract(fd, "abc")) == [("value_not_allowed", "error", "sku")]
    broken = PlanField("sku", "string", regex="(unclosed")
    assert check_field_contract(broken, "anything") == []


def test_min_max_contract() -> None:
    fd = PlanField("value", "number", min_value=0, max_value=100)
    assert check_field_contract(fd, "50") == []
    assert "below the minimum 0" in check_field_contract(fd, "-1")[0].message
    assert "above the maximum 100" in check_field_contract(fd, "101")[0].message
    assert "not a number" in check_field_contract(fd, "lots")[0].message
    assert check_field_contract(PlanField("x", "number", max_value=float("inf")), "5") == []


def test_payload_scalars_render_like_the_scan() -> None:
    assert scalar_text(True) == "true"
    assert scalar_text(3.0) == "3"
    assert scalar_text(2.5) == "2.5"
    assert scalar_text(None) is None
    assert scalar_text({"a": 1}) is None
    item = ValidationItem(
        ref="a",
        event_type="se",
        fields={"category": "shop", "action": "open", "label": "tap"},
        properties={"value": 250, "platform": "web", "sku": "nope"},
    )
    findings, _, _ = _verdict(item)
    assert sorted(f.field for f in findings if f.field) == ["platform", "sku", "value"]


def test_identity_index_matches_holes_on_both_sides() -> None:
    index = IdentityIndex([TAP, SWIPE, ITEM_VIEW])
    assert {m.event.id for m in index.match("shop:${a}:tap")} == {TAP.id}
    # A query hole against a plan hole.
    assert [m.event.id for m in index.match("shop:item_${k}:view")] == [ITEM_VIEW.id]
    captured = index.match("shop:item_hat:view")
    assert captured[0].captures == (("item_kind", "hat"),)
    assert index.match("shop:${a}:${b}") != ()
    assert index.match("nothing") == ()


def test_non_ascii_literal_identity_is_looked_up() -> None:
    assert has_literal_content("экран_${screen}")
    assert has_literal_content("экран")
    findings, event_id, _ = _verdict(ValidationItem(ref="a", name="экран_главная"))
    assert (findings, event_id) == ([], SCREEN_RU.id)
    findings, event_id, _ = _verdict(ValidationItem(ref="b", name="экран_настройки"))
    assert _codes(findings) == [("unknown_event", "error", None)]
    assert event_id is None
    # Fully literal but with no letter or digit at all: still looked up, and
    # an unplanned one is an error, not silently skipped.
    findings, _, identity = _verdict(ValidationItem(ref="c", name="—·—"))
    assert identity == "—·—"
    assert _codes(findings) == [("unknown_event", "error", None)]


def test_complete_payload_renders_absent_rule_keys_like_the_scan() -> None:
    fmt = "{category}:{action}:{label}"
    assert build_identity(fmt, {"category": "shop"}, missing="") == "shop::"
    # Present but unreadable stays a hole even in payload mode.
    assert build_identity(fmt, {"category": "shop", "action": None}, missing="") == (
        "shop:${action}:"
    )
    item = ValidationItem(
        ref="a",
        event_type="se",
        properties={"category": "shop", "action": "open"},
        complete=True,
    )
    findings, event_id, identity = _verdict(item)
    assert identity == "shop:open:"
    assert event_id is None
    # Literal, so an unplanned identity is an error, not a "might fit" warning.
    assert _codes(findings) == [("unknown_event", "error", None)]
    # The same call from a static scan keeps the hole and matches both planned
    # events of shop/open.
    static = ValidationItem(
        ref="b", event_type="se", properties={"category": "shop", "action": "open"}
    )
    assert _verdict(static)[2] == "shop:open:${label}"


def test_too_many_holes_is_an_info_note_and_skipped() -> None:
    too_many = "shop" + "_${p}" * (MAX_HOLES_PER_QUERY + 1)
    findings, event_id, _ = _verdict(ValidationItem(ref="a", name=too_many))
    assert _codes(findings) == [("too_dynamic", "info", None)]
    assert event_id is None
    assert item_status(findings) == "ok"
    at_cap = "shop" + "_${p}" * MAX_HOLES_PER_QUERY
    findings, _, _ = _verdict(ValidationItem(ref="b", name=at_cap))
    assert [f.code for f in findings] == ["unknown_event"]


def test_query_with_no_literal_prefix_is_filtered_by_its_longest_literal() -> None:
    index = IdentityIndex([TAP, SWIPE, ITEM_VIEW, LOGIN])
    assert {m.event.id for m in index.match("${a}:open:tap")} == {TAP.id}
    assert {m.event.id for m in index.match("${a}_done")} == {LOGIN.id}
    assert index.match("${a}:nothing") == ()


def test_built_identity_with_nothing_literal_falls_back_to_the_name() -> None:
    item = ValidationItem(ref="a", event_type="se", name="shop:open:tap", fields={"category": None})
    findings, event_id, identity = _verdict(item)
    assert identity == "shop:open:tap"
    assert (findings, event_id) == ([], TAP.id)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


async def _post(client: AsyncClient, url: str, body: dict[str, object]) -> dict[str, object]:
    resp = await client.post(url, json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


async def _seed_scan_name_rule(slug: str, event_type_id: str, name_format: str) -> None:
    from tripl.models.data_source import DataSource
    from tripl.models.scan_config import ScanConfig

    async with TestSessionLocal() as session, session.begin():
        project = (await session.execute(select(Project).where(Project.slug == slug))).scalar_one()
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"wh-{slug}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
            password_encrypted="x",
        )
        session.add(data_source)
        await session.flush()
        session.add(
            ScanConfig(
                id=uuid.uuid4(),
                project_id=project.id,
                data_source_id=data_source.id,
                event_type_id=uuid.UUID(event_type_id),
                name="scan",
                base_query="SELECT * FROM events",
                event_name_format=name_format,
            )
        )


class Seeded:
    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.url = f"/api/v1/projects/{slug}/plan/validate"
        self.events: dict[str, uuid.UUID] = {}


async def seed_validation_plan(client: AsyncClient, slug: str) -> Seeded:
    """A type ``se`` named ``{category}:{action}:{label}`` with a few events."""
    s = Seeded(slug)
    await _post(client, "/api/v1/projects", {"name": slug, "slug": slug, "description": ""})
    base = f"/api/v1/projects/{slug}"
    se = await _post(
        client,
        f"{base}/event-types",
        {
            "name": "se",
            "display_name": "Structured",
            "field_definitions": [
                {"name": "category", "display_name": "C", "field_type": "string"},
                {"name": "action", "display_name": "A", "field_type": "string"},
                {"name": "label", "display_name": "L", "field_type": "string"},
                {
                    "name": "value",
                    "display_name": "V",
                    "field_type": "number",
                    "contract_min_value": 0,
                    "contract_max_value": 10,
                },
                {
                    "name": "screen",
                    "display_name": "S",
                    "field_type": "string",
                    "is_required": True,
                },
            ],
        },
    )
    await _post(client, f"{base}/variables", {"name": "item_kind", "allowed_values": ["hat"]})
    await _seed_scan_name_rule(slug, str(se["id"]), "{category}:{action}:{label}")
    definitions = se["field_definitions"]
    assert isinstance(definitions, list)
    field_ids = {fd["name"]: uuid.UUID(fd["id"]) for fd in definitions}

    async with TestSessionLocal() as session, session.begin():
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
        for identity, status in (
            ("shop:open:tap", "live"),
            ("shop:open:swipe", "deprecated"),
            ("shop:item_${item_kind}:view", "live"),
        ):
            event = Event(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=main_id,
                event_type_id=uuid.UUID(str(se["id"])),
                name=identity,
                source_name=identity,
                status=status,
            )
            session.add(event)
            await session.flush()
            category, action, label = identity.split(":")
            for key, value in (("category", category), ("action", action), ("label", label)):
                session.add(
                    EventFieldValue(
                        event_id=event.id, field_definition_id=field_ids[key], value=value
                    )
                )
            s.events[identity] = event.id
    return s


def _se(
    category: str | None, action: str | None, label: str | None, **extra: object
) -> dict[str, object]:
    return {
        "event_type": "se",
        "fields": {"category": category, "action": action, "label": label},
        **extra,
    }


@pytest.mark.asyncio
async def test_validate_route_verdicts_and_summary(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "pv-route")
    resp = await client.post(
        s.url,
        json={
            "items": [
                {**_se("shop", "open", "tap"), "ref": "App.swift:10"},
                _se("shop", "open", "typo"),
                _se("shop", "open", "swipe"),
                _se("shop", "item_car", "view"),
                _se("shop", None, "tap"),
                {"event_type": "nope", "name": "x"},
                {"name": "shop:open:tap", "properties": {"value": 50, "colour": "red"}},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["items"]
    assert items[0] == {
        "ref": "App.swift:10",
        "status": "ok",
        "event_id": str(s.events["shop:open:tap"]),
        "identity": "shop:open:tap",
        "findings": [],
    }
    assert items[1]["ref"] == "1"
    assert [f["code"] for f in items[1]["findings"]] == ["unknown_event"]
    assert items[2]["status"] == "warning"
    assert items[2]["findings"][0]["code"] == "deprecated_event"
    assert items[3]["event_id"] == str(s.events["shop:item_${item_kind}:view"])
    assert items[3]["findings"] == [
        {
            "code": "value_not_allowed",
            "severity": "error",
            "field": "action",
            "message": "'car' is not an allowed value of variable ${item_kind} (hat)",
            "rule": None,
        }
    ]
    assert items[4]["status"] == "ok"
    assert items[4]["identity"] == "shop:${action}:tap"
    assert items[5]["findings"][0]["code"] == "unknown_event_type"
    assert {(f["code"], f["field"]) for f in items[6]["findings"]} == {
        ("value_not_allowed", "value"),
        ("unknown_field", "colour"),
    }
    assert body["summary"] == {"ok": 2, "warnings": 1, "errors": 4}


@pytest.mark.asyncio
async def test_validate_route_strict_and_payload_mode(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "pv-strict")
    static = await client.post(
        s.url, json={"items": [_se("shop", "open", "tap", properties={"value": None})]}
    )
    assert static.json()["items"][0]["findings"] == []
    strict = await client.post(
        s.url,
        json={"strict": True, "items": [_se("shop", "open", "tap", properties={"value": None})]},
    )
    assert [f["code"] for f in strict.json()["items"][0]["findings"]] == ["dynamic_value"]
    assert strict.json()["items"][0]["status"] == "ok"
    payload = await client.post(s.url, json={"items": [_se("shop", "open", "tap", complete=True)]})
    finding = payload.json()["items"][0]["findings"][0]
    assert (finding["code"], finding["field"]) == ("missing_required_field", "screen")


@pytest.mark.asyncio
async def test_validate_route_reads_the_requested_branch(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "pv-branch")
    created = await client.post(f"/api/v1/projects/{s.slug}/branches", json={"name": "cleanup"})
    assert created.status_code == 201, created.text
    branch_id = created.json()["id"]
    async with TestSessionLocal() as session, session.begin():
        await session.execute(
            sql_update(Event)
            .where(
                Event.branch_id == uuid.UUID(branch_id),
                Event.source_name == "shop:open:tap",
            )
            .values(status="archived")
        )

    body = {"items": [_se("shop", "open", "tap")]}
    on_main = await client.post(s.url, json=body)
    assert on_main.json()["items"][0]["status"] == "ok"
    on_branch = await client.post(s.url, params={"branch": branch_id}, json=body)
    assert on_branch.status_code == 200, on_branch.text
    item = on_branch.json()["items"][0]
    # The branch copy of the type still names events by main's rule.
    assert item["identity"] == "shop:open:tap"
    assert item["event_id"] != str(s.events["shop:open:tap"])
    assert [(f["code"], f["severity"]) for f in item["findings"]] == [("deprecated_event", "error")]
    unknown = await client.post(s.url, params={"branch": str(uuid.uuid4())}, json=body)
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_validate_route_event_overrides_are_loaded(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "pv-override")
    event_id = s.events["shop:item_${item_kind}:view"]
    async with TestSessionLocal() as session, session.begin():
        variable = (
            await session.execute(select(Variable).where(Variable.name == "item_kind"))
        ).scalar_one()
        session.add(
            VariableEventValueOverride(
                project_id=variable.project_id,
                branch_id=variable.branch_id,
                variable_id=variable.id,
                event_id=event_id,
                values=["car"],
            )
        )
    resp = await client.post(s.url, json={"items": [_se("shop", "item_car", "view")]})
    assert resp.json()["items"][0]["findings"] == []


@pytest.mark.asyncio
async def test_demo_payload_missing_a_required_field_is_an_error(client: AsyncClient) -> None:
    """The issue's Done-when: a demo payload without a required field fails."""
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202, resp.text
    slug = resp.json()["slug"]
    checked = await client.post(
        f"/api/v1/projects/{slug}/plan/validate",
        json={
            "items": [
                {
                    "event_type": "screen_view",
                    "name": "Home Screen View",
                    "properties": {"platform": "ios"},
                    "complete": True,
                },
                {
                    "event_type": "screen_view",
                    "name": "Home Screen View",
                    "properties": {"platform": "ios", "screen_name": "home"},
                    "complete": True,
                },
            ]
        },
    )
    assert checked.status_code == 200, checked.text
    missing, fine = checked.json()["items"]
    assert missing["status"] == "error"
    assert [(f["code"], f["field"]) for f in missing["findings"]] == [
        ("missing_required_field", "screen_name")
    ]
    assert missing["event_id"] is not None
    assert fine["status"] == "ok", fine
    assert checked.json()["summary"] == {"ok": 1, "warnings": 0, "errors": 1}


@pytest.mark.asyncio
async def test_non_member_gets_404_and_viewer_can_validate(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "pv-members")
    body = {"items": [_se("shop", "open", "tap")]}
    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "pv-outsider@example.com", "password": PASSWORD, "name": "Outsider"},
    )
    assert registered.status_code == 201, registered.text

    denied = await client.post(s.url, json=body)
    assert denied.status_code == 404
    assert denied.json()["detail"] == "Project not found"

    await add_member_by_slug(s.slug, "pv-outsider@example.com", "viewer")
    allowed = await client.post(s.url, json=body)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["items"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_validate_route_caps_the_batch(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "pv-cap")
    item = _se("shop", "open", "tap")
    at_cap = await client.post(s.url, json={"items": [item] * MAX_VALIDATION_ITEMS})
    assert at_cap.status_code == 200, at_cap.text
    assert at_cap.json()["summary"]["ok"] == MAX_VALIDATION_ITEMS
    over = await client.post(s.url, json={"items": [item] * (MAX_VALIDATION_ITEMS + 1)})
    assert over.status_code == 422


@pytest.mark.asyncio
async def test_validate_route_resolves_variables_by_source_name_and_binding(
    client: AsyncClient,
) -> None:
    """A plan token naming a variable by source name or binding gets its values."""
    s = await seed_validation_plan(client, "pv-tokens")
    async with TestSessionLocal() as session, session.begin():
        anchor = (
            await session.execute(select(Event).where(Event.id == s.events["shop:open:tap"]))
        ).scalar_one()
        variable = Variable(
            id=uuid.uuid4(),
            project_id=anchor.project_id,
            branch_id=anchor.branch_id,
            name="gift_kind",
            source_name="property.gift_kind",
            bindings=["payload.gift"],
            allowed_values=["hat"],
        )
        session.add(variable)
        ids: dict[str, uuid.UUID] = {}
        for identity in ("shop:box_${property.gift_kind}:view", "shop:gift_${payload.gift}:view"):
            event = Event(
                id=uuid.uuid4(),
                project_id=anchor.project_id,
                branch_id=anchor.branch_id,
                event_type_id=anchor.event_type_id,
                name=identity,
                source_name=identity,
                status="live",
            )
            session.add(event)
            ids[identity] = event.id
        await session.flush()
        session.add(
            VariableEventValueOverride(
                project_id=anchor.project_id,
                branch_id=anchor.branch_id,
                variable_id=variable.id,
                event_id=ids["shop:gift_${payload.gift}:view"],
                values=["car"],
            )
        )
    resp = await client.post(
        s.url,
        json={
            "items": [
                _se("shop", "box_hat", "view"),
                _se("shop", "box_car", "view"),
                # The per-event override is found through the binding token.
                _se("shop", "gift_car", "view"),
                _se("shop", "gift_hat", "view"),
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["status"] for item in items] == ["ok", "error", "ok", "error"]
    assert items[1]["event_id"] == str(ids["shop:box_${property.gift_kind}:view"])
    assert "${property.gift_kind}" in items[1]["findings"][0]["message"]
    assert items[3]["event_id"] == str(ids["shop:gift_${payload.gift}:view"])


@pytest.mark.asyncio
async def test_validate_route_accepts_scalar_field_values(client: AsyncClient) -> None:
    s = await seed_validation_plan(client, "pv-scalars")
    base = {"category": "shop", "action": "open", "label": "tap"}
    resp = await client.post(
        s.url,
        json={
            "items": [
                {"event_type": "se", "fields": {**base, "value": value}}
                for value in (5, 5.0, 50, True)
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["status"] for item in items] == ["ok", "ok", "error", "error"]
    assert items[2]["findings"][0]["message"] == "50 is above the maximum 10 of 'value'"
    # A boolean renders as the scan renders it, and is not a number.
    assert "'true' is not a number" in items[3]["findings"][0]["message"]
