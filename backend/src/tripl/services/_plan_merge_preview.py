"""One event as main will hold it after the merge: a pure projection, no I/O.

A reviewer reads a branch as field-level rows — the event's own attributes in
its row, its properties in the variables' ``event_value_overrides`` rows — and
"Show full event" shows the branch copy, which is stale once main has moved.
This puts the pieces back together the way the merge would.

Two sources of truth, and the parity test in
``tests/test_analyst_feedback_merge_preview.py`` pins this module to both:

* **Whether it merges** is ``merge_blocking_conflicts``, the merge's own gate,
  passed in as ``blocking``. Nothing here decides a conflict on its own; the
  per-key ``conflicting_fields`` only says WHICH keys of a gated event clash.
* **What it merges to** re-states ``_apply_merge``'s apply rules for one
  event: rows paired by ``merge_slots`` with the merge's own arguments, each
  change key taken whole from the side that changed it (``main_keeps`` first,
  for catalog order), the status arm's rank rule, the rename condition, the
  successor pointer resolved through the landings, and each variable's
  override list taken whole from the side that changed it, after the
  variables' own ``pair_renames``. A change to either side of the merge must
  change this module too; the parity test fails when they drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

from tripl.models.event import EventStatus, event_status_rank
from tripl.schemas.plan_branch import (
    MergedEventPreview,
    MergedProperty,
    MergedPropertyValue,
    MergedState,
    MergedValue,
)
from tripl.services._plan_branch_renames import pair_renames, rekey_in_place
from tripl.services._plan_merge_slots import merge_slots
from tripl.services.plan_branch_conflicts import (
    _EV_CHANGE_KEYS,
    _VAR_CHANGE_KEYS,
    _decided_keys,
    comparable_field,
    conflicting_fields,
    main_keeps,
)

Snapshot = dict[str, Any]
EventKey = tuple[str, str]
Outcome = Literal["added", "changed", "unchanged", "removed", "skipped"]

# The attributes shown, in the order the event page shows them. ``order`` and
# ``photos`` are applied by the merge but not shown: notes name them when they
# change or clash, so a blocked event never looks clean.
_SHOWN_ATTRIBUTES = (
    "title",
    "description",
    "status",
    "source_name",
    "owner_id",
    "reviewed",
    "sunset_at",
    "superseded_by",
    "metric_breakdown_columns",
    "required_presence_threshold",
)
_VARIABLE_ATTRIBUTES = ("variable_type", "json_schema", "allowed_values", "description")
# A gated variable's clash on one of these keys changes what the event's
# property holds; a clash on any other key (its description, say) leaves the
# property as projected and marks only the variable.
_PROPERTY_KEYS = frozenset({"event_value_overrides", "allowed_values", "variable_type"})


class EventNotFound(LookupError):
    """No side of the merge holds the event asked for."""


class AmbiguousEvent(LookupError):
    """The event is one of several namesakes the merge cannot tell apart."""


@dataclass(frozen=True)
class EventTarget:
    """The event to project: by any side's id, or by ``(type, name)``."""

    id: str | None = None
    key: EventKey | None = None


class MergedEventProjection(NamedTuple):
    preview: MergedEventPreview
    # The rows of ``blocking`` this event accounts for, for the branch-wide
    # "N other conflicts" count.
    matched_blocking: list[dict[str, Any]]


