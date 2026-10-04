"""Pair two sides of a plan row by row: origin id first, natural key second.

Events and relations carry no uniqueness on the natural key the branch diff,
merge, conflict scan and revert used to pair them by — (event type, name) and
the four names a relation links — so two rows can share it (namesakes). Keyed
one row per key, every one of those paths collapsed a pair to whichever row was
listed last: a deleted namesake stayed on main after the merge, an edit to one
landed on the other, and an earlier attempt to pair by content found 21 more
ways to guess wrong.

A branch copy now records the main row it was made from (``origin_id``), and a
main row IS its own origin, so two sides pair exactly when one of them names
the other's ids: ``ref_of_new`` answers with the origin id for a branch copy and
the row's own id for a main row, and ``id_of_old`` with the id the old side
recorded (a stored base snapshot records main's ids). The natural key is the
fallback, and only for rows the ids do not place: a row authored on the branch,
or a branch opened before origin ids whose namesakes the migration could not
tell apart. Even then a key is paired only when it holds exactly one unplaced
row on each side. Several are ``ambiguous`` — unless the caller knows the new
side records every origin it has (``unplaced_are_new``): then an unplaced old
row is one the new side deleted and an unplaced new row one it created, and
nothing is left to guess.

Pure: no I/O, no ORM, so the diff (snapshot dicts), the conflict scan and the
merge (ORM rows) share one definition of "the same row".
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Pairing[OldT, NewT]:
    """How the rows of an old side and a new side correspond.

    ``pairs`` are one row each side under the same natural key, placed by id or,
    failing that, as the only unplaced row under that key on both sides.
    ``renamed`` are placed by id but under a different key on each side — the
    same row, renamed. ``added`` and ``removed`` have no counterpart.
    ``ambiguous`` holds, per key, the unplaced rows of a key where at least one
    side has several: nothing says which of them correspond.
    """

    pairs: list[tuple[OldT, NewT]] = field(default_factory=list)
    renamed: list[tuple[OldT, NewT]] = field(default_factory=list)
    added: list[NewT] = field(default_factory=list)
    removed: list[OldT] = field(default_factory=list)
    ambiguous: dict[Hashable, tuple[list[OldT], list[NewT]]] = field(default_factory=dict)


def pair_rows[OldT, NewT](
    old_rows: Sequence[OldT],
    new_rows: Sequence[NewT],
    *,
    key_of_old: Callable[[OldT], Hashable],
    key_of_new: Callable[[NewT], Hashable],
    id_of_old: Callable[[OldT], object],
    ref_of_new: Callable[[NewT], object],
    unplaced_are_new: bool = False,
) -> Pairing[OldT, NewT]:
    """Pair ``new_rows`` to ``old_rows`` — see the module docstring."""
    result: Pairing[OldT, NewT] = Pairing()
    old_by_id: dict[object, OldT] = {}
    # A payload is data, not a schema: should it list one id twice, the id
    # places neither row and both fall to the natural key.
    repeated_ids: set[object] = set()
    for old in old_rows:
        old_id = id_of_old(old)
        if old_id is None:
            continue
        if old_id in old_by_id:
            repeated_ids.add(old_id)
        old_by_id[old_id] = old
    claimed: set[object] = set()
    placed_new: set[int] = set()
    for index, new in enumerate(new_rows):
        ref = ref_of_new(new)
        if ref is None or ref in claimed or ref in repeated_ids or ref not in old_by_id:
            continue
        old = old_by_id[ref]
        claimed.add(ref)
        placed_new.add(index)
        if key_of_old(old) == key_of_new(new):
            result.pairs.append((old, new))
        else:
            result.renamed.append((old, new))

    unplaced_old: dict[Hashable, list[OldT]] = {}
    for old in old_rows:
        if id_of_old(old) in claimed:
            continue
        unplaced_old.setdefault(key_of_old(old), []).append(old)
    unplaced_new: dict[Hashable, list[NewT]] = {}
    for index, new in enumerate(new_rows):
        if index in placed_new:
            continue
        unplaced_new.setdefault(key_of_new(new), []).append(new)

    for key in {**unplaced_old, **unplaced_new}:
        olds = unplaced_old.get(key, [])
        news = unplaced_new.get(key, [])
        if not olds:
            result.added.extend(news)
        elif not news:
            result.removed.extend(olds)
        elif len(olds) == 1 and len(news) == 1:
            result.pairs.append((olds[0], news[0]))
        elif unplaced_are_new:
            result.removed.extend(olds)
            result.added.extend(news)
        else:
            result.ambiguous[key] = (olds, news)
    return result


def snapshot_ref(item: dict[str, Any]) -> object:
    """The main row a snapshot entry stands for: its origin, else itself."""
    return item.get("origin_id") or item.get("id")


def snapshot_id(item: dict[str, Any]) -> object:
    """The id a snapshot entry recorded, for the old side of a pairing."""
    return item.get("id")
