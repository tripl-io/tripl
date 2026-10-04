"""Line up base, main and branch rows for the merge, one slot per row.

The merge used to upsert events and relations by natural key, one row per key
on each side: with two main rows sharing (type, name) — namesakes, which nothing
forbids — a main row was deleted only when its KEY vanished from the branch, so
deleting one namesake on the branch left both on main, and each branch copy's
edits landed on whichever main namesake the dict kept. Every
arm that followed a row through that map inherited the guess: the variable
overrides, the successor pointer, the order, the photos and the discussion.

A slot is one base row with its main counterpart (paired by main's own id,
which the base recorded) and its branch counterpart (paired by the copy's
``origin_id``). The natural key is the fallback, only for rows the ids do not
place — see ``_origin_pairing``. Rows the base never had are matched across the
two sides by key when exactly one of each is there (both sides added the same
thing, which the conflict scan has already judged), and otherwise the branch's
are created.

Pure, like ``pair_rows`` and ``pair_renames``: the caller is the merge engine,
where a wrongly matched id cannot be taken back.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tripl.services._origin_pairing import pair_rows, snapshot_id
from tripl.services._plan_branch_renames import NaturalKey, pair_renames


@dataclass
class Slot[MainT, BranchT]:
    """One row of the plan as the base, main and the branch each hold it.

    ``base`` is None for a row both sides added. ``branch_known`` is False when
    the branch side of this base row is one of several namesakes nothing tells
    apart: the merge then neither deletes main's row nor writes to it, the
    behaviour a branch opened before origin ids had for them.
    """

    base: dict[str, Any] | None
    main: MainT | None = None
    branch: BranchT | None = None
    branch_known: bool = True


@dataclass
class MergeSlots[MainT, BranchT]:
    slots: list[Slot[MainT, BranchT]] = field(default_factory=list)
    # Branch rows with no counterpart on main or in the base: created on main.
    created: list[BranchT] = field(default_factory=list)


def merge_slots[MainT, BranchT, KeyT: NaturalKey](
    base_items: Sequence[dict[str, Any]],
    main_rows: Sequence[MainT],
    branch_rows: Sequence[BranchT],
    *,
    base_key: Callable[[dict[str, Any]], KeyT],
    main_key: Callable[[MainT], KeyT],
    branch_key: Callable[[BranchT], KeyT],
    main_ref: Callable[[MainT], object],
    branch_ref: Callable[[BranchT], object],
    follows_rename: Callable[[KeyT, KeyT], bool],
    identities: tuple[
        Callable[[dict[str, Any]], str | None],
        Callable[[MainT], str | None],
        Callable[[BranchT], str | None],
    ]
    | None = None,
    branch_origins_complete: bool = False,
) -> MergeSlots[MainT, BranchT]:
    """Pair the three sides — see the module docstring.

    ``follows_rename(base_key, branch_key)`` says whether a branch copy placed
    by its origin under another key is still that row, renamed (an event keeps
    its type), or has to be read as a removal plus an addition, which is how
    the merge has always carried a relation whose ends moved. ``identities``
    reads each side's scan identity for the rows the ids leave unplaced, so a
    rename on a row without an origin id still pairs the way ``pair_renames``
    always paired it. ``branch_origins_complete`` is the branch's
    ``origin_ids_complete``; main's rows always are their own origin.
    """
    main_side = pair_rows(
        base_items,
        main_rows,
        key_of_old=base_key,
        key_of_new=main_key,
        id_of_old=snapshot_id,
        ref_of_new=main_ref,
        unplaced_are_new=True,
    )
    branch_side = pair_rows(
        base_items,
        branch_rows,
        key_of_old=base_key,
        key_of_new=branch_key,
        id_of_old=snapshot_id,
        ref_of_new=branch_ref,
        unplaced_are_new=branch_origins_complete,
    )
    slot_of: dict[int, Slot[MainT, BranchT]] = {id(item): Slot(base=item) for item in base_items}

    for base_item, main_row in [*main_side.pairs, *main_side.renamed]:
        slot_of[id(base_item)].main = main_row

    branch_added: list[BranchT] = list(branch_side.added)
    for base_item, branch_row in branch_side.pairs:
        slot_of[id(base_item)].branch = branch_row
    for base_item, branch_row in branch_side.renamed:
        if follows_rename(base_key(base_item), branch_key(branch_row)):
            slot_of[id(base_item)].branch = branch_row
        else:
            branch_added.append(branch_row)
    for olds, branches in branch_side.ambiguous.values():
        # Only on a branch opened before origin ids, under a name the migration
        # could not link: the one-row-per-name answer it always had — the last
        # of each paired, the other base rows neither deleted nor written.
        slot_of[id(olds[-1])].branch = branches[-1]
        for base_item in olds[:-1]:
            slot_of[id(base_item)].branch_known = False

    if identities is not None and branch_side.removed and branch_added:
        base_identity, main_identity, branch_identity = identities
        removed = _unique_by_key(branch_side.removed, base_key)
        added = _unique_by_key(branch_added, branch_key)
        renames = pair_renames(
            {key: base_identity(item) for key, item in removed.items()},
            {key: main_identity(row) for key, row in _unique_by_key(main_rows, main_key).items()},
            {key: branch_identity(row) for key, row in added.items()},
        )
        for old_key, new_key in renames.items():
            slot_of[id(removed[old_key])].branch = added[new_key]
            branch_added.remove(added[new_key])

    result: MergeSlots[MainT, BranchT] = MergeSlots(slots=list(slot_of.values()))
    main_added = _unique_by_key(main_side.added, main_key)
    branch_added_counts = Counter(branch_key(row) for row in branch_added)
    for branch_row in branch_added:
        key = branch_key(branch_row)
        matched = main_added.get(key) if branch_added_counts[key] == 1 else None
        if matched is None:
            result.created.append(branch_row)
        else:
            result.slots.append(Slot(base=None, main=matched, branch=branch_row))
    return result


def _unique_by_key[RowT, KeyT: Hashable](
    rows: Sequence[RowT], key_of: Callable[[RowT], KeyT]
) -> dict[KeyT, RowT]:
    """``rows`` by key, leaving out every key more than one of them holds."""
    counts = Counter(key_of(row) for row in rows)
    return {key_of(row): row for row in rows if counts[key_of(row)] == 1}
