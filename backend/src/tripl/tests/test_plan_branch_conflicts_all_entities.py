"""The three-way overlap check across every entity type.

Pure: payloads are hand-built snapshots, so each rule of
``_plan_branch_three_way`` is pinned without a database. The endpoint side is
covered in ``test_plan_branch_update_from_main.py``.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from tripl.schemas.plan_branch import ResolutionCreate
from tripl.services._plan_branch_three_way import plan_three_way
from tripl.services._plan_branch_three_way_model import PRESENCE_FIELD
from tripl.services.plan_branch_conflicts import (
    _detect_merge_conflicts,
    detect_field_conflicts,
    merge_blocked_by,
)


def _event(
    id_: str, name: str, *, et: str = "track", origin: str | None = None, **fields: Any
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": id_,
        "event_type_id": f"et-{et}",
        "event_type_name": et,
        "name": name,
        "title": "",
        "source_name": None,
        "description": "",
        "order": 0,
        "status": "draft",
        "sunset_at": None,
        "superseded_by": None,
        "owner_id": None,
        "reviewed": False,
        "metric_breakdown_columns": [],
        "field_values": [],
        "meta_values": [],
        "tags": [],
        "photos": [],
    }
    if origin is not None:
        item["origin_id"] = origin
    item.update(fields)
    return item


def _field(id_: str, name: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "display_name": name.title(),
        "field_type": "string",
        "is_required": False,
        "enum_options": None,
        "description": "",
        "order": 0,
        "sensitivity": "none",
        "contract_required_max_null_rate": None,
        "contract_regex": None,
        "contract_min_value": None,
        "contract_max_value": None,
        "contract_max_bad_rate": 0.0,
        **fields,
    }


def _payload() -> dict[str, Any]:
    """Main at the cut: two event types, one event, one of everything else."""
    return {
        "snapshot_version": 2,
        "event_types": [
            {
                "id": "et-track",
                "name": "track",
                "display_name": "Track",
                "description": "",
                "color": "#111111",
                "order": 0,
                "field_definitions": [_field("fd-name", "name")],
            },
            {
                "id": "et-screen",
                "name": "screen",
                "display_name": "Screen",
                "description": "",
                "color": "#222222",
                "order": 1,
                "field_definitions": [_field("fd-id", "id")],
            },
        ],
        "events": [_event("ev-1", "purchase")],
        "variables": [
            {
                "id": "var-1",
                "name": "currency",
                "source_name": "S1",
                "variable_type": "string",
                "description": "",
                "allowed_values": [],
                "bindings": [],
                "excluded_from_scans": False,
                "event_value_overrides": [],
            }
        ],
        "meta_fields": [
            {
                "id": "mf-1",
                "name": "team",
                "display_name": "Team",
                "field_type": "string",
                "is_required": False,
                "allow_multiple": False,
                "enum_options": None,
                "default_value": None,
                "link_template": None,
                "order": 0,
                "sensitivity": "none",
            }
        ],
        "relations": [
            {
                "id": "rel-1",
                "source_event_type_id": "et-track",
                "source_event_type_name": "track",
                "target_event_type_id": "et-screen",
                "target_event_type_name": "screen",
                "source_field_name": "name",
                "target_field_name": "id",
                "relation_type": "belongs_to",
                "description": "",
            }
        ],
    }


def _branch_of(base: dict[str, Any]) -> dict[str, Any]:
    """The branch's deep copy: fresh ids, events and relations linked by origin."""
    branch = copy.deepcopy(base)
    for event_type in branch["event_types"]:
        event_type["id"] = f"b-{event_type['id']}"
        for fd in event_type["field_definitions"]:
            fd["id"] = f"b-{fd['id']}"
    for key in ("events", "relations"):
        for item in branch[key]:
            item["origin_id"] = item["id"]
            item["id"] = f"b-{item['id']}"
    for key in ("variables", "meta_fields"):
        for item in branch[key]:
            item["id"] = f"b-{item['id']}"
    return branch


def _sides() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    base = _payload()
    return base, copy.deepcopy(base), _branch_of(base)


def _rows(base: dict[str, Any], main: dict[str, Any], branch: dict[str, Any]) -> list[dict]:
    return detect_field_conflicts(base, main, branch, origins_complete=True)


