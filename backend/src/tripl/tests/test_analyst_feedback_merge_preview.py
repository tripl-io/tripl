""" "As merged": one event as main will hold it after the merge (tripl-4zzc.3).

Three layers. The pure projection over hand-built snapshots pins each apply
rule of ``_apply_merge`` it re-states. The gate extraction pins that
``merge_blocked_by`` and the merge read one gate. The route and parity tests
run real branches: a clean branch is previewed, merged, and main afterwards is
compared with the preview field by field; a conflicting one is previewed as
blocked and the merge refuses with exactly those names.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.plan_branch import PlanBranch
from tripl.models.project import Project
from tripl.services._plan_merge_preview import (
    AmbiguousEvent,
    EventTarget,
    project_merged_event,
)
from tripl.services.plan_branch_conflicts import (
    _EV_CHANGE_KEYS,
    _conflict_set,
    conflicting_fields,
    merge_blocked_by,
    merge_blocking_conflicts,
)
from tripl.services.plan_revision_service import build_plan_snapshot
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_branch_context_main_and_read_only import _signed_in_as
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch, _transition

# --- pure projection --------------------------------------------------------


def _ev(id_: str, name: str, *, et: str = "track", **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": id_,
        "event_type_id": f"et-{et}",
        "event_type_name": et,
        "name": name,
        "title": "",
        "source_name": name,
        "description": "",
        "order": 0,
        "status": "draft",
        "sunset_at": None,
        "superseded_by": None,
        "owner_id": None,
        "reviewed": False,
        "metric_breakdown_columns": [],
        "required_presence_threshold": None,
        "field_values": [],
        "meta_values": [],
        "tags": [],
        "photos": [],
    }
    item.update(fields)
    return item


def _ov(event: str, values: list[str] | None = None, *, required: bool = False) -> dict[str, Any]:
    return {"event_type_name": "track", "event_name": event, "values": values, "required": required}


def _var(id_: str, name: str, *overrides: dict[str, Any], **fields: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": id_,
        "name": name,
        "source_name": name,
        "variable_type": "string",
        "description": "",
        "allowed_values": [],
        "bindings": [],
        "excluded_from_scans": False,
        "json_schema": None,
        "event_value_overrides": list(overrides),
    }
    item.update(fields)
    return item


def _et(name: str, *fields: str) -> dict[str, Any]:
    return {
        "id": f"et-{name}",
        "name": name,
        "display_name": name.title(),
        "description": "",
        "color": "#111111",
        "order": 0,
        "field_definitions": [
            {"id": f"fd-{name}-{f}", "name": f, "display_name": f, "field_type": "string"}
            for f in fields
        ],
    }


def _base(events: list[dict[str, Any]], variables: list[dict[str, Any]] | None = None) -> dict:
    return {
        "snapshot_version": 2,
        "event_types": [_et("track", "name", "page")],
        "events": events,
        "variables": variables or [],
        "meta_fields": [],
        "relations": [],
    }


def _sides(base: dict[str, Any]) -> tuple[dict, dict, dict]:
    """Main as the base, and a branch copy whose rows carry origin ids."""
    main = copy.deepcopy(base)
    branch = copy.deepcopy(base)
    for event in branch["events"]:
        event["origin_id"] = event["id"]
        event["id"] = f"b-{event['id']}"
    for variable in branch["variables"]:
        variable["id"] = f"b-{variable['id']}"
    return copy.deepcopy(base), main, branch


def _project(
    base: dict,
    main: dict,
    branch: dict,
    *,
    id_: str | None = None,
    key: tuple[str, str] | None = None,
    origins_complete: bool = True,
):
    blocking = merge_blocking_conflicts(base, main, branch, origins_complete=origins_complete)
    return project_merged_event(
        base,
        main,
        branch,
        target=EventTarget(id=id_, key=key),
        origins_complete=origins_complete,
        blocking=blocking,
    ).preview


def _attr(preview, key: str):
    return next(row for row in preview.attributes if row.key == key)


def _prop(preview, name: str):
    return next(row for row in preview.properties if row.name == name)


def test_an_event_added_on_the_branch_is_added() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start")]))
    branch["events"].append(_ev("b-new", "done", title="Done"))

    preview = _project(base, main, branch, id_="b-new")

    assert preview.outcome == "added"
    assert _attr(preview, "title").state == "added"
    assert _attr(preview, "title").value == "Done"
    assert preview.main_event_id is None
    assert preview.ref_id == "b-new"


def test_a_branch_only_title_change_shows_main_as_previous() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start", title="Old")]))
    branch["events"][0]["title"] = "New"

    preview = _project(base, main, branch, id_="b-e1")

    title = _attr(preview, "title")
    assert (preview.outcome, title.state, title.value, title.previous) == (
        "changed",
        "changed",
        "New",
        "Old",
    )
    assert preview.blocked is False


def test_a_main_only_edit_is_kept_and_marked() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start", description="cut")]))
    main["events"][0]["description"] = "main later"

    preview = _project(base, main, branch, id_="b-e1")

    description = _attr(preview, "description")
    assert description.value == "main later"
    assert description.state == "unchanged"
    assert description.main_moved is True
    assert preview.outcome == "unchanged"


def test_a_title_both_sides_changed_is_a_conflict_that_blocks() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start", title="Old")]))
    main["events"][0]["title"] = "Main"
    branch["events"][0]["title"] = "Branch"

    preview = _project(base, main, branch, id_="b-e1")

    title = _attr(preview, "title")
    assert (title.state, title.value, title.previous, title.branch_value) == (
        "conflict",
        None,
        "Main",
        "Branch",
    )
    assert preview.blocked is True


def test_field_values_edited_member_by_member_on_each_side_conflict_whole() -> None:
    """The merge compares ``field_values`` as one value: two members, one clash."""
    event = _ev(
        "e1",
        "start",
        field_values=[
            {"field_name": "name", "value": "a", "is_authored": True},
            {"field_name": "page", "value": "p", "is_authored": True},
        ],
    )
    base, main, branch = _sides(_base([event]))
    branch["events"][0]["field_values"][0]["value"] = "a2"
    main["events"][0]["field_values"][1]["value"] = "p2"

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.blocked is True
    assert {row.state for row in preview.field_values} == {"conflict"}
    page = next(row for row in preview.field_values if row.key == "page")
    assert (page.previous, page.branch_value) == ("p2", "p")
    assert merge_blocked_by(base, main, branch, origins_complete=True) is True


def test_a_branch_only_field_value_edit_takes_the_branch_collection() -> None:
    event = _ev(
        "e1",
        "start",
        field_values=[
            {"field_name": "name", "value": "a", "is_authored": True},
            {"field_name": "page", "value": "p", "is_authored": True},
        ],
    )
    base, main, branch = _sides(_base([event]))
    branch["events"][0]["field_values"][0]["value"] = "a2"

    preview = _project(base, main, branch, id_="b-e1")

    by_key = {row.key: row for row in preview.field_values}
    assert (by_key["name"].value, by_key["name"].previous, by_key["name"].state) == (
        "a2",
        "a",
        "changed",
    )
    assert by_key["page"].state == "unchanged"
    assert preview.outcome == "changed"


def test_an_override_list_both_sides_changed_conflicts_on_every_event_in_it() -> None:
    """Branch edits X on E2, main edits X on E1: the list clashes, E1 is blocked."""
    base, main, branch = _sides(
        _base(
            [_ev("e1", "start"), _ev("e2", "done")],
            [_var("v1", "plan", _ov("start", ["free"]), _ov("done", ["free"]))],
        )
    )
    branch["variables"][0]["event_value_overrides"][1]["values"] = ["free", "pro"]
    main["variables"][0]["event_value_overrides"][0]["values"] = ["free", "team"]

    preview = _project(base, main, branch, id_="b-e1")

    plan = _prop(preview, "plan")
    assert plan.state == "conflict"
    assert preview.blocked is True


def test_a_branch_only_override_change_shows_the_branch_entry() -> None:
    base, main, branch = _sides(
        _base([_ev("e1", "start")], [_var("v1", "plan", _ov("start", ["free"]))])
    )
    branch["variables"][0]["event_value_overrides"][0]["values"] = ["free", "pro"]

    preview = _project(base, main, branch, id_="b-e1")

    plan = _prop(preview, "plan")
    assert plan.state == "changed"
    assert plan.override is True
    assert [(v.value, v.state) for v in plan.values] == [("free", "unchanged"), ("pro", "added")]
    assert preview.blocked is False


def test_a_new_variable_with_an_entry_for_the_event_is_added() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start")]))
    branch["variables"].append(_var("b-v9", "tier", _ov("start", required=True)))

    preview = _project(base, main, branch, id_="b-e1")

    tier = _prop(preview, "tier")
    assert (tier.state, tier.variable_state, tier.required) == ("added", "added", True)


def test_edited_allowed_values_reach_an_entry_without_its_own_values() -> None:
    base, main, branch = _sides(
        _base(
            [_ev("e1", "start")],
            [_var("v1", "plan", _ov("start"), allowed_values=["free", "team"])],
        )
    )
    branch["variables"][0]["allowed_values"] = ["free", "pro"]

    preview = _project(base, main, branch, id_="b-e1")

    plan = _prop(preview, "plan")
    assert plan.variable_state == "changed"
    assert {(v.value, v.state) for v in plan.values} == {
        ("free", "unchanged"),
        ("pro", "added"),
        ("team", "removed"),
    }


@pytest.mark.parametrize(
    ("main_status", "branch_status"),
    [("live", "archived"), ("live", "draft")],
)
def test_a_status_the_merge_does_not_carry_shows_main_and_says_so(
    main_status: str, branch_status: str
) -> None:
    base, main, branch = _sides(_base([_ev("e1", "start", status=main_status)]))
    branch["events"][0]["status"] = branch_status

    preview = _project(base, main, branch, id_="b-e1")

    status = _attr(preview, "status")
    assert (status.value, status.branch_value, status.state) == (
        main_status,
        branch_status,
        "unchanged",
    )
    assert status.note is not None
    assert status.note in preview.notes


def test_a_branch_rename_lands_when_main_kept_the_base_name() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start")]))
    branch["events"][0]["name"] = "begin"

    preview = _project(base, main, branch, id_="b-e1")

    assert (preview.name, preview.previous_name) == ("begin", "start")


def test_main_renamed_too_keeps_main_name() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start")]))
    branch["events"][0]["name"] = "begin"
    main["events"][0]["name"] = "kickoff"

    preview = _project(base, main, branch, id_="b-e1")

    assert (preview.name, preview.previous_name) == ("kickoff", None)


def test_a_renamed_variable_finds_each_side_entry_by_that_side_key() -> None:
    """The branch renames both the variable and the event; main keeps both names.

    The branch's entry is keyed by the branch's event name, main's by main's.
    """
    base, main, branch = _sides(
        _base([_ev("e1", "start")], [_var("v1", "plan", _ov("start", ["free"]), source_name="S")])
    )
    branch["events"][0]["name"] = "begin"
    branch["variables"][0]["name"] = "plan_v2"
    branch["variables"][0]["event_value_overrides"] = [_ov("begin", ["free", "pro"])]

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.blocked is False
    assert (preview.name, preview.previous_name) == ("begin", "start")
    prop = _prop(preview, "plan_v2")
    assert prop.state == "changed"
    assert [(v.value, v.state) for v in prop.values] == [("free", "unchanged"), ("pro", "added")]


def test_a_renamed_variable_conflict_is_matched_under_its_other_names() -> None:
    """The gate names the variable by main's name; the merged name differs."""
    base, main, branch = _sides(
        _base([_ev("e1", "start")], [_var("v1", "plan", _ov("start", ["free"]), source_name="S")])
    )
    branch["variables"][0]["name"] = "plan_v2"
    main["variables"][0]["event_value_overrides"][0]["values"] = ["free", "team"]

    blocking = merge_blocking_conflicts(base, main, branch, origins_complete=True)
    assert {"entity_type": "variable", "name": "plan"} in blocking

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.blocked is True
    assert _prop(preview, "plan_v2").state == "conflict"


