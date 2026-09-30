"""Diff entries that are the machine's doing, not the author's.

A branch diff is read by a reviewer deciding whether to merge, and every row
in it reads as "the author did this". Two kinds of removal are not that. A scan
mints a variable for every JSON key it meets, including keys typed by users —
on production a branch with two authored events showed seven removed variables
named after cities, all "Auto-detected variable from data source scan", none
bound, documented or referenced (tripl-kjhi.12). And a removal main has ALSO
made since the branch was cut is nothing the merge will do. Both are tagged
here with a reason, kept out of the headline counts, and folded into one line
by the UI; the entries themselves stay in the response, so nothing is hidden.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tripl.core.analyzers._event_generator_variables import (
    SCAN_PROVENANCE_DESCRIPTION,
    display_name_candidates,
)
from tripl.core.property_schema import is_scan_inferred_schema
from tripl.core.variable_retirement import referenced_tokens, tokens_of
from tripl.models.variable import Variable
from tripl.schemas.plan_revision import PlanDiffEntry

#: The reasons ``PlanDiffEntry.housekeeping`` can carry, worded for the row.
RETIRED_SCAN_VARIABLE = "unused scan variable retired"
ALREADY_REMOVED_ON_MAIN = "already removed on main"


def _bindings_are_the_scans(before: dict[str, Any]) -> bool:
    """Whether the variable's bindings are exactly the ones the scan writes.

    ``ensure_variable`` mints every scan variable with ``bindings=[source_name]``
    and never touches them again, so that — not an empty list — is the scan's
    own shape, and anything else is a hand-written binding
    (``core.variable_retirement._human_claim`` reads it the same way). Requiring
    NO bindings matched no scan-minted row at all: only a variable made through
    the API, or a base serialised before bindings existed, so the very rows this
    rule was written for stayed in the reviewer's counts (tripl-0zpq.138). A
    payload without the key predates bindings, and is read as the scan's.
    """
    bindings = before.get("bindings")
    if bindings is None:
        return True
    source_name = before.get("source_name")
    return isinstance(bindings, list) and bindings == ([source_name] if source_name else [])


def _name_is_the_scans(before: dict[str, Any]) -> bool:
    """Whether the name is one the scan could have given this variable.

    A person's rename is a mark like any other — the retirement sweep keeps a
    renamed row for exactly that reason (tripl-bwo8) — and ``source_name`` is
    what the scan named it by. A row without one has no scan-written name to
    compare against.
    """
    source_name = before.get("source_name")
    if not source_name:
        return True
    name = before.get("name")
    return name == source_name or name in display_name_candidates(str(source_name))


def _is_unused_scan_variable(before: dict[str, Any] | None) -> bool:
    """A variable only the scan ever knew about.

    Scan provenance is the description the generator stamps and nobody has
    edited. "Unused" is every other place a person leaves a mark on a variable
    left as the scan wrote it: no documented values, no per-event overrides, no
    binding beyond the scan's own, the scan's name, and no tombstone — an
    excluded variable is an instruction to the scan, and removing it un-excludes
    the name, so the next scan mints it again. Whether an event still names it
    is the one test the row cannot answer alone; ``note_references`` answers it
    from the diff's new side, which no longer has the row but may still have
    events that name it, and ``mark_housekeeping`` from main's. Drift triage and
    observed values are not in the snapshot, so this rule cannot see them.
    """
    if not before:
        return False
    return (
        before.get("description") == SCAN_PROVENANCE_DESCRIPTION
        and not before.get("allowed_values")
        and not before.get("event_value_overrides")
        and not before.get("excluded_from_scans")
        and is_scan_inferred_schema(before.get("json_schema"))
        and _bindings_are_the_scans(before)
        and _name_is_the_scans(before)
    )


def _tokens_of(before: dict[str, Any]) -> set[str]:
    """Every name a ``${token}`` could use for the removed variable.

    ``core.variable_retirement.tokens_of`` itself, run on a transient row built
    from the snapshot: the token arm ``plan_retirement`` keeps a REFERENCED
    variable by, and the set the scan resolves tokens through. A copy spelled
    out here could drift from it by a field, and tag a variable the plan still
    names.
    """
    bindings = before.get("bindings")
    name, source_name = before.get("name"), before.get("source_name")
    return tokens_of(
        Variable(
            name=name if isinstance(name, str) else None,
            source_name=source_name if isinstance(source_name, str) else None,
            bindings=[b for b in bindings if isinstance(b, str)]
            if isinstance(bindings, list)
            else [],
        )
    )


def _tokens_named_by_events(payload: dict[str, Any]) -> set[str]:
    """Every ``${token}`` the events in ``payload`` carry in a field or meta value.

    Both, for the reason ``variable_retirement_service`` reads both: a token in
    a meta value renders "Unknown variable token" just the same once its
    variable is gone.
    """
    values: list[str] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        for collection in ("field_values", "meta_values"):
            for member in event.get(collection) or []:
                if isinstance(member, dict) and isinstance(member.get("value"), str):
                    values.append(member["value"])
    return referenced_tokens(values)


def note_references(entries: Iterable[PlanDiffEntry], new_payload: dict[str, Any]) -> None:
    """Mark every removed variable an event in ``new_payload`` still names.

    ``new_payload`` is the diff's new side, the one without the variable: the
    branch, in a branch diff. The scan-shape test reads the row alone, and a
    scan variable is normally still in scan shape AND referenced: the scan
    itself rewrites field values to ``${<display name>}``. Without this, a
    branch deleting a live ``session_time`` read as "unused scan variable
    retired", out of the counts and past the merge's "deletes variables from
    main" warning, while the merge deleted it on main with its observed values
    and drift history, and left ``${session_time}`` unresolved (tripl-0zpq.138).
    """
    removed = [
        entry
        for entry in entries
        if entry.kind == "removed" and entry.entity_type == "variable" and entry.before
    ]
    if not removed:
        return
    named = _tokens_named_by_events(new_payload)
    for entry in removed:
        entry._still_referenced = bool(named & _tokens_of(entry.before or {}))


def _identity(entry: PlanDiffEntry) -> tuple[str, str | None, str]:
    return (entry.entity_type, entry.parent, entry.name)


def mark_housekeeping(
    entries: list[PlanDiffEntry],
    *,
    behind_entries: Iterable[PlanDiffEntry] = (),
    main_payload: dict[str, Any] | None = None,
) -> None:
    """Stamp ``housekeeping`` on the removals a reviewer need not read.

    ``behind_entries`` is base-vs-main: what main did since the branch was cut.
    A removal present on both sides is already true on main, so the merge
    leaves nothing for it to do — the stronger statement, so it wins when both
    rules match. The scan-variable rule needs no main side, so a legacy branch
    without a base snapshot still gets it.

    A removal whose scan identity reappears on an ADDED variable is half of a
    rename, or a delete-and-recreate — the author's doing either way, whatever
    the old row looked like. ``snapshot_rename_pairs`` names the pairs the merge
    will make, but it needs main and runs after this; an identity carried by an
    addition is the superset of those pairs, so it can only keep a removal in
    the counts, never hide one (tripl-0zpq.138). So can a reference: a
    variable an event still names is in use, however scan-shaped the row —
    on the diff's new side (``note_references``), or on main, ``main_payload``.
    Main's events are read here because the merge deletes the variable THERE:
    one main started naming after the cut is deleted on main with its observed
    values, and main's ``${token}`` left unresolved, whatever the branch holds.
    """
    removed_on_main = {_identity(e) for e in behind_entries if e.kind == "removed"}
    carried_on = {
        entry.after.get("source_name")
        for entry in entries
        if entry.kind == "added" and entry.entity_type == "variable" and entry.after
    } - {None, ""}
    scan_shaped: list[PlanDiffEntry] = []
    for entry in entries:
        if entry.kind != "removed":
            continue
        if _identity(entry) in removed_on_main:
            entry.housekeeping = ALREADY_REMOVED_ON_MAIN
        elif (
            entry.entity_type == "variable"
            and not entry._still_referenced
            and _is_unused_scan_variable(entry.before)
            and (entry.before or {}).get("source_name") not in carried_on
        ):
            scan_shaped.append(entry)
    if not scan_shaped:
        return
    named_on_main = _tokens_named_by_events(main_payload) if main_payload is not None else set()
    for entry in scan_shaped:
        if not named_on_main & _tokens_of(entry.before or {}):
            entry.housekeeping = RETIRED_SCAN_VARIABLE


def reviewable(entries: Iterable[PlanDiffEntry]) -> list[PlanDiffEntry]:
    """The entries that count as the author's changes."""
    return [entry for entry in entries if entry.housekeeping is None]