def test_every_entity_type_reports_a_same_field_conflict() -> None:
    base, main, branch = _sides()
    main["event_types"][0]["color"] = "#aaaaaa"
    branch["event_types"][0]["color"] = "#bbbbbb"
    main["event_types"][0]["field_definitions"][0]["field_type"] = "number"
    branch["event_types"][0]["field_definitions"][0]["field_type"] = "boolean"
    main["events"][0]["description"] = "main"
    branch["events"][0]["description"] = "branch"
    main["variables"][0]["description"] = "main"
    branch["variables"][0]["description"] = "branch"
    main["meta_fields"][0]["enum_options"] = ["a"]
    branch["meta_fields"][0]["enum_options"] = ["b"]
    main["relations"][0]["relation_type"] = "has_many"
    branch["relations"][0]["relation_type"] = "has_one"

    rows = _rows(base, main, branch)

    found = {(row["entity_type"], row["name"], row["field"]) for row in rows}
    assert found == {
        ("event_type", "track", "color"),
        ("field_definition", "track.name", "field_type"),
        ("event", "track.purchase", "description"),
        ("variable", "currency", "description"),
        ("meta_field", "team", "enum_options"),
        ("relation", "track.name->screen.id", "relation_type"),
    }
    event_row = next(row for row in rows if row["entity_type"] == "event")
    assert event_row["parent"] == "track"
    assert event_row["label"] == "purchase"
    assert (event_row["base"], event_row["ours"], event_row["theirs"]) == ("", "main", "branch")


def test_one_sided_and_convergent_edits_are_not_conflicts() -> None:
    base, main, branch = _sides()
    main["events"][0]["description"] = "main only"
    main["variables"][0]["description"] = "same"
    branch["variables"][0]["description"] = "same"
    branch["meta_fields"][0]["display_name"] = "branch only"

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert plan.rows == []
    writes = [op for op in plan.ops if op.kind == "write"]
    assert [(op.entity_type, op.fields) for op in writes] == [("event", ("description",))]


def test_different_fields_of_one_entity_merge_without_conflict() -> None:
    base, main, branch = _sides()
    main["events"][0]["title"] = "Main title"
    branch["events"][0]["description"] = "branch text"

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert plan.rows == []
    assert [op.fields for op in plan.ops if op.kind == "write"] == [("title",)]


def test_collection_fields_are_one_conflict_each() -> None:
    base, main, branch = _sides()
    main["events"][0]["tags"] = ["a"]
    branch["events"][0]["tags"] = ["b"]
    main["events"][0]["field_values"] = [{"field_name": "name", "value": "x", "is_authored": True}]
    branch["events"][0]["field_values"] = [
        {"field_name": "name", "value": "y", "is_authored": True}
    ]

    rows = _rows(base, main, branch)

    assert sorted(row["field"] for row in rows) == ["field_values", "tags"]


def test_photo_comments_are_not_an_overlap() -> None:
    base, main, branch = _sides()
    photo = {"storage_key_fingerprint": "k", "sort_order": 0, "comments": []}
    for side in (base, main, branch):
        side["events"][0]["photos"] = [copy.deepcopy(photo)]
    main["events"][0]["photos"][0]["comments"] = [{"body_fingerprint": "m", "replies": []}]
    branch["events"][0]["photos"][0]["comments"] = [{"body_fingerprint": "b", "replies": []}]

    assert _rows(base, main, branch) == []


def test_rename_on_main_plus_edit_on_branch_is_no_conflict() -> None:
    base, main, branch = _sides()
    main["events"][0]["name"] = "purchase_done"
    branch["events"][0]["description"] = "branch"
    main["variables"][0]["name"] = "currency_code"
    branch["variables"][0]["description"] = "branch"

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert plan.rows == []
    renames = {op.entity_type: op.main["name"] for op in plan.ops if "name" in op.fields}
    assert renames == {"event": "purchase_done", "variable": "currency_code"}
    assert plan.main_changes["variable"]["renamed"] == 1


def test_renames_to_different_names_are_a_name_conflict() -> None:
    base, main, branch = _sides()
    main["variables"][0]["name"] = "currency_code"
    branch["variables"][0]["name"] = "currency_iso"

    rows = _rows(base, main, branch)

    assert [(row["entity_type"], row["field"], row["ours"], row["theirs"]) for row in rows] == [
        ("variable", "name", "currency_code", "currency_iso")
    ]


