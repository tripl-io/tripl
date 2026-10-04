"""What stops an "Update from main" plan outright, whatever is chosen.

Two things do. A change of main's to a row the branch's pairing cannot place
(``ambiguous_main_changes``): namesakes on a branch opened before origin ids.
And a plan that would break a uniqueness rule (``identity_clashes``):

The three-way plan pairs rows by the merge's identities, so it cannot see two
DIFFERENT rows ending up with the same name: main renames variable ``a`` to
``b`` while the branch adds its own ``b``; main and the branch each add a
variable (or an event of one type) with the same ``source_name``. Applied,
such a plan fails on ``uq_variable_project_name``,
``uq_variable_project_source_name`` or ``uq_event_scan_identity`` with nothing
in the conflict list to act on. This module predicts the branch's rows after
the plan and names every clash up front, so the preview says which of the
branch's own rows to rename before the update can run.

Pure, like ``_plan_branch_three_way``: snapshot dicts and ops in, rows out.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from typing import Any

from tripl.services._plan_branch_three_way_model import (
    Op,
    Slot,
    conflict_label,
    conflict_name,
    entity_key,
    fields_of,
)
from tripl.services.plan_branch_conflicts import comparable_field


def ambiguous_main_changes(
    entity_type: str,
    ambiguous: Mapping[Hashable, tuple[Sequence[dict[str, Any]], Sequence[dict[str, Any]]]],
    slot_of: Mapping[int, Slot],
    main_added: Sequence[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> None:
    """Record every change of main's to a row the branch's pairing cannot place."""
    seen: set[str] = set()

    def block(item: Mapping[str, Any]) -> None:
        name = conflict_name(entity_type, item)
        if name in seen:
            return
        seen.add(name)
        blockers.append(
            {
                "kind": "ambiguous",
                "entity_type": entity_type,
                "name": name,
                "message": (
                    f"Main changed '{conflict_label(entity_type, item)}', which this branch "
                    "holds more than once under the same name. The branch predates origin "
                    "tracking, so there is no telling which copy main's change belongs to. "
                    "Copy your changes to a new branch."
                ),
            }
        )

    ambiguous_keys = set(ambiguous)
    for olds, _news in ambiguous.values():
        for base_item in olds:
            main_row = slot_of[id(base_item)].main
            if main_row is None or any(
                comparable_field(dict(base_item), f) != comparable_field(dict(main_row), f)
                for f in fields_of(entity_type)
            ):
                block(base_item)
    for row in main_added:
        if entity_key(entity_type, row) in ambiguous_keys:
            block(row)


def _identity(entity_type: str, item: Mapping[str, Any]) -> dict[str, Any]:
    if entity_type == "event":
        return {"_et": item.get("event_type_name"), "source_name": item.get("source_name")}
    return {"name": item.get("name"), "source_name": item.get("source_name")}


def _final_rows(
    entity_type: str, branch_items: Iterable[Mapping[str, Any]], ops: Sequence[Op]
) -> dict[str, dict[str, Any]]:
    """The identities of the branch's rows of one type once ``ops`` ran."""
    rows: dict[str, dict[str, Any]] = {
        f"branch:{item.get('id')}": _identity(entity_type, item) for item in branch_items
    }
    for op in ops:
        if op.kind != "delete" or op.branch is None:
            continue
        if op.entity_type == entity_type:
            rows.pop(f"branch:{op.branch.get('id')}", None)
        elif entity_type == "event" and op.entity_type == "event_type":
            type_name = op.branch.get("name")
            for handle in [h for h, row in rows.items() if row.get("_et") == type_name]:
                rows.pop(handle, None)
    for op in ops:
        if op.entity_type != entity_type or op.main is None:
            continue
        target = _identity(entity_type, op.main)
        if op.kind == "write" and op.branch is not None:
            row = rows.get(f"branch:{op.branch.get('id')}")
            if row is not None:
                row.update({attr: target[attr] for attr in target if attr in op.fields})
        elif op.kind == "create":
            rows[f"main:{op.main.get('id')}"] = target
    return rows