def test_an_event_whose_type_main_deleted_is_skipped_and_blocked() -> None:
    base_payload = _base([_ev("e1", "start")])
    base_payload["event_types"].append(_et("screen"))
    base, main, branch = _sides(base_payload)
    main["event_types"] = [et for et in main["event_types"] if et["name"] != "screen"]
    branch["events"].append(_ev("b-new", "home", et="screen"))

    preview = _project(base, main, branch, id_="b-new")

    assert preview.outcome == "skipped"
    assert preview.blocked is True
    assert any("not on main" in note for note in preview.notes)
    assert any("event type 'screen'" in note for note in preview.notes)


def test_an_event_main_deleted_and_the_branch_left_alone_is_removed() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start"), _ev("e2", "done")]))
    main["events"] = [e for e in main["events"] if e["id"] != "e1"]

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.outcome == "removed"
    assert preview.blocked is False
    assert any("Main deleted" in note for note in preview.notes)


def test_a_photo_only_conflict_blocks_and_says_photos() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start")]))
    main["events"][0]["photos"] = [{"original_filename": "a.png", "kind": "photo"}]
    branch["events"][0]["photos"] = [{"original_filename": "b.png", "kind": "photo"}]

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.blocked is True
    assert any("Photos" in note for note in preview.notes)
    assert all(row.state != "conflict" for row in preview.attributes)