def test_main_deleted_what_the_branch_edited_is_a_presence_conflict() -> None:
    base, main, branch = _sides()
    main["events"] = []
    branch["events"][0]["description"] = "kept on the branch"

    plan = plan_three_way(base, main, branch, origins_complete=True)
    assert [(row["field"], row["base"], row["ours"], row["theirs"]) for row in plan.rows] == [
        (PRESENCE_FIELD, "present", "absent", "present")
    ]
    assert plan.unresolved

    key = ("event", "track.purchase", PRESENCE_FIELD)
    take_main = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "ours"})
    assert [(op.kind, op.entity_type) for op in take_main.ops] == [("delete", "event")]

    keep = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "theirs"})
    assert [(op.kind, op.main) for op in keep.ops] == [("origin", None)]


def test_branch_deleted_what_main_edited_recreates_on_take_main() -> None:
    base, main, branch = _sides()
    main["variables"][0]["description"] = "main edit"
    branch["variables"] = []
    key = ("variable", "currency", PRESENCE_FIELD)

    take_main = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "ours"})
    assert [(op.kind, op.entity_type) for op in take_main.ops] == [("create", "variable")]
    keep = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "theirs"})
    assert keep.ops == []


def test_unedited_main_deletion_applies_without_asking() -> None:
    base, main, branch = _sides()
    main["meta_fields"] = []

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert plan.rows == []
    assert [(op.kind, op.entity_type) for op in plan.ops] == [("delete", "meta_field")]


def test_parent_deleted_on_main_while_branch_adds_a_child() -> None:
    base, main, branch = _sides()
    main["event_types"] = [et for et in main["event_types"] if et["name"] != "screen"]
    main["relations"] = []
    branch["events"].append(_event("b-new", "opened", et="screen"))

    plan = plan_three_way(base, main, branch, origins_complete=True)

    # One question about the parent, none about what hangs off it.
    assert [(row["entity_type"], row["name"], row["field"]) for row in plan.rows] == [
        ("event_type", "screen", PRESENCE_FIELD)
    ]
    assert plan.rows[0]["dependents"] == 1
    key = ("event_type", "screen", PRESENCE_FIELD)
    take_main = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "ours"})
    assert [(op.kind, op.entity_type, op.cascade) for op in take_main.ops] == [
        ("delete", "event_type", True)
    ]
    keep = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "theirs"})
    # The kept relation stops pointing at main's deleted row.
    assert [(op.kind, op.entity_type, op.main) for op in keep.ops] == [("origin", "relation", None)]


def test_main_additions_are_created_and_both_sides_adding_one_event_pairs_it() -> None:
    base, main, branch = _sides()
    main["events"].append(_event("ev-2", "signup", description="main"))
    branch["events"].append(_event("b-ev-2", "signup", description="branch"))
    main["events"].append(_event("ev-3", "logout"))

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert [(row["name"], row["field"], row["base"]) for row in plan.rows] == [
        ("track.signup", "description", None)
    ]
    assert ("create", "track.logout") in {
        (op.kind, f"{op.main['event_type_name']}.{op.main['name']}")
        for op in plan.ops
        if op.main is not None
    }
    origin_ops = [op for op in plan.ops if op.kind == "origin"]
    assert [(op.branch["id"], op.main["id"]) for op in origin_ops] == [("b-ev-2", "ev-2")]


def test_every_merge_conflict_is_listed_or_blocks_the_merge() -> None:
    base, main, branch = _sides()
    main["event_types"][0]["color"] = "#aaaaaa"
    branch["event_types"][0]["color"] = "#bbbbbb"
    main["events"][0]["description"] = "main"
    branch["events"][0]["description"] = "branch"
    main["variables"][0]["name"] = "currency_code"
    branch["variables"][0]["description"] = "branch"

    rows = _rows(base, main, branch)
    listed = {(row["entity_type"], row["name"]) for row in rows}
    blocked = merge_blocked_by(base, main, branch, origins_complete=True)
    for conflict in _detect_merge_conflicts(base, main, branch, theirs_origins_complete=True):
        assert (conflict["entity_type"], conflict["name"]) in listed or blocked
    # The event conflict is a hard merge refusal today; the header must say so.
    assert blocked is True


def test_resolution_rejects_an_unknown_entity_type() -> None:
    with pytest.raises(ValidationError):
        ResolutionCreate(
            entity_type="widget",  # type: ignore[arg-type]
            entity_name="x",
            field_name="name",
            choice="ours",  # type: ignore[arg-type]
        )