@dataclass
class _View:
    """One event as each side holds it, paired the way the merge pairs it."""

    base: Snapshot | None
    main: Snapshot | None
    branch: Snapshot | None
    branch_known: bool = True
    # Placed by ``merge_slots`` (a slot or a created row), not main's alone.
    placed: bool = True
    sides: list[Snapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.sides = [side for side in (self.branch, self.main, self.base) if side is not None]


def _event_key(event: Snapshot) -> EventKey:
    return (event["event_type_name"], event["name"])


def _display(event: Snapshot) -> str:
    return f"{event['event_type_name']}.{event['name']}"


def _views(
    base: Snapshot, main: Snapshot, branch: Snapshot, *, origins_complete: bool
) -> tuple[list[_View], list[Snapshot]]:
    """Every event the merge sees, and the branch rows it leaves unplaced."""
    main_events = list(main.get("events", []))
    branch_events = list(branch.get("events", []))
    slots = merge_slots(
        list(base.get("events", [])),
        main_events,
        branch_events,
        base_key=_event_key,
        main_key=_event_key,
        branch_key=_event_key,
        main_ref=lambda event: event["id"],
        branch_ref=lambda event: event.get("origin_id") or event["id"],
        follows_rename=lambda old_key, new_key: old_key[0] == new_key[0],
        branch_origins_complete=origins_complete,
        identities=(
            lambda event: event.get("source_name"),
            lambda event: event.get("source_name"),
            lambda event: event.get("source_name"),
        ),
    )
    views = [
        _View(base=slot.base, main=slot.main, branch=slot.branch, branch_known=slot.branch_known)
        for slot in slots.slots
    ]
    views.extend(_View(base=None, main=None, branch=row) for row in slots.created)
    placed_main = {id(view.main) for view in views if view.main is not None}
    placed_branch = {id(view.branch) for view in views if view.branch is not None}
    # Main rows added after the cut that no branch row matched: the merge
    # leaves them alone, so they are on main afterwards exactly as they are.
    views.extend(
        _View(base=None, main=row, branch=None, placed=False)
        for row in main_events
        if id(row) not in placed_main
    )
    unplaced = [row for row in branch_events if id(row) not in placed_branch]
    return views, unplaced


def _resolve(views: list[_View], unplaced: list[Snapshot], target: EventTarget) -> _View:
    if target.id is not None:

        def match(side: Snapshot | None) -> bool:
            return side is not None and str(side.get("id")) == target.id

        unplaced_hit = any(str(row.get("id")) == target.id for row in unplaced)
    else:
        assert target.key is not None
        key = target.key

        def match(side: Snapshot | None) -> bool:
            return side is not None and _event_key(side) == key

        unplaced_hit = any(_event_key(row) == key for row in unplaced)

    for side in ("branch", "base", "main"):
        hits = [view for view in views if match(getattr(view, side))]
        if side == "branch" and unplaced_hit:
            raise AmbiguousEvent(
                "This event is one of several with the same type and name, and the "
                "merge cannot tell which main event it lands on. Rename one of them "
                "to see it as merged."
            )
        if len(hits) > 1:
            raise AmbiguousEvent(
                "Several events share this type and name, so this view cannot tell "
                "them apart. Open it from the event's own row in the diff."
            )
        if hits:
            view = hits[0]
            if not view.branch_known:
                raise AmbiguousEvent(
                    "This event is one of several with the same type and name on a "
                    "branch opened before origin ids, and the merge leaves it as it "
                    "is on main without telling which copy is which."
                )
            return view
    raise EventNotFound("Event not found on this branch, its base or main")


def _types_after_merge(base: Snapshot, main: Snapshot, branch: Snapshot) -> set[str]:
    """The event-type names main holds once the merge's type arm has run."""
    base_types = {et["name"] for et in base.get("event_types", [])}
    main_types = {et["name"] for et in main.get("event_types", [])}
    branch_types = {et["name"] for et in branch.get("event_types", [])}
    kept = {name for name in main_types if not (name in base_types and name not in branch_types)}
    added = {name for name in branch_types if name not in main_types and name not in base_types}
    return kept | added


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _state(value: Any, previous: Any) -> MergedState:
    if value == previous:
        return "unchanged"
    if _empty(previous):
        return "added"
    if _empty(value):
        return "removed"
    return "changed"


def _status_after(
    base_event: Snapshot | None, main_event: Snapshot, branch_event: Snapshot
) -> tuple[str, bool]:
    """``_apply_merge``'s status arm: (status on main, whether the branch's was dropped)."""
    # Every snapshot event carries a status: the event column is NOT NULL.
    branch_status = str(branch_event["status"])
    main_status = str(main_event["status"])
    if base_event is not None and branch_status == base_event.get("status"):
        return main_status, False
    b_status = EventStatus(branch_status)
    m_status = EventStatus(main_status)
    if b_status == EventStatus.archived:
        return main_status, branch_status != main_status
    if event_status_rank(b_status) >= event_status_rank(m_status):
        return branch_status, False
    return main_status, True


def _taken(
    key: str, base_event: Snapshot | None, main_event: Snapshot, branch_event: Snapshot
) -> Any:
    """The value ``_apply_merge`` leaves on main for one change key."""
    main_value = main_event.get(key)
    if main_keeps(key, base_event, main_value):
        return main_value
    if base_event is None or branch_event.get(key) != base_event.get(key):
        return branch_event.get(key)
    return main_value


def _main_moved(
    key: str,
    base_event: Snapshot | None,
    main_event: Snapshot | None,
    branch_event: Snapshot | None,
) -> bool:
    if base_event is None or main_event is None or branch_event is None:
        return False
    return main_event.get(key) != base_event.get(key) and branch_event.get(key) == base_event.get(
        key
    )


# --- collections ---------------------------------------------------------


def _members(key: str, value: Any) -> dict[str, Any]:
    """A collection-valued key as ``member -> value``, for display only."""
    items = value or []
    if key == "tags":
        return {str(tag): str(tag) for tag in items}
    if key == "field_values":
        return {str(item.get("field_name")): item.get("value") for item in items}
    grouped: dict[str, list[Any]] = {}
    for item in items:
        grouped.setdefault(str(item.get("meta_field_name")), []).append(item.get("value"))
    return {
        name: values[0] if len(values) == 1 else sorted(values, key=str)
        for name, values in grouped.items()
    }


def _member_rows(
    key: str,
    merged: Any,
    main_value: Any,
    *,
    conflict: bool,
    branch_value: Any,
    base_value: Any,
    main_moved: bool,
) -> list[MergedValue]:
    if conflict:
        main_members = _members(key, main_value)
        branch_members = _members(key, branch_value)
        return [
            MergedValue(
                key=member,
                value=None,
                previous=main_members.get(member),
                branch_value=branch_members.get(member),
                state="conflict",
            )
            for member in sorted({*main_members, *branch_members})
        ]
    after = _members(key, merged)
    before = _members(key, main_value)
    base_members = _members(key, base_value) if main_moved else {}
    return [
        MergedValue(
            key=member,
            value=after.get(member),
            previous=before.get(member),
            state=_state(after.get(member), before.get(member)),
            main_moved=main_moved and before.get(member) != base_members.get(member),
        )
        for member in sorted({*after, *before})
    ]


# --- properties ----------------------------------------------------------


def _var_attrs(variable: Snapshot | None) -> dict[str, Any] | None:
    if variable is None:
        return None
    return {attr: variable.get(attr) for attr in _VARIABLE_ATTRIBUTES}


def _entry_for(variable: Snapshot | None, event_key: EventKey | None) -> Snapshot | None:
    """``variable``'s override of the event, looked up by that side's own key."""
    if variable is None or event_key is None:
        return None
    entries = [
        entry
        for entry in variable.get("event_value_overrides") or []
        if isinstance(entry, dict)
        and (entry.get("event_type_name"), entry.get("event_name")) == event_key
    ]
    if len(entries) > 1:
        raise AmbiguousEvent(
            "Several events share this type and name, so their properties cannot "
            "be told apart. Rename one of them to see it as merged."
        )
    return entries[0] if entries else None


def _effective(entry: Snapshot | None, attrs: dict[str, Any] | None) -> list[str] | None:
    if entry is None:
        return None
    if entry.get("values") is not None:
        return [str(value) for value in entry["values"]]
    return [str(value) for value in (attrs or {}).get("allowed_values") or []]


def _value_rows(after: list[str] | None, before: list[str] | None) -> list[MergedPropertyValue]:
    after_list = after or []
    before_list = before or []
    rows = [
        MergedPropertyValue(
            value=value,
            state="unchanged" if before is not None and value in before_list else "added",
        )
        for value in after_list
    ]
    rows.extend(
        MergedPropertyValue(value=value, state="removed")
        for value in before_list
        if value not in after_list
    )
    return rows


class _VariableSides(NamedTuple):
    base: Snapshot | None
    main: Snapshot | None
    branch: Snapshot | None
    # What main holds after the merge: None when the variable is gone.
    attrs: dict[str, Any] | None
    overrides_from: str  # "branch" | "main" | "none"
    names: set[str]


def _variable_sides(base: Snapshot, main: Snapshot, branch: Snapshot) -> dict[str, _VariableSides]:
    """Each variable after ``_apply_merge``'s variable arm, keyed by its merged name."""
    base_by = {v["name"]: v for v in base.get("variables", [])}
    main_by = {v["name"]: v for v in main.get("variables", [])}
    branch_by = {v["name"]: v for v in branch.get("variables", [])}
    original_main_name = {id(v): name for name, v in main_by.items()}
    original_base_name = {id(v): name for name, v in base_by.items()}
    renames = {
        old_key[0]: new_key[0]
        for old_key, new_key in pair_renames(
            {(name,): v.get("source_name") for name, v in base_by.items()},
            {(name,): v.get("source_name") for name, v in main_by.items()},
            {(name,): v.get("source_name") for name, v in branch_by.items()},
            vacate_removed=True,
        ).items()
    }
    main_re = dict(main_by)
    base_re = dict(base_by)
    for new_name in renames.values():
        if new_name in main_re and new_name not in renames:
            main_re.pop(new_name)
    if renames:
        rekey_in_place(main_re, renames)
        rekey_in_place(base_re, renames)

    out: dict[str, _VariableSides] = {}
    for name in {*branch_by, *main_re, *base_re}:
        b_v = branch_by.get(name)
        m_v = main_re.get(name)
        base_v = base_re.get(name)
        names = {name}
        if m_v is not None:
            names.add(original_main_name[id(m_v)])
        if base_v is not None:
            names.add(original_base_name[id(base_v)])
        if b_v is not None and m_v is not None:
            attrs = {
                attr: b_v.get(attr)
                if base_v is None or b_v.get(attr) != base_v.get(attr)
                else m_v.get(attr)
                for attr in _VARIABLE_ATTRIBUTES
            }
            changed = base_v is None or b_v.get("event_value_overrides") != base_v.get(
                "event_value_overrides"
            )
            out[name] = _VariableSides(
                base_v, m_v, b_v, attrs, "branch" if changed else "main", names
            )
        elif b_v is not None:
            # Main deleted it after the cut: main's deletion stands. Else new.
            if base_v is not None:
                out[name] = _VariableSides(base_v, None, b_v, None, "none", names)
            else:
                out[name] = _VariableSides(None, None, b_v, _var_attrs(b_v), "branch", names)
        elif m_v is not None:
            if base_v is not None:
                # The branch removed it: the merge deletes main's row.
                out[name] = _VariableSides(base_v, m_v, None, None, "none", names)
            else:
                out[name] = _VariableSides(None, m_v, None, _var_attrs(m_v), "main", names)
    return out


# --- the projection ------------------------------------------------------


def project_merged_event(
    base: Snapshot,
    main: Snapshot,
    branch: Snapshot,
    *,
    target: EventTarget,
    origins_complete: bool,
    blocking: list[dict[str, Any]],
) -> MergedEventProjection:
    """``target`` as main holds it after merging ``branch`` — see the module docstring.

    ``base`` must be a complete snapshot (``require_complete_base``) and
    ``blocking`` the merge's own ``merge_blocking_conflicts`` for these sides.
    Raises ``EventNotFound`` and ``AmbiguousEvent``.
    """
    views, unplaced = _views(base, main, branch, origins_complete=origins_complete)
    view = _resolve(views, unplaced, target)
    base_ev, main_ev, branch_ev = view.base, view.main, view.branch
    types_after = _types_after_merge(base, main, branch)
    notes: list[str] = []

    reference = branch_ev or main_ev or base_ev
    assert reference is not None
    type_name = reference["event_type_name"]

    outcome: Outcome
    if branch_ev is not None and main_ev is None and base_ev is None:
        outcome = "added" if type_name in types_after else "skipped"
        if outcome == "skipped":
            notes.append("This event's type is not on main; the merge will not create it.")
    elif main_ev is None:
        outcome = "removed"
        notes.append(
            "Main deleted this event after the branch was cut; the merge leaves it deleted."
        )
    elif branch_ev is None and base_ev is not None and view.placed:
        outcome = "removed"
        notes.append("This branch deletes the event; the merge removes it from main.")
    elif type_name not in types_after:
        outcome = "removed"
        notes.append("This branch deletes the event's type, and the event goes with it.")
    else:
        outcome = "changed"  # settled below against main's values

    # --- the merge's gate, filtered to this event
    event_names = {_display(side) for side in view.sides}
    field_names = {
        f"{type_name}.{value.get('field_name')}"
        for side in view.sides
        for value in side.get("field_values") or []
    }
    meta_names = {
        str(value.get("meta_field_name"))
        for side in view.sides
        for value in side.get("meta_values") or []
    }
    variables = _variable_sides(base, main, branch)
    side_keys = {
        "base": _event_key(base_ev) if base_ev is not None else None,
        "main": _event_key(main_ev) if main_ev is not None else None,
        "branch": _event_key(branch_ev) if branch_ev is not None else None,
    }

    def holds_event(var: _VariableSides) -> bool:
        return any(
            _entry_for(variable, side_keys[side]) is not None
            for side, variable in (("base", var.base), ("main", var.main), ("branch", var.branch))
        )

    own_variables = {name: var for name, var in variables.items() if holds_event(var)}
    variable_names = {n for var in own_variables.values() for n in var.names}

    def is_own(row: dict[str, Any]) -> bool:
        kind, name = row.get("entity_type"), row.get("name")
        return (
            (kind == "event" and name in event_names)
            or (kind == "event_type" and name == type_name)
            or (kind == "field_definition" and name in field_names)
            or (kind == "meta_field" and name in meta_names)
            or (kind == "variable" and name in variable_names)
        )

    matched = [row for row in blocking if is_own(row)]
    event_blocked = any(
        row.get("entity_type") == "event" and row.get("name") in event_names for row in matched
    )
    for row in matched:
        if row.get("entity_type") == "event_type":
            notes.append(
                f"The event type '{type_name}' conflicts with main (deleted on one side, "
                "changed on the other); the merge refuses until it is settled."
            )
        elif row.get("entity_type") == "field_definition":
            notes.append(
                f"The field '{row['name']}' this event fills conflicts with main; "
                "the merge refuses until it is settled."
            )
        elif row.get("entity_type") == "meta_field":
            notes.append(
                f"The meta field '{row['name']}' this event fills conflicts with main; "
                "the merge refuses until it is settled."
            )

    conflict_keys: set[str] = set()
    presence_conflict = False
    if event_blocked and main_ev is not None and branch_ev is not None:
        if base_ev is not None:
            conflict_keys = set(conflicting_fields(base_ev, main_ev, branch_ev, _EV_CHANGE_KEYS))
        else:
            # Added on both sides (``merge_slots`` paired the two new rows):
            # the gate compares the copies on the keys a choice is asked for.
            conflict_keys = {
                key
                for key in _decided_keys(_EV_CHANGE_KEYS)
                if comparable_field(main_ev, key) != comparable_field(branch_ev, key)
            }
            notes.append(
                "Added on both sides with different values; the merge refuses until "
                "they are settled."
            )
    elif event_blocked:
        presence_conflict = True
        notes.append("Deleted on one side and changed on the other; the merge refuses.")
    if "photos" in conflict_keys:
        notes.append("Photos changed on both sides; the merge refuses until that is settled.")

    # --- name
    name = reference["name"]
    previous_name: str | None = None
    if main_ev is not None and branch_ev is not None:
        renamed = (
            base_ev is not None
            and _event_key(branch_ev) != _event_key(base_ev)
            and _event_key(main_ev) == _event_key(base_ev)
        )
        name = branch_ev["name"] if renamed else main_ev["name"]
        if name != main_ev["name"]:
            previous_name = main_ev["name"]
    elif main_ev is not None:
        name = main_ev["name"]

    # --- attributes and collections
    attributes: list[MergedValue] = []
    collections: dict[str, list[MergedValue]] = {"field_values": [], "meta_values": [], "tags": []}
    differs = previous_name is not None
    if outcome in ("removed", "skipped") or presence_conflict:
        shown = main_ev or branch_ev or base_ev
        assert shown is not None
        state: MergedState = "conflict" if presence_conflict else "removed"
        for key in _SHOWN_ATTRIBUTES:
            attributes.append(
                MergedValue(
                    key=key,
                    value=None,
                    previous=main_ev.get(key) if main_ev is not None else None,
                    branch_value=branch_ev.get(key) if branch_ev is not None else None,
                    state=state,
                )
            )
        for key in collections:
            if presence_conflict:
                # Both sides, the way a both-sides conflict shows them.
                collections[key] = _member_rows(
                    key,
                    None,
                    main_ev.get(key) if main_ev is not None else None,
                    conflict=True,
                    branch_value=branch_ev.get(key) if branch_ev is not None else None,
                    base_value=None,
                    main_moved=False,
                )
                continue
            collections[key] = [
                MergedValue(
                    key=member,
                    value=None,
                    previous=value if main_ev is not None else None,
                    state=state,
                )
                for member, value in sorted(_members(key, shown.get(key)).items())
            ]
    elif main_ev is None or branch_ev is None:
        # Added on the branch, or main's alone: the row lands as that side has it.
        only = branch_ev if main_ev is None else main_ev
        assert only is not None
        for key in _SHOWN_ATTRIBUTES:
            value = only.get(key)
            attributes.append(
                MergedValue(
                    key=key,
                    value=value,
                    previous=None if main_ev is None else value,
                    state=("unchanged" if _empty(value) else "added")
                    if main_ev is None
                    else "unchanged",
                )
            )
        for key in collections:
            collections[key] = _member_rows(
                key,
                only.get(key),
                None if main_ev is None else only.get(key),
                conflict=False,
                branch_value=None,
                base_value=None,
                main_moved=False,
            )
        if main_ev is None:
            differs = True
    else:
        for key in _SHOWN_ATTRIBUTES:
            previous = main_ev.get(key)
            note: str | None = None
            if key in conflict_keys:
                attributes.append(
                    MergedValue(
                        key=key,
                        value=None,
                        previous=previous,
                        branch_value=branch_ev.get(key),
                        state="conflict",
                    )
                )
                continue
            if key == "status":
                value, dropped = _status_after(base_ev, main_ev, branch_ev)
                if dropped:
                    note = (
                        "The merge does not carry an archive to main."
                        if branch_ev.get("status") == EventStatus.archived.value
                        else "The merge does not carry a lower status to main."
                    )
                attributes.append(
                    MergedValue(
                        key=key,
                        value=value,
                        previous=previous,
                        branch_value=branch_ev.get(key) if dropped else None,
                        state="unchanged" if dropped else _state(value, previous),
                        main_moved=_main_moved(key, base_ev, main_ev, branch_ev),
                        note=note,
                    )
                )
                if note:
                    notes.append(note)
                differs = differs or value != previous
                continue
            if key == "superseded_by":
                value, note = _successor_after(views, base_ev, main_ev, branch_ev, types_after)
                if note:
                    notes.append(note)
            else:
                value = _taken(key, base_ev, main_ev, branch_ev)
            differs = differs or value != previous
            attributes.append(
                MergedValue(
                    key=key,
                    value=value,
                    previous=previous,
                    state=_state(value, previous),
                    main_moved=_main_moved(key, base_ev, main_ev, branch_ev),
                    note=note,
                )
            )
        for key in collections:
            conflict = key in conflict_keys
            merged = None if conflict else _taken(key, base_ev, main_ev, branch_ev)
            main_moved = _main_moved(key, base_ev, main_ev, branch_ev)
            collections[key] = _member_rows(
                key,
                merged,
                main_ev.get(key),
                conflict=conflict,
                branch_value=branch_ev.get(key),
                base_value=base_ev.get(key) if base_ev is not None else None,
                main_moved=main_moved,
            )
            differs = differs or (not conflict and merged != main_ev.get(key))
        if _taken("order", base_ev, main_ev, branch_ev) != main_ev.get("order"):
            differs = True
            notes.append("The merge also moves this event's display order.")
        if "photos" not in conflict_keys and comparable_field(
            {"photos": _taken("photos", base_ev, main_ev, branch_ev)}, "photos"
        ) != comparable_field(main_ev, "photos"):
            differs = True
            notes.append("The merge changes this event's photos; this view does not show them.")

    # --- properties
    lands_on_main = outcome not in ("removed", "skipped") and not presence_conflict
    properties: list[MergedProperty] = []
    if lands_on_main:
        blocked_variable_names = {
            row["name"] for row in matched if row.get("entity_type") == "variable"
        }
        for var_name, var in sorted(own_variables.items()):
            gated = bool(var.names & blocked_variable_names)
            clash = _variable_clash(var) if gated else set()
            # A clash on presence, on no named key, or on the property's own
            # keys makes the whole property a conflict.
            whole = gated and (not clash or bool(clash & _PROPERTY_KEYS))
            prop = _property(var_name, var, side_keys, blocked=whole)
            if prop is not None and clash and not whole:
                prop = prop.model_copy(update={"variable_state": "conflict"})
                notes.append(
                    f"The variable '{var_name}' conflicts with main on "
                    f"{', '.join(sorted(clash))}; the merge refuses until it is settled."
                )
            if prop is not None:
                properties.append(prop)
                differs = differs or prop.state != "unchanged" or prop.variable_state != "unchanged"

    if outcome == "changed" and not differs and not conflict_keys:
        outcome = "unchanged"
    blocked = bool(matched)
    explained = (
        bool(conflict_keys)
        or presence_conflict
        or any(prop.state == "conflict" for prop in properties)
        or any(row.get("entity_type") != "event" for row in matched)
    )
    if blocked and not explained:
        # The gate refuses on something no row above shows — say so, so a
        # blocked event never reads as clean.
        notes.append("This event conflicts with main; the merge refuses until it is settled.")

    ref = branch_ev or main_ev or base_ev
    assert ref is not None
    preview = MergedEventPreview(
        event_id=branch_ev["id"] if branch_ev is not None else None,
        main_event_id=main_ev["id"] if main_ev is not None else None,
        ref_id=str(ref["id"]),
        event_type_name=type_name,
        name=name,
        previous_name=previous_name,
        outcome=outcome,
        blocked=blocked,
        notes=_unique(notes),
        attributes=attributes,
        field_values=collections["field_values"],
        meta_values=collections["meta_values"],
        tags=collections["tags"],
        properties=properties,
    )
    return MergedEventProjection(preview=preview, matched_blocking=matched)


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _successor_after(
    views: list[_View],
    base_ev: Snapshot | None,
    main_ev: Snapshot,
    branch_ev: Snapshot,
    types_after: set[str],
) -> tuple[Any, str | None]:
    """``_apply_merge``'s successor pass: the pointer re-resolved through the landings."""
    if base_ev is not None and branch_ev.get("superseded_by") == base_ev.get("superseded_by"):
        return main_ev.get("superseded_by"), None
    pointer = branch_ev.get("superseded_by")
    if pointer is None:
        return None, None
    landed = [
        view
        for view in views
        if view.branch is not None
        and _display(view.branch) == pointer
        and view.branch["event_type_name"] in types_after
        and (view.main is not None or view.base is None)
    ]
    if not landed:
        return None, (
            f"The replacement '{pointer}' does not land on main, so the merge clears the pointer."
        )
    target = landed[-1]
    if target.main is None:
        return pointer, None
    # It lands on main's row, under the name the merge leaves that row with.
    renamed = (
        target.base is not None
        and target.branch is not None
        and _event_key(target.branch) != _event_key(target.base)
        and _event_key(target.main) == _event_key(target.base)
    )
    landed_name = target.branch["name"] if renamed and target.branch else target.main["name"]
    return f"{target.main['event_type_name']}.{landed_name}", None


def _variable_clash(var: _VariableSides) -> set[str] | None:
    """The keys a gated variable clashes on, or None when its presence does.

    An empty set means the gate refuses for a reason no key names (the copies
    added on both sides, say): the caller treats it like a presence clash.
    """
    if var.base is None or var.main is None or var.branch is None:
        return None
    return set(conflicting_fields(var.base, var.main, var.branch, _VAR_CHANGE_KEYS))


def _property(
    var_name: str,
    var: _VariableSides,
    side_keys: dict[str, EventKey | None],
    *,
    blocked: bool,
) -> MergedProperty | None:
    main_entry = _entry_for(var.main, side_keys["main"])
    main_attrs = _var_attrs(var.main)
    previous_values = _effective(main_entry, main_attrs)
    if blocked:
        branch_entry = _entry_for(var.branch, side_keys["branch"])
        entry = branch_entry or main_entry
        attrs = _var_attrs(var.branch) or main_attrs or {}
        return MergedProperty(
            name=var_name,
            variable_type=str(attrs.get("variable_type") or ""),
            required=bool((entry or {}).get("required")),
            previous_required=None if main_entry is None else bool(main_entry.get("required")),
            override=(entry or {}).get("values") is not None,
            values=[
                MergedPropertyValue(value=value, state="conflict")
                for value in sorted(
                    {*(_effective(branch_entry, attrs) or []), *(previous_values or [])}
                )
            ],
            state="conflict",
            variable_state="conflict",
        )
    if var.attrs is None:
        # The variable does not survive the merge.
        if main_entry is None:
            return None
        return MergedProperty(
            name=var_name,
            variable_type=str((main_attrs or {}).get("variable_type") or ""),
            required=False,
            previous_required=bool(main_entry.get("required")),
            override=main_entry.get("values") is not None,
            values=[MergedPropertyValue(value=v, state="removed") for v in previous_values or []],
            state="removed",
            variable_state="removed",
        )
    if var.overrides_from == "branch":
        entry = _entry_for(var.branch, side_keys["branch"])
        main_moved = False
    else:
        entry = main_entry
        base_entry = _entry_for(var.base, side_keys["base"])
        main_moved = var.base is not None and main_entry != base_entry
    if entry is None and main_entry is None:
        return None
    values = _effective(entry, var.attrs)
    if entry is None:
        state: MergedState = "removed"
    elif main_entry is None:
        state = "added"
    elif (
        bool(entry.get("required")) != bool(main_entry.get("required")) or values != previous_values
    ):
        state = "changed"
    else:
        state = "unchanged"
    if var.main is None:
        variable_state: MergedState = "added"
    elif var.attrs != main_attrs:
        variable_state = "changed"
    else:
        variable_state = "unchanged"
    return MergedProperty(
        name=var_name,
        variable_type=str(var.attrs.get("variable_type") or ""),
        required=bool((entry or {}).get("required")),
        previous_required=None if main_entry is None else bool(main_entry.get("required")),
        override=(entry or {}).get("values") is not None,
        values=_value_rows(values, previous_values)
        if entry is not None
        else [MergedPropertyValue(value=v, state="removed") for v in previous_values or []],
        state=state,
        variable_state=variable_state,
        main_moved=main_moved,
    )