def test_both_sides_reordering_keeps_main_order_and_no_conflict() -> None:
    """``order`` is main-wins: never a conflict, and main's position stands."""
    base, main, branch = _sides(_base([_ev("e1", "start", order=1)]))
    main["events"][0]["order"] = 5
    branch["events"][0]["order"] = 9

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.blocked is False
    assert preview.outcome == "unchanged"
    assert "order" not in conflicting_fields(
        base["events"][0], main["events"][0], branch["events"][0], _EV_CHANGE_KEYS
    )


def test_main_deleted_and_the_branch_edited_shows_the_branch_side() -> None:
    """A presence conflict shows each member's branch value, not two blanks."""
    event = _ev(
        "e1",
        "start",
        field_values=[{"field_name": "page", "value": "p", "is_authored": True}],
    )
    base, main, branch = _sides(_base([event, _ev("e2", "done")]))
    main["events"] = [e for e in main["events"] if e["id"] != "e1"]
    branch["events"][0]["field_values"][0]["value"] = "p2"
    branch["events"][0]["tags"] = ["funnel"]

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.blocked is True
    assert any("Deleted on one side" in note for note in preview.notes)
    page = next(row for row in preview.field_values if row.key == "page")
    assert (page.state, page.previous, page.branch_value) == ("conflict", None, "p2")
    funnel = next(row for row in preview.tags if row.key == "funnel")
    assert (funnel.state, funnel.previous, funnel.branch_value) == ("conflict", None, "funnel")