# --- review follow-ups: definitions, ambiguity, identity clashes ---------------


def _values_write(plan: Any, field: str) -> list[dict[str, Any]] | None:
    """The ``field`` collection the plan writes onto the branch's event, if any."""
    for op in plan.ops:
        if op.kind == "write" and op.entity_type == "event" and field in op.fields:
            return list(op.main[field])
    return None


def test_keeping_a_field_main_deleted_keeps_its_event_values() -> None:
    base, main, branch = _sides()
    for side in (base, main, branch):
        side["events"][0]["field_values"] = [
            {"field_name": "name", "value": "x", "is_authored": True}
        ]
    main["event_types"][0]["field_definitions"] = []
    main["relations"] = []
    main["events"][0]["field_values"] = []
    branch["event_types"][0]["field_definitions"][0]["description"] = "branch"
    key = ("field_definition", "track.name", PRESENCE_FIELD)

    plan = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "theirs"})

    assert [(row["name"], row["field"]) for row in plan.rows] == [("track.name", PRESENCE_FIELD)]
    # Nothing rewrites the event: its value for the kept field stays.
    assert _values_write(plan, "field_values") is None


def test_keeping_a_meta_field_main_deleted_keeps_its_event_values() -> None:
    base, main, branch = _sides()
    for side in (base, main, branch):
        side["events"][0]["meta_values"] = [{"meta_field_name": "team", "value": "core"}]
    main["meta_fields"] = []
    main["events"][0]["meta_values"] = []
    main["events"][0]["description"] = "main"
    branch["meta_fields"][0]["display_name"] = "Squad"
    key = ("meta_field", "team", PRESENCE_FIELD)

    plan = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "theirs"})

    assert not plan.unresolved
    assert _values_write(plan, "meta_values") is None
    [write] = [op for op in plan.ops if op.kind == "write" and op.entity_type == "event"]
    assert write.fields == ("description",)


def test_main_deleting_a_field_is_no_conflict_on_an_event_the_branch_edited() -> None:
    base, main, branch = _sides()
    for side in (base, main, branch):
        side["events"][0]["field_values"] = [
            {"field_name": "name", "value": "x", "is_authored": True}
        ]
    base["event_types"][0]["field_definitions"].append(_field("fd-extra", "extra"))
    main["event_types"][0]["field_definitions"] = [_field("fd-name", "name")]
    branch["event_types"][0]["field_definitions"].append(_field("b-fd-extra", "extra"))
    base["events"][0]["field_values"].append(
        {"field_name": "extra", "value": "e", "is_authored": True}
    )
    branch["events"][0]["field_values"] = [
        {"field_name": "extra", "value": "e", "is_authored": True},
        {"field_name": "name", "value": "edited", "is_authored": True},
    ]

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert plan.rows == []
    assert _values_write(plan, "field_values") is None


def test_taking_main_for_a_field_the_branch_deleted_restores_its_values() -> None:
    base, main, branch = _sides()
    for side in (base, main, branch):
        side["events"][0]["field_values"] = [
            {"field_name": "name", "value": "x", "is_authored": True}
        ]
    main["event_types"][0]["field_definitions"][0]["description"] = "main"
    branch["event_types"][0]["field_definitions"] = []
    branch["relations"] = []
    branch["events"][0]["field_values"] = []
    key = ("field_definition", "track.name", PRESENCE_FIELD)

    plan = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "ours"})

    assert ("create", "field_definition") in {(op.kind, op.entity_type) for op in plan.ops}
    assert _values_write(plan, "field_values") == [
        {"field_name": "name", "value": "x", "is_authored": True}
    ]


def test_main_change_to_an_ambiguous_namesake_blocks_the_update() -> None:
    base, main, branch = _sides()
    base["events"].append(_event("ev-2", "purchase"))
    main["events"].append(_event("ev-2", "purchase"))
    branch["events"].append(_event("b-ev-2", "purchase"))
    for item in branch["events"]:
        item.pop("origin_id", None)
    main["events"][1]["description"] = "main"

    plan = plan_three_way(base, main, branch, origins_complete=False)

    assert [(b["kind"], b["entity_type"], b["name"]) for b in plan.blockers] == [
        ("ambiguous", "event", "track.purchase")
    ]