def _clashes(
    entity_type: str,
    rows: Mapping[str, Mapping[str, Any]],
    identity: tuple[str, ...],
    label: str,
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], int] = defaultdict(int)
    for row in rows.values():
        values = tuple(row.get(attr) for attr in identity)
        if any(value in (None, "") for value in values):
            continue
        groups[values] += 1
    out: list[dict[str, Any]] = []
    for values, count in groups.items():
        if count < 2:
            continue
        shown = values[-1]
        out.append(
            {
                "kind": "identity_clash",
                "entity_type": entity_type,
                "name": str(shown),
                "message": (
                    f"After the update two {entity_type.replace('_', ' ')}s would share the "
                    f"{label} '{shown}': one from main and one this branch added or renamed. "
                    "Rename the branch's one, then update."
                ),
            }
        )
    return out


def identity_clashes(branch: Mapping[str, Any], ops: Sequence[Op]) -> list[dict[str, Any]]:
    """Every name / ``source_name`` two rows would share after ``ops``."""
    variables = _final_rows("variable", branch.get("variables", []), ops)
    events = _final_rows("event", branch.get("events", []), ops)
    return [
        *_clashes("variable", variables, ("name",), "name"),
        *_clashes("variable", variables, ("source_name",), "source name"),
        *_clashes("event", events, ("_et", "source_name"), "source name"),
    ]


def ambiguous_reference_writes(
    main: Mapping[str, Any], branch: Mapping[str, Any], ops: Sequence[Op]
) -> list[dict[str, Any]]:
    """Every reference the update would place on an event it cannot tell apart.

    On a branch opened before origin ids, the apply finds the branch copy of
    main's event by name among rows no origin claims, and refuses when several
    share that name. A whole override write re-places EVERY override main's
    variable holds, and a successor write main's successor, so any of them
    naming such an event would fail the update at apply time; this names them
    before, so the preview does not offer an update that cannot run.
    """
    unclaimed = Counter(
        f"{item.get('event_type_name') or ''}.{item.get('name')}"
        for item in branch.get("events", [])
        if not item.get("origin_id")
    )
    ambiguous = {key for key, count in unclaimed.items() if count > 1}
    if not ambiguous:
        return []
    placed = {
        str(item.get("origin_id")) for item in branch.get("events", []) if item.get("origin_id")
    }
    for op in ops:
        if op.main is None or op.entity_type != "event":
            continue
        if op.kind in ("create", "origin"):
            placed.add(str(op.main.get("id")))
    main_ids: dict[str, list[str]] = defaultdict(list)
    for item in main.get("events", []):
        main_ids[f"{item.get('event_type_name') or ''}.{item.get('name')}"].append(str(item["id"]))

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def check(key: str) -> None:
        if key in seen or key not in ambiguous:
            return
        if all(main_id in placed for main_id in main_ids.get(key, [])):
            return
        seen.add(key)
        out.append(
            {
                "kind": "ambiguous",
                "entity_type": "event",
                "name": key[:255],
                "message": (
                    f"Main points at the event '{key}', which this branch holds more than "
                    "once under that name. The branch predates origin tracking, so there is "
                    "no telling which copy is meant. Rename one of them on the branch, then "
                    "update from main again."
                ),
            }
        )

    for op in ops:
        if op.main is None or op.kind not in ("write", "create"):
            continue
        if op.entity_type == "variable" and (
            op.kind == "create" or "event_value_overrides" in op.fields
        ):
            for override in op.main.get("event_value_overrides") or []:
                check(f"{override.get('event_type_name') or ''}.{override.get('event_name')}")
        elif (
            op.entity_type == "event"
            and (op.kind == "create" or "superseded_by" in op.fields)
            and op.main.get("superseded_by")
        ):
            check(str(op.main["superseded_by"]))
    return out