def test_an_event_added_on_both_sides_clashes_only_where_they_differ() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start")], [_var("v1", "plan")]))
    main["events"].append(_ev("m-x", "x", title="A"))
    branch["events"].append(_ev("b-x", "x", title="B"))
    branch["variables"][0]["event_value_overrides"] = [_ov("x", ["free"])]

    preview = _project(base, main, branch, id_="b-x")

    assert preview.blocked is True
    assert not any("Deleted on one side" in note for note in preview.notes)
    assert any("Added on both sides" in note for note in preview.notes)
    title = _attr(preview, "title")
    assert (title.state, title.previous, title.branch_value) == ("conflict", "A", "B")
    assert _attr(preview, "status").state == "unchanged"
    assert _attr(preview, "description").state == "unchanged"
    assert _prop(preview, "plan").state == "added"


def test_a_variable_description_clash_leaves_the_property_values_alone() -> None:
    """The gate refuses on the description; the event's entry lands unchanged."""
    base, main, branch = _sides(
        _base([_ev("e1", "start")], [_var("v1", "plan", _ov("start", ["free", "pro"]))])
    )
    main["variables"][0]["description"] = "Main"
    branch["variables"][0]["description"] = "Branch"

    preview = _project(base, main, branch, id_="b-e1")

    plan = _prop(preview, "plan")
    assert preview.blocked is True
    assert plan.variable_state == "conflict"
    assert plan.state == "unchanged"
    assert [(v.value, v.state) for v in plan.values] == [
        ("free", "unchanged"),
        ("pro", "unchanged"),
    ]
    assert any("'plan' conflicts with main on description" in note for note in preview.notes)


def test_a_clean_event_while_another_blocks_is_not_blocked() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start"), _ev("e2", "done", title="t")]))
    branch["events"][0]["title"] = "clean"
    main["events"][1]["title"] = "main"
    branch["events"][1]["title"] = "branch"

    preview = _project(base, main, branch, id_="b-e1")

    assert preview.blocked is False
    assert merge_blocked_by(base, main, branch, origins_complete=True) is True