def test_untouched_ambiguous_namesakes_do_not_block() -> None:
    base, main, branch = _sides()
    base["events"].append(_event("ev-2", "purchase"))
    main["events"].append(_event("ev-2", "purchase"))
    branch["events"].append(_event("b-ev-2", "purchase"))
    for item in branch["events"]:
        item.pop("origin_id", None)
    main["variables"][0]["description"] = "main"

    plan = plan_three_way(base, main, branch, origins_complete=False)

    assert plan.blockers == []


@pytest.mark.parametrize(
    "case",
    ["main_rename_onto_branch_add", "both_add_same_source_name", "branch_rename_onto_main_add"],
)
def test_name_and_source_name_clashes_block_the_update(case: str) -> None:
    base, main, branch = _sides()
    extra = {**base["variables"][0], "description": ""}
    if case == "main_rename_onto_branch_add":
        main["variables"][0]["name"] = "money"
        branch["variables"].append({**extra, "id": "b-var-9", "name": "money", "source_name": "S9"})
    elif case == "both_add_same_source_name":
        main["variables"].append({**extra, "id": "var-8", "name": "c", "source_name": "S8"})
        branch["variables"].append({**extra, "id": "b-var-9", "name": "d", "source_name": "S8"})
    else:
        branch["variables"][0]["name"] = "x"
        main["variables"].append({**extra, "id": "var-8", "name": "x", "source_name": "S8"})

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert {(b["kind"], b["entity_type"]) for b in plan.blockers} == {
        ("identity_clash", "variable")
    }


def test_a_main_swap_of_source_names_is_no_clash() -> None:
    base, main, branch = _sides()
    second = {**base["variables"][0], "id": "var-2", "name": "amount", "source_name": "S2"}
    base["variables"].append(copy.deepcopy(second))
    main["variables"].append(copy.deepcopy(second))
    branch["variables"].append({**copy.deepcopy(second), "id": "b-var-2"})
    main["variables"][0]["name"], main["variables"][1]["name"] = "amount", "currency"

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert plan.blockers == []


def test_comment_order_on_main_is_no_branch_change_of_a_deleted_type() -> None:
    base, main, branch = _sides()
    photos = [
        {"kind": "upload", "storage_key": "a", "comments": [{"body": "z"}]},
        {"kind": "upload", "storage_key": "b", "comments": []},
    ]
    base["events"][0]["photos"] = copy.deepcopy(photos)
    # The branch never copies the thread, so its snapshot orders them apart.
    branch["events"][0]["photos"] = [
        {"kind": "upload", "storage_key": "b", "comments": []},
        {"kind": "upload", "storage_key": "a", "comments": []},
    ]
    main["event_types"] = [et for et in main["event_types"] if et["name"] != "track"]
    main["events"] = []
    main["relations"] = []

    plan = plan_three_way(base, main, branch, origins_complete=True)

    assert plan.rows == []
    assert ("delete", "event_type") in {(op.kind, op.entity_type) for op in plan.ops}


def test_a_long_event_name_stays_within_the_resolution_column() -> None:
    base, main, branch = _sides()
    long_name = "e" * 500
    for side in (base, main, branch):
        side["events"][0]["name"] = long_name
    main["events"][0]["description"] = "main"
    branch["events"][0]["description"] = "branch"

    [row] = plan_three_way(base, main, branch, origins_complete=True).rows
    assert len(row["name"]) <= 255
    assert row["label"] == long_name
    resolution = ResolutionCreate(
        entity_type="event",
        entity_name=row["name"],
        field_name="description",
        choice="ours",  # type: ignore[arg-type]
    )
    key = (resolution.entity_type, resolution.entity_name, resolution.field_name)
    plan = plan_three_way(base, main, branch, origins_complete=True, resolutions={key: "ours"})
    assert not plan.unresolved


def test_retoken_values_rewrites_in_one_pass_so_a_swap_does_not_fuse() -> None:
    from tripl.services._plan_branch_three_way import retoken_values

    values = [
        {"field_name": "a", "value": "${x}-${y}-${z}"},
        {"field_name": "b", "value": "plain"},
    ]
    out = retoken_values(values, {"x": "y", "y": "x"})

    assert [v["value"] for v in out] == ["${y}-${x}-${z}", "plain"]
    # The input is left as it was.
    assert values[0]["value"] == "${x}-${y}-${z}"
