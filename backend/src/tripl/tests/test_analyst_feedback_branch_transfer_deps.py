"""The pure half of moving or copying branch changes: closure, ops, refusals.

Snapshots are written by hand in ``build_plan_snapshot``'s shape: the base
with main ids (``m-…``), the source branch with its own (``s-…``) and the
target with its own (``t-…``), events naming main's row as ``origin_id``. The
diff entries come from ``compute_plan_diff_entries`` itself, so a unit is
built from exactly what the branch diff would list.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi import HTTPException

from tripl.schemas.plan_branch import BranchTransferEntryRef, BranchTransferRequest, PlanBranchDiff
from tripl.schemas.plan_revision import PlanDiffEntry
from tripl.services._plan_branch_transfer_deps import (
    Sides,
    Unit,
    build_units,
    plan_transfer,
    rename_write_fields,
    revert_order,
    transfer_closure,
)
from tripl.services.plan_branch_transfer_service import _selected_indices
from tripl.services.plan_revision_service import compute_plan_diff_entries, with_snapshot_defaults


def _fd(name: str, side: str = "m", **fields: Any) -> dict[str, Any]:
    return {
        "id": f"{side}-fd-{name}",
        "name": name,
        "display_name": name,
        "field_type": "string",
        "is_required": False,
        "enum_options": None,
        "description": None,
        "order": 0,
        "sensitivity": "none",
        "contract_required_max_null_rate": None,
        "contract_regex": None,
        "contract_min_value": None,
        "contract_max_value": None,
        "contract_max_bad_rate": 0.0,
        **fields,
    }


def _et(name: str, *fds: dict[str, Any], side: str = "m") -> dict[str, Any]:
    return {
        "id": f"{side}-et-{name}",
        "name": name,
        "display_name": name,
        "description": None,
        "color": None,
        "order": 0,
        "field_definitions": list(fds),
    }


def _ev(
    name: str,
    *,
    et: str = "track",
    side: str = "m",
    origin: str | None = None,
    values: dict[str, str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    item = {
        "id": f"{side}-ev-{name}",
        "event_type_id": f"{side}-et-{et}",
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
        "required_presence_threshold": None,
        "field_values": [
            {"field_name": key, "value": value, "is_authored": True}
            for key, value in sorted((values or {}).items())
        ],
        "meta_values": [],
        "tags": [],
        "photos": [],
        **fields,
    }
    if origin is not None:
        item["origin_id"] = origin
    return item


def _var(name: str, side: str = "m", **fields: Any) -> dict[str, Any]:
    return {
        "id": f"{side}-var-{name}",
        "name": name,
        "source_name": None,
        "variable_type": "string",
        "description": "",
        "allowed_values": [],
        "bindings": [],
        "excluded_from_scans": False,
        "json_schema": None,
        "event_value_overrides": [],
        **fields,
    }


def _mf(name: str, side: str = "m") -> dict[str, Any]:
    return {
        "id": f"{side}-mf-{name}",
        "name": name,
        "display_name": name,
        "field_type": "string",
        "is_required": False,
        "allow_multiple": False,
        "enum_options": None,
        "default_value": None,
        "link_template": None,
        "order": 0,
        "sensitivity": "none",
    }


def _plan(
    event_types: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    variables: list[dict[str, Any]] | None = None,
    meta_fields: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return with_snapshot_defaults(
        {
            "snapshot_version": 2,
            "event_types": event_types or [],
            "events": events or [],
            "variables": variables or [],
            "meta_fields": meta_fields or [],
            "relations": [],
        }
    )


def _copy(base: dict[str, Any], side: str) -> dict[str, Any]:
    """A branch's deep copy of ``base``: own ids, events naming main's as origin."""
    out = copy.deepcopy(base)
    for et in out["event_types"]:
        et["id"] = et["id"].replace("m-", f"{side}-", 1)
        for fd in et["field_definitions"]:
            fd["id"] = fd["id"].replace("m-", f"{side}-", 1)
    for event in out["events"]:
        event["origin_id"] = event["id"]
        event["id"] = event["id"].replace("m-", f"{side}-", 1)
    for variable in out["variables"]:
        variable["id"] = variable["id"].replace("m-", f"{side}-", 1)
    for meta in out["meta_fields"]:
        meta["id"] = meta["id"].replace("m-", f"{side}-", 1)
    return out


def _units(base: dict[str, Any], source: dict[str, Any], target: dict[str, Any], **sides: Any):
    entries = compute_plan_diff_entries(base, source, origins_complete=True)
    the_sides = Sides(source=source, base=base, target=target, target_name="TASK-2", **sides)
    return entries, build_units(entries, [], the_sides), the_sides


def _pick(units: list[Unit], entity_type: str, name: str) -> Unit:
    return next(u for u in units if u.entity_type == entity_type and u.entries[0].name == name)


def _names(pairs: list[tuple[Unit, str]]) -> dict[tuple[str, str], str]:
    return {(unit.entity_type, unit.entries[0].name): why for unit, why in pairs}


# --- closure ----------------------------------------------------------------------


def test_an_event_valued_for_an_added_field_carries_the_field_and_its_added_type() -> None:
    base = _plan()
    source = _plan(
        event_types=[_et("screen", _fd("title", side="s"), side="s")],
        events=[_ev("open", et="screen", side="s", values={"title": "Home"})],
    )
    _, units, _ = _units(base, source, _copy(base, "t"))
    closure = transfer_closure([_pick(units, "event", "open")], units, mode="copy")
    assert _names(closure.carried) == {
        ("field_definition", "title"): "open",
        ("event_type", "screen"): "open",
    }


def test_a_changed_field_a_moved_event_uses_is_carried() -> None:
    base = _plan(event_types=[_et("track", _fd("plan", field_type="string"))])
    source = _copy(base, "s")
    source["event_types"][0]["field_definitions"][0]["enum_options"] = ["free", "pro"]
    source["events"].append(_ev("buy", side="s", values={"plan": "pro"}))
    _, units, _ = _units(base, source, _copy(base, "t"))
    closure = transfer_closure([_pick(units, "event", "buy")], units, mode="copy")
    assert _names(closure.carried) == {("field_definition", "plan"): "buy"}


def test_a_token_carries_its_added_variable() -> None:
    base = _plan(event_types=[_et("track", _fd("name"))])
    source = _copy(base, "s")
    source["variables"].append(_var("currency", side="s"))
    source["events"].append(_ev("buy", side="s", values={"name": "${currency}"}))
    _, units, _ = _units(base, source, _copy(base, "t"))
    closure = transfer_closure([_pick(units, "event", "buy")], units, mode="copy")
    assert _names(closure.carried) == {("variable", "currency"): "buy"}


def test_a_successor_carries_its_added_event() -> None:
    base = _plan(event_types=[_et("track")])
    source = _copy(base, "s")
    source["events"] += [
        _ev("old", side="s", superseded_by="track.new"),
        _ev("new", side="s"),
    ]
    _, units, _ = _units(base, source, _copy(base, "t"))
    closure = transfer_closure([_pick(units, "event", "old")], units, mode="copy")
    assert _names(closure.carried) == {("event", "new"): "old"}


def test_a_move_takes_what_the_source_would_lose_and_a_copy_does_not() -> None:
    base = _plan(event_types=[_et("track", _fd("plan"))])
    source = _copy(base, "s")
    source["event_types"].append(_et("screen", _fd("title", side="s"), side="s"))
    source["events"] += [
        _ev("open", et="screen", side="s"),
        _ev("close", et="screen", side="s"),
        _ev("buy", side="s", values={"plan": "pro"}),
    ]
    source["event_types"][0]["field_definitions"][0]["description"] = "Plan tier"
    _, units, _ = _units(base, source, _copy(base, "t"))

    moved_type = transfer_closure([_pick(units, "event_type", "screen")], units, mode="move")
    assert set(_names(moved_type.carried)) == {
        ("event", "open"),
        ("event", "close"),
        ("field_definition", "title"),
    }
    copied_type = transfer_closure([_pick(units, "event_type", "screen")], units, mode="copy")
    assert copied_type.carried == []

    # A moved CHANGED field pulls the remaining events that hold a value for it.
    moved_field = transfer_closure([_pick(units, "field_definition", "plan")], units, mode="move")
    assert _names(moved_field.carried) == {("event", "buy"): "plan"}
    copied_field = transfer_closure([_pick(units, "field_definition", "plan")], units, mode="copy")
    assert copied_field.carried == []


def test_a_move_of_an_added_event_takes_the_variable_overriding_it_and_its_predecessor() -> None:
    base = _plan(event_types=[_et("track")], variables=[_var("currency")])
    source = _copy(base, "s")
    source["events"] += [
        _ev("new", side="s"),
        _ev("old", side="s", superseded_by="track.new"),
    ]
    source["variables"][0]["event_value_overrides"] = [
        {"event_type_name": "track", "event_name": "new", "values": ["EUR"], "required": False}
    ]
    _, units, _ = _units(base, source, _copy(base, "t"))
    closure = transfer_closure([_pick(units, "event", "new")], units, mode="move")
    assert set(_names(closure.carried)) == {("event", "old"), ("variable", "currency")}


# --- refusals -----------------------------------------------------------------------


@pytest.mark.parametrize("entity_type", ["event_type", "field_definition", "meta_field"])
def test_a_removed_definition_is_not_transferable(entity_type: str) -> None:
    base = _plan(event_types=[_et("track", _fd("plan"))], meta_fields=[_mf("team")])
    source = _copy(base, "s")
    if entity_type == "event_type":
        source["event_types"] = []
    elif entity_type == "field_definition":
        source["event_types"][0]["field_definitions"] = []
    else:
        source["meta_fields"] = []
    _, units, sides = _units(base, source, _copy(base, "t"))
    removed = next(u for u in units if u.entity_type == entity_type and u.kind == "removed")
    plan = plan_transfer([removed], units, sides, mode="copy")
    assert plan.ops == []
    assert plan.not_transferable and "directly" in plan.not_transferable[0]


def test_a_parent_the_target_deleted_is_target_missing() -> None:
    base = _plan(event_types=[_et("track")])
    source = _copy(base, "s")
    source["events"].append(_ev("buy", side="s"))
    target = _copy(base, "t")
    target["event_types"] = []
    _, units, sides = _units(base, source, target)
    plan = plan_transfer([_pick(units, "event", "buy")], units, sides, mode="copy")
    assert [c["reason"] for c in plan.conflicts] == ["target_missing"]
    assert "event type 'track'" in plan.conflicts[0]["message"]
    assert plan.ops == []


def test_a_housekeeping_row_is_refused() -> None:
    entry = PlanDiffEntry(
        entity_type="variable", kind="removed", name="legacy", housekeeping="Already gone on main"
    )
    diff = PlanBranchDiff(entries=[entry], summary={}, behind_base=False)
    request = BranchTransferRequest(
        target_branch_id=None,
        mode="copy",
        dry_run=True,
        entries=[BranchTransferEntryRef(entity_type="variable", name="legacy")],
    )
    with pytest.raises(HTTPException) as refused:
        _selected_indices(diff, request)
    assert refused.value.status_code == 400


# --- pairing and preconditions ---------------------------------------------------------


def test_an_addition_the_target_holds_alike_is_skipped_and_otherwise_refused() -> None:
    base = _plan()
    source = _plan(variables=[_var("currency", side="s", description="ISO code")])
    same = _plan(variables=[_var("currency", side="t", description="ISO code")])
    _, units, sides = _units(base, source, same)
    plan = plan_transfer([_pick(units, "variable", "currency")], units, sides, mode="copy")
    assert plan.ops == [] and plan.conflicts == []
    assert [reason for _, reason in plan.skipped] == ["already on 'TASK-2'"]

    other = _plan(variables=[_var("currency", side="t", description="Something else")])
    _, units, sides = _units(base, source, other)
    plan = plan_transfer([_pick(units, "variable", "currency")], units, sides, mode="copy")
    assert [c["reason"] for c in plan.conflicts] == ["target_exists"]


def test_an_equal_carried_parent_is_skipped_too() -> None:
    base = _plan()
    source = _plan(
        event_types=[_et("screen", side="s")], events=[_ev("open", et="screen", side="s")]
    )
    target = _plan(event_types=[_et("screen", side="t")])
    _, units, sides = _units(base, source, target)
    plan = plan_transfer([_pick(units, "event", "open")], units, sides, mode="copy")
    assert [(u.entity_type, why) for u, why in plan.skipped] == [
        ("event_type", "already on 'TASK-2'")
    ]
    assert [op.kind for op in plan.ops] == ["create"]


def test_the_precondition_reads_the_base_snapshot_and_ignores_order() -> None:
    base = _plan(event_types=[_et("track")], events=[_ev("buy", description="old")])
    source = _copy(base, "s")
    source["events"][0]["description"] = "new"
    target = _copy(base, "t")
    target["events"][0]["order"] = 7
    _, units, sides = _units(base, source, target)
    plan = plan_transfer([_pick(units, "event", "buy")], units, sides, mode="copy")
    assert plan.conflicts == []
    (op,) = plan.ops
    assert op.kind == "write" and op.fields == ("description",)
    assert op.branch is not None and op.branch["id"] == "t-ev-buy"

    target["events"][0]["description"] = "theirs"
    _, units, sides = _units(base, source, target)
    plan = plan_transfer([_pick(units, "event", "buy")], units, sides, mode="copy")
    (conflict,) = plan.conflicts
    assert conflict["reason"] == "target_changed" and conflict["field"] == "description"
    assert '"theirs"' in conflict["message"] and '"old"' in conflict["message"]


def test_a_value_change_the_target_already_made_is_skipped() -> None:
    base = _plan(event_types=[_et("track")], events=[_ev("buy", description="old")])
    source = _copy(base, "s")
    source["events"][0]["description"] = "new"
    target = _copy(base, "t")
    target["events"][0]["description"] = "new"
    _, units, sides = _units(base, source, target)
    plan = plan_transfer([_pick(units, "event", "buy")], units, sides, mode="copy")
    assert plan.ops == [] and plan.conflicts == []
    assert len(plan.skipped) == 1


def test_pairing_falls_back_by_name_when_only_the_source_is_legacy() -> None:
    base = _plan(event_types=[_et("track")], events=[_ev("buy", description="old")])
    source = _copy(base, "s")
    source["events"][0]["description"] = "new"
    target = _copy(base, "t")
    del target["events"][0]["origin_id"]
    _, units, sides = _units(base, source, target, source_origins_complete=False)
    plan = plan_transfer([_pick(units, "event", "buy")], units, sides, mode="copy")
    assert [op.kind for op in plan.ops] == ["write"]

    _, units, sides = _units(base, source, target)
    plan = plan_transfer([_pick(units, "event", "buy")], units, sides, mode="copy")
    assert [c["reason"] for c in plan.conflicts] == ["target_missing"]


def test_a_removed_event_pairs_by_origin_despite_a_namesake() -> None:
    base = _plan(event_types=[_et("track")], events=[_ev("buy")])
    source = _copy(base, "s")
    source["events"] = []
    target = _copy(base, "t")
    target["events"].append(_ev("buy", side="t2"))
    _, units, sides = _units(base, source, target)
    plan = plan_transfer([units[0]], units, sides, mode="move")
    (op,) = plan.ops
    assert op.kind == "delete" and op.branch is not None and op.branch["id"] == "t-ev-buy"


def test_rename_write_fields_are_the_name_and_every_other_edit() -> None:
    old = _var("currency", description="ISO")
    new = _var("currency_code", side="s", description="ISO 4217")
    assert rename_write_fields(new, old) == ["name", "description"]
    assert rename_write_fields(_var("b", side="s"), _var("a"), "variable") == ["name"]


def test_revert_order_undoes_children_first_then_changes_then_removals() -> None:
    def unit(entity_type: Any, kind: Any, uid: int) -> Unit:
        entry = PlanDiffEntry(entity_type=entity_type, kind=kind, name=f"{entity_type}{uid}")
        return Unit(
            uid=(uid,), entity_type=entity_type, kind=kind, entries=(entry,), item={}, base={}
        )

    units = [
        unit("event_type", "added", 1),
        unit("variable", "removed", 2),
        unit("field_definition", "added", 3),
        unit("event", "changed", 4),
        unit("event", "added", 5),
        unit("relation", "added", 6),
        unit("event", "removed", 7),
    ]
    assert [u.uid[0] for u in revert_order(units)] == [6, 5, 3, 1, 4, 7, 2]