def test_a_namesake_the_merge_cannot_place_is_ambiguous() -> None:
    """A branch opened before origin ids, two rows under one key."""
    base_payload = _base([_ev("e1", "dup"), _ev("e2", "dup")])
    base = copy.deepcopy(base_payload)
    main = copy.deepcopy(base_payload)
    branch = copy.deepcopy(base_payload)
    for event in branch["events"]:
        event["id"] = f"b-{event['id']}"

    with pytest.raises(AmbiguousEvent):
        _project(base, main, branch, key=("track", "dup"), origins_complete=False)


def test_a_key_lookup_equals_the_id_lookup() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start", title="Old")]))
    branch["events"][0]["title"] = "New"

    by_id = _project(base, main, branch, id_="b-e1")
    by_key = _project(base, main, branch, key=("track", "start"))
    by_main_id = _project(base, main, branch, id_="e1")

    assert by_id == by_key == by_main_id


def test_a_main_only_event_is_unchanged() -> None:
    base, main, branch = _sides(_base([_ev("e1", "start")]))
    main["events"].append(_ev("m-new", "scanned", title="From a scan"))

    preview = _project(base, main, branch, id_="m-new")

    assert preview.outcome == "unchanged"
    assert _attr(preview, "title").value == "From a scan"


# --- the gate, extracted ---------------------------------------------------


def _gate_fixtures() -> list[tuple[dict, dict, dict]]:
    out = []
    clean = _sides(_base([_ev("e1", "start")]))
    out.append(clean)
    title = _sides(_base([_ev("e1", "start")]))
    title[1]["events"][0]["title"] = "m"
    title[2]["events"][0]["title"] = "b"
    out.append(title)
    et_color = _sides(_base([_ev("e1", "start")]))
    et_color[1]["event_types"][0]["color"] = "#aaaaaa"
    et_color[2]["event_types"][0]["color"] = "#bbbbbb"
    out.append(et_color)
    variable = _sides(_base([_ev("e1", "start")], [_var("v1", "plan")]))
    variable[1]["variables"][0]["description"] = "m"
    variable[2]["variables"][0]["description"] = "b"
    out.append(variable)
    return out


@pytest.mark.parametrize("sides", _gate_fixtures())
def test_merge_blocked_by_is_the_shared_gate(sides: tuple[dict, dict, dict]) -> None:
    base, main, branch = sides
    assert merge_blocked_by(base, main, branch, origins_complete=True) == bool(
        merge_blocking_conflicts(base, main, branch, origins_complete=True)
    )


def test_an_event_type_field_conflict_is_resolvable_not_blocking() -> None:
    base, main, branch = _gate_fixtures()[2]
    assert merge_blocking_conflicts(base, main, branch, origins_complete=True) == []


@pytest.mark.parametrize("sides", _gate_fixtures())
def test_conflicting_fields_agrees_with_the_conflict_set(sides: tuple[dict, dict, dict]) -> None:
    base, main, branch = sides
    flagged = {
        row["name"]
        for row in _conflict_set(
            entity_type="event",
            base_items=base["events"],
            ours_items=main["events"],
            theirs_items=branch["events"],
            key_fn=lambda x: (x["event_type_name"], x["name"]),
            name_fn=lambda x: f"{x['event_type_name']}.{x['name']}",
            change_keys=_EV_CHANGE_KEYS,
            anchored=True,
            theirs_origins_complete=True,
        )
    }
    by_fields = {
        f"{o['event_type_name']}.{o['name']}"
        for b, o, t in zip(base["events"], main["events"], branch["events"], strict=True)
        if conflicting_fields(b, o, t, _EV_CHANGE_KEYS)
    }
    assert flagged == by_fields


# --- route and parity --------------------------------------------------------


async def _seed(client: AsyncClient, slug: str) -> dict[str, str]:
    """track with fields name+page; checkout_started and checkout_done; plan."""
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    et_id = et.json()["id"]
    field_ids = {}
    for name in ("name", "page"):
        field = await client.post(
            f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
            json={"name": name, "display_name": name.title(), "field_type": "string"},
        )
        assert field.status_code == 201, field.text
        field_ids[name] = field.json()["id"]
    ids: dict[str, str] = {"et": et_id}
    for event_name in ("checkout_started", "checkout_done"):
        event = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": et_id,
                "name": event_name,
                "title": event_name.replace("_", " "),
                "field_values": [
                    {"field_definition_id": field_ids["name"], "value": "Checkout"},
                    {"field_definition_id": field_ids["page"], "value": "/cart"},
                ],
            },
        )
        assert event.status_code == 201, event.text
        ids[event_name] = event.json()["id"]
    variable = await client.post(
        f"/api/v1/projects/{slug}/variables", json={"name": "plan", "allowed_values": ["free"]}
    )
    assert variable.status_code == 201, variable.text
    ids["plan"] = variable.json()["id"]
    for event_name in ("checkout_started", "checkout_done"):
        resp = await client.put(
            f"/api/v1/projects/{slug}/variables/{ids['plan']}/event-overrides/{ids[event_name]}",
            json={"values": ["free"]},
        )
        assert resp.status_code == 200, resp.text
    return ids


async def _branch_ids(client: AsyncClient, slug: str, branch_id: str) -> dict[str, Any]:
    events = (await client.get(f"/api/v1/projects/{slug}/events?branch={branch_id}")).json()
    variables = (await client.get(f"/api/v1/projects/{slug}/variables?branch={branch_id}")).json()
    out: dict[str, Any] = {e["name"]: e for e in events["items"]}
    out["plan"] = next(v["id"] for v in variables["items"] if v["name"] == "plan")
    return out


async def _branch_event(
    client: AsyncClient, slug: str, branch_id: str, event_id: str
) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}?branch={branch_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _field_values(event: dict[str, Any], old: str, new: str) -> list[dict[str, str]]:
    """The event's field values with the one holding ``old`` set to ``new``."""
    return [
        {
            "field_definition_id": fv["field_definition_id"],
            "value": new if fv["value"] == old else fv["value"],
        }
        for fv in event["field_values"]
    ]


async def _preview(client: AsyncClient, slug: str, branch_id: str, **params: str):
    return await client.get(
        f"/api/v1/projects/{slug}/branches/{branch_id}/merge-preview/event", params=params
    )


async def _clean_branch(client: AsyncClient, slug: str) -> tuple[dict[str, str], str, dict]:
    ids = await _seed(client, slug)
    branch_id = await _create_branch(client, slug, "WND-2")
    on_branch = await _branch_ids(client, slug, branch_id)
    started = on_branch["checkout_started"]
    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{started['id']}?branch={branch_id}",
        json={"title": "Start checkout", "tags": ["funnel"]},
    )
    assert resp.status_code == 200, resp.text
    for event_name in ("checkout_started", "checkout_done"):
        resp = await client.put(
            f"/api/v1/projects/{slug}/variables/{on_branch['plan']}/event-overrides/"
            f"{on_branch[event_name]['id']}?branch={branch_id}",
            json={"values": ["free", "pro"]},
        )
        assert resp.status_code == 200, resp.text
    # Main moves on after the cut, on a field the branch left alone.
    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{ids['checkout_started']}",
        json={"description": "Main wrote this later"},
    )
    assert resp.status_code == 200, resp.text
    return ids, branch_id, on_branch


@pytest.mark.asyncio
async def test_a_viewer_reads_the_preview_by_id_and_by_key(client: AsyncClient) -> None:
    slug = "merge-preview-viewer"
    _, branch_id, on_branch = await _clean_branch(client, slug)
    async with _signed_in_as("viewer", client, slug) as (viewer, headers):
        by_id = await viewer.get(
            f"/api/v1/projects/{slug}/branches/{branch_id}/merge-preview/event",
            params={"event_id": on_branch["checkout_started"]["id"]},
            headers=headers,
        )
        by_key = await viewer.get(
            f"/api/v1/projects/{slug}/branches/{branch_id}/merge-preview/event",
            params={"event_type": "track", "event_name": "checkout_started"},
            headers=headers,
        )
    assert by_id.status_code == 200, by_id.text
    assert by_id.json() == by_key.json()
    body = by_id.json()
    assert body["behind_base"] is True
    attributes = {row["key"]: row for row in body["attributes"]}
    assert attributes["title"]["state"] == "changed"
    assert attributes["description"]["value"] == "Main wrote this later"
    assert attributes["description"]["main_moved"] is True


@pytest.mark.asyncio
async def test_the_preview_refuses_bad_targets(client: AsyncClient) -> None:
    slug = "merge-preview-refusals"
    _, branch_id, _ = await _clean_branch(client, slug)
    branches = (await client.get(f"/api/v1/projects/{slug}/branches")).json()["items"]
    main_id = next(b for b in branches if b["kind"] == "main")["id"]

    assert (await _preview(client, slug, main_id, event_id=str(uuid.uuid4()))).status_code == 400
    assert (await _preview(client, slug, branch_id, event_id=str(uuid.uuid4()))).status_code == 404
    assert (await _preview(client, slug, branch_id)).status_code == 422
    both = await _preview(
        client,
        slug,
        branch_id,
        event_id=str(uuid.uuid4()),
        event_type="track",
        event_name="checkout_done",
    )
    assert both.status_code == 422
    half = await _preview(client, slug, branch_id, event_type="track")
    assert half.status_code == 422


@pytest.mark.asyncio
async def test_a_removed_event_is_found_by_its_base_side_id(client: AsyncClient) -> None:
    slug = "merge-preview-removed"
    ids = await _seed(client, slug)
    branch_id = await _create_branch(client, slug, "drop")
    on_branch = await _branch_ids(client, slug, branch_id)
    resp = await client.delete(
        f"/api/v1/projects/{slug}/events/{on_branch['checkout_done']['id']}?branch={branch_id}"
    )
    assert resp.status_code in (200, 204), resp.text

    preview = await _preview(client, slug, branch_id, event_id=ids["checkout_done"])

    assert preview.status_code == 200, preview.text
    assert preview.json()["outcome"] == "removed"


@pytest.mark.asyncio
async def test_a_branch_without_a_complete_base_gets_the_merge_refusal(
    client: AsyncClient,
) -> None:
    slug = "merge-preview-nobase"
    ids = await _seed(client, slug)
    branch_id = await _create_branch(client, slug, "legacy")
    async with TestSessionLocal() as session:
        branch = await session.get(PlanBranch, uuid.UUID(branch_id))
        assert branch is not None
        branch.base_revision_id = None
        await session.commit()

    preview = await _preview(client, slug, branch_id, event_id=ids["checkout_done"])
    await _transition(client, slug, branch_id, "submit")
    await _transition(client, slug, branch_id, "approve")
    merge = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")

    assert preview.status_code == merge.status_code == 409
    assert preview.json()["detail"] == merge.json()["detail"]
    assert preview.json()["detail"]["incomplete_base_snapshot"] is True


@pytest.mark.asyncio
async def test_a_closed_branch_is_not_open(client: AsyncClient) -> None:
    slug = "merge-preview-closed"
    ids = await _seed(client, slug)
    branch_id = await _create_branch(client, slug, "gone")
    await _transition(client, slug, branch_id, "close")

    preview = await _preview(client, slug, branch_id, event_id=ids["checkout_done"])

    assert preview.status_code == 409
    assert preview.json()["detail"]["branch_not_open"] is True


@pytest.mark.asyncio
async def test_a_clean_event_beside_a_blocked_one_reports_the_branch_wide_block(
    client: AsyncClient,
) -> None:
    slug = "merge-preview-elsewhere"
    ids = await _seed(client, slug)
    branch_id = await _create_branch(client, slug, "mixed")
    on_branch = await _branch_ids(client, slug, branch_id)
    # checkout_done clashes on its title; checkout_started is clean.
    await client.patch(
        f"/api/v1/projects/{slug}/events/{on_branch['checkout_done']['id']}?branch={branch_id}",
        json={"title": "branch"},
    )
    await client.patch(
        f"/api/v1/projects/{slug}/events/{ids['checkout_done']}", json={"title": "main"}
    )

    body = (
        await _preview(client, slug, branch_id, event_id=on_branch["checkout_started"]["id"])
    ).json()

    assert body["blocked"] is False
    assert body["branch_merge_blocked"] is True
    assert body["other_blocking_count"] == 1


async def _main_snapshot(slug: str) -> dict[str, Any]:
    async with TestSessionLocal() as session:
        project = (await session.execute(select(Project).where(Project.slug == slug))).scalar_one()
        main_branch = (
            await session.execute(
                select(PlanBranch).where(
                    PlanBranch.project_id == project.id, PlanBranch.kind == "main"
                )
            )
        ).scalar_one()
        return await build_plan_snapshot(session, project.id, branch_id=main_branch.id)


@pytest.mark.asyncio
async def test_parity_a_clean_merge_lands_what_the_preview_showed(client: AsyncClient) -> None:
    """The drift guard: preview every touched event, merge, compare main."""
    slug = "merge-preview-parity"
    _, branch_id, on_branch = await _clean_branch(client, slug)
    started = await _branch_event(client, slug, branch_id, on_branch["checkout_started"]["id"])
    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{started['id']}?branch={branch_id}",
        json={"field_values": _field_values(started, "Checkout", "Checkout v2")},
    )
    assert resp.status_code == 200, resp.text

    previews = {}
    for event_name in ("checkout_started", "checkout_done"):
        resp = await _preview(client, slug, branch_id, event_id=on_branch[event_name]["id"])
        assert resp.status_code == 200, resp.text
        previews[event_name] = resp.json()
        assert previews[event_name]["blocked"] is False

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text

    snapshot = await _main_snapshot(slug)
    for event_name, preview in previews.items():
        event = next(e for e in snapshot["events"] if e["id"] == preview["main_event_id"])
        assert event["name"] == preview["name"]
        for row in preview["attributes"]:
            assert event[row["key"]] == row["value"], (event_name, row["key"])
        assert {fv["field_name"]: fv["value"] for fv in event["field_values"]} == {
            row["key"]: row["value"] for row in preview["field_values"] if row["state"] != "removed"
        }
        assert sorted(event["tags"]) == sorted(
            row["value"] for row in preview["tags"] if row["state"] != "removed"
        )
        properties = (
            await client.get(f"/api/v1/projects/{slug}/events/{event['id']}/properties")
        ).json()
        assert {p["name"]: (p["required"], p["effective_values"]) for p in properties} == {
            p["name"]: (p["required"], [v["value"] for v in p["values"] if v["state"] != "removed"])
            for p in preview["properties"]
            if p["state"] != "removed"
        }
    started_preview = previews["checkout_started"]
    plan = next(p for p in started_preview["properties"] if p["name"] == "plan")
    assert {(v["value"], v["state"]) for v in plan["values"]} == {
        ("free", "unchanged"),
        ("pro", "added"),
    }


@pytest.mark.asyncio
async def test_parity_the_preview_blocks_exactly_what_the_merge_refuses(
    client: AsyncClient,
) -> None:
    slug = "merge-preview-refused"
    ids = await _seed(client, slug)
    branch_id = await _create_branch(client, slug, "clash")
    on_branch = await _branch_ids(client, slug, branch_id)
    started = await _branch_event(client, slug, branch_id, on_branch["checkout_started"]["id"])
    # field_values: the branch edits `name`, main edits `page` — one key, both sides.
    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{started['id']}?branch={branch_id}",
        json={"field_values": _field_values(started, "Checkout", "Branch name")},
    )
    assert resp.status_code == 200, resp.text
    main_started = (
        await client.get(f"/api/v1/projects/{slug}/events/{ids['checkout_started']}")
    ).json()
    resp = await client.patch(
        f"/api/v1/projects/{slug}/events/{ids['checkout_started']}",
        json={"field_values": _field_values(main_started, "/cart", "/basket")},
    )
    assert resp.status_code == 200, resp.text
    # plan's override list: the branch edits checkout_done's, main checkout_started's.
    await client.put(
        f"/api/v1/projects/{slug}/variables/{on_branch['plan']}/event-overrides/"
        f"{on_branch['checkout_done']['id']}?branch={branch_id}",
        json={"values": ["free", "pro"]},
    )
    await client.put(
        f"/api/v1/projects/{slug}/variables/{ids['plan']}/event-overrides/"
        f"{ids['checkout_started']}",
        json={"values": ["free", "team"]},
    )

    started_preview = (await _preview(client, slug, branch_id, event_id=started["id"])).json()
    done_preview = (
        await _preview(client, slug, branch_id, event_id=on_branch["checkout_done"]["id"])
    ).json()
    merge = await _approve_and_merge(client, slug, branch_id)

    assert merge.status_code == 409, merge.text
    refused = {(c["entity_type"], c["name"]) for c in merge.json()["detail"]["conflicts"]}
    assert refused == {("event", "track.checkout_started"), ("variable", "plan")}
    assert started_preview["blocked"] is True
    assert started_preview["other_blocking_count"] == 0
    assert {row["state"] for row in started_preview["field_values"]} == {"conflict"}
    assert done_preview["blocked"] is True
    assert next(p for p in done_preview["properties"] if p["name"] == "plan")["state"] == (
        "conflict"
    )
