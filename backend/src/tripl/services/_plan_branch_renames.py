from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tripl.schemas.plan_branch import PlanDiffRename
from tripl.schemas.plan_revision import PlanEntityType

# A natural key is the row's rename scope followed by its name:
# ``("track", "purchase:success")`` for an event, ``("variant",)`` for a
# variable. A rename moves the LAST component and nothing else, so the boundary
# a rename may not cross is exactly ``key[:-1]`` and needs no second argument.
# Moving an event to another event type therefore reads as what it is — not a
# rename — and never pairs.
NaturalKey = tuple[str, ...]


def _sole_key_by_identity[KeyT: NaturalKey](
    side: Mapping[KeyT, str | None],
) -> dict[tuple[NaturalKey, str], KeyT]:
    """Each ``(scope, source_name)`` this side names exactly once, and its key.

    An identity carried by two rows is dropped rather than guessed at. Live rows
    cannot do that any more — ``uq_variable_project_source_name`` makes a
    variable's identity singular per branch, ``uq_event_scan_identity`` an
    event's singular per type — but one of the three sides is the
    BASE, a stored ``PlanRevision`` payload, and a snapshot taken before that
    constraint existed can still list one identity twice. A payload is data,
    not a constraint, so the rule stays on every side rather than being argued
    away for the two that are live.

    Which rows are "this side" is the caller's decision, and it is not always the
    whole mapping — see ``pair_renames``, which narrows main to the base first so
    that a row that could never be a rename source cannot veto one either.
    """
    keys_by_identity: dict[tuple[NaturalKey, str], list[KeyT]] = {}
    for key, source_name in side.items():
        # Nullable by design — "rows created outside a scan" — so two unrelated
        # rows can both be empty and neither identifies anything. Pairing on
        # absence would fuse arbitrary rows.
        if not source_name:
            continue
        keys_by_identity.setdefault((key[:-1], source_name), []).append(key)
    return {identity: keys[0] for identity, keys in keys_by_identity.items() if len(keys) == 1}


def pair_renames[KeyT: NaturalKey](
    base: Mapping[KeyT, str | None],
    main: Mapping[KeyT, str | None],
    branch: Mapping[KeyT, str | None],
    *,
    vacate_removed: bool = False,
) -> dict[KeyT, KeyT]:
    """Match main's rows to the branch rows they were renamed into.

    Each mapping is one side's natural key -> that row's ``source_name``.
    Returns ``old_key -> new_key`` for every pair proven to be a single row the
    branch renamed, so the caller can UPDATE main's row instead of replacing it.

    ``_apply_merge`` upserts by natural key, and for an Event that key contains
    the very name the user edited — Event has no ``display_name``, so its
    machine name IS the displayed one and editing it is routine. Left unpaired,
    a rename reads as a removal plus an unrelated addition: main's row is
    deleted, a fresh uuid inserted, the FK cascade takes
    ``variable_values.event_id``, their drift rows and ``event_changes`` with
    it, and the ``event_metrics`` series is left holding a NULL ``event_id``.
    None of those are in ``build_plan_snapshot``, so no diff shows the loss and
    no arm of the merge carries them across. Photos cascade as well but do not
    belong on that list — they are snapshotted and the merge has an arm that
    re-creates them — and alerting holds no FK to an event at all; what makes
    the losses above worth a rename is that nothing sees them go.

    ``source_name`` is what survives a rename, and that is the repo's own rule
    rather than a marker invented here: ``update_event`` and ``update_variable``
    write ``name`` and never touch it, ``create_event`` stamps it from the
    generated name because scan dedup keys on it, the Event model declares it
    the stable scan identity, and ``deep_copy_plan_to_branch`` carries it onto
    the branch copy so a branch row still answers to main's identity.

    So the pairing reads the identity directly on both sides and never the name
    key sets. That is the whole point: derived from the key sets, a
    rename was only visible when its name VANISHED from the branch, and in a
    cycle no name vanishes. A plain swap — A renamed to B while B is renamed to
    A, reachable through a temporary name — left both sets empty and paired
    nothing, and a longer rotation put mismatched rows in them. The upsert then
    matched each branch row to the main row wearing its new name and wrote that
    row's ``source_name`` onto it, which for a Variable is a non-deferrable
    ``UNIQUE (project_id, branch_id, source_name)`` violated inside one flush.

    A pair is still made only when the identity is unambiguous, and the shapes
    that must keep merging as a delete plus an insert still do:

    * **No source_name**, or **two or more candidates on one side** — see
      ``_sole_key_by_identity``, which drops both. On main "one side" means the
      rows that were there when the branch was cut, not every row main holds
      now; the comment on the narrowing below says why.
    * **An identity only one side carries.** A genuinely added row and a
      genuinely removed one, which must keep merging as an add and a removal.
    * **A row main never had at the base.** A rename moves a row that existed
      when the branch was cut; anything else is main's own edit racing the
      branch's, which conflict detection judges rather than this.
    * **A key main re-used for another identity.** ``old_key`` must hold the
      same ``source_name`` on the base and on main; a base key main has since
      handed to a different row is main's rename, not the branch's.
    * **A move onto a name a STAYING main row still holds.** The branch renamed
      A to B while main independently grew its own B: honouring the rename would
      put two rows on one name. Dropping one such move can strand another that
      was only legal because its destination was being vacated, so the check
      repeats until it stops finding any.

    ``vacate_removed`` relaxes that last rule for one shape only:
    the branch DELETED the row holding the destination and moved another row
    onto its name. The occupant counts as leaving when it was there at the cut
    under the identity it still carries on main, that identity is non-empty, and
    no branch row carries it any more. Anything short of that — an occupant with
    no ``source_name``, one main re-identified since the cut, one main grew after
    it, or an identity the branch still holds — stays blocked, because there the
    branch row at that name could just as well be the occupant edited. A caller
    that passes it MUST delete, and flush, every displaced occupant (a
    destination main holds that is not itself moving) before writing the moves.

    The key type is the caller's own, not widened to ``NaturalKey``, so the
    result can be used to re-key the very maps it was derived from — with
    ``rekey_in_place``, because the result may now be a permutation.

    Pure by construction — three plain mappings in, one mapping out — because
    the caller is the merge engine, where a wrongly matched id is unrecoverable,
    and every case above therefore deserves a test that needs no database.
    """
    # Main's side is narrowed to the base BEFORE the identities are counted. The
    # loop below already refuses an ``old_key not in base``, so a row main grew
    # after the branch was cut can never BE a rename source — but left in the
    # grouping it can still VETO one, by making an identity look ambiguous.
    # Grouping whole sides is new here: the previous shape grouped only the
    # merge's own would-delete and would-insert candidates, so a duplicate that
    # was not itself a candidate never poisoned anything. When this was written
    # a second Event carrying one identity was a live shape — events had only an
    # index — and a rename that used to pair fell back to delete-plus-insert,
    # which cascades ``variable_values``, their drift rows and ``event_changes``.
    # ``uq_event_scan_identity`` has since closed that shape for
    # live rows; the base is a stored payload and can still carry
    # it, which is why the narrowing stays.
    #
    # The branch side is deliberately NOT narrowed: in a cycle a rename's
    # destination is a key that main and the base both already hold, and that is
    # the whole shape this function exists to see. Nor does this narrowing touch
    # the "is the destination occupied?" test at the end, which reads ``main``
    # itself — a row main grew after the cut still holds its name and must still
    # block a move onto it.
    main_by_identity = _sole_key_by_identity(
        {key: source_name for key, source_name in main.items() if key in base}
    )
    branch_by_identity = _sole_key_by_identity(branch)

    moves: dict[KeyT, KeyT] = {}
    for identity, old_key in main_by_identity.items():
        new_key = branch_by_identity.get(identity)
        if new_key is None or new_key == old_key or old_key not in base:
            continue
        # The base row under ``old_key`` has to carry the identity main's row
        # carries there now. Otherwise the key was re-used on MAIN after the
        # cut: main deleted ``c`` (S2) and renamed ``a`` (S1) to ``c`` — retire
        # v1, promote v2 — and a branch that touched neither still holds S1
        # under ``a``. Paired, that reads as the branch renaming ``c`` to ``a``,
        # and the merge would rename main's row back and re-key base-``c`` onto
        # it, undoing main's rename and resurrecting what main deleted.
        # A true branch rename moves a row that still wears
        # its base identity on main.
        if base[old_key] != identity[1]:
            continue
        moves[old_key] = new_key

    # A destination is free either because main has nothing there or because the
    # row that is there is itself moving away. Removing a move makes its source a
    # staying row, which can block a move that was previously fine, so this runs
    # to a fixed point rather than once.
    branch_identities = {
        (key[:-1], source_name) for key, source_name in branch.items() if source_name
    }

    def vacated(key: KeyT) -> bool:
        occupant = main.get(key)
        return (
            vacate_removed
            and bool(occupant)
            and key in base
            and base[key] == occupant
            and (key[:-1], occupant) not in branch_identities
        )

    while blocked := [
        old for old, new in moves.items() if new in main and new not in moves and not vacated(new)
    ]:
        for old in blocked:
            del moves[old]
    return moves


def _variable_identities(payload: Mapping[str, Any]) -> dict[tuple[str, ...], str | None]:
    """A plan snapshot's variables as ``pair_renames`` wants them."""
    return {(item["name"],): item.get("source_name") for item in payload.get("variables", [])}


def _event_identities(payload: Mapping[str, Any]) -> dict[tuple[str, ...], str | None]:
    return {
        (item["event_type_name"], item["name"]): item.get("source_name")
        for item in payload.get("events", [])
    }


# The two entity kinds the merge pairs, and only those: ``_apply_merge`` calls
# ``pair_renames`` for variables and for events and for nothing else, because
# ``source_name`` is the only identity a rename cannot move and it is the only
# thing ``build_plan_snapshot`` records it on. Event types, fields, meta fields
# and relations therefore keep merging — and keep reporting — as a removal plus
# an addition, which is what they are.
#
# The keys built above are the keys ``compute_plan_diff_entries`` uses for the
# same two sets (``item["name"]`` for a variable, ``(event_type_name, name)`` for
# an event). That correspondence is load-bearing: the membership tests in
# ``snapshot_rename_pairs`` decide whether a diff ENTRY exists, so a key shaped
# differently here would answer about a row the diff never split.
_PAIRED_ENTITY_TYPES: tuple[
    tuple[PlanEntityType, Callable[[Mapping[str, Any]], dict[tuple[str, ...], str | None]]], ...
] = (
    ("variable", _variable_identities),
    ("event", _event_identities),
)


def snapshot_rename_pairs(
    base_payload: Mapping[str, Any],
    main_payload: Mapping[str, Any],
    branch_payload: Mapping[str, Any],
) -> list[PlanDiffRename]:
    """The renames a merge of this branch will pair, named the way the diff names them.

    A branch diff compares the base snapshot with the branch, and keys entities
    by name, so a rename arrives as a removal of the old name plus an addition of
    the new one — two rows that look like a deletion and an unrelated creation.
    The merge knows better, and this says what the merge knows, from the same
    ``pair_renames`` the merge itself calls. A second implementation
    would be free to drift, and the cost of drift here is a UI that promises a
    rename the merge then performs as a delete-plus-insert, cascading the
    variable's observed values and drift rows on the way through.

    Only pairs the diff actually SPLIT are returned. ``pair_renames`` also pairs
    a swap and a longer rotation, where every name is present on both sides and
    the diff shows two ``changed`` entries instead — there is no removal and no
    addition to join up, so reporting one would name entries that do not exist.
    Hence the two membership tests: a removal exists only for an old key absent
    from the branch, an addition only for a new key absent from the base.

    Three payloads and not two, because the merge's pairing reads main: a move
    onto a name a staying main row still holds is refused, and a row main grew
    after the branch was cut can never be a rename source. Passing the base twice
    would answer a question nobody asked and would promise pairings the merge
    would then refuse. Callers already hold all three — ``diff_branch`` builds
    the main and branch snapshots to compute the diff at all.

    The answer is as fresh as ``main_payload``: main can move between this call
    and the merge, and then the merge pairs what main says at that moment. That
    is the same staleness ``behind_base`` already reports, not a new one.
    """
    pairs: list[PlanDiffRename] = []
    for entity_type, identities_of in _PAIRED_ENTITY_TYPES:
        base = identities_of(base_payload)
        branch = identities_of(branch_payload)
        # Same pairing the merge performs, including its variable-only vacating
        # rule, so the diff never promises what the merge refuses.
        renames = pair_renames(
            base,
            identities_of(main_payload),
            branch,
            vacate_removed=entity_type == "variable",
        )
        for old_key, new_key in renames.items():
            if old_key in branch or new_key in base:
                continue
            pairs.append(
                PlanDiffRename(
                    entity_type=entity_type,
                    # ``pair_renames`` never crosses ``key[:-1]``, so both halves
                    # share a parent and either one names it.
                    parent=old_key[0] if len(old_key) > 1 else None,
                    removed_name=old_key[-1],
                    added_name=new_key[-1],
                )
            )
    # Sorted so the response does not reorder between two identical requests
    # merely because a dict was built in a different order.
    return sorted(pairs, key=lambda pair: (pair.entity_type, pair.parent or "", pair.removed_name))


def rekey_in_place[KeyT, ValueT](mapping: dict[KeyT, ValueT], renames: Mapping[KeyT, KeyT]) -> None:
    """Move every ``old_key -> new_key`` of ``renames`` at once.

    ``pair_renames`` can return a permutation — a two-row swap, or a longer
    rotation — so one pair's new key is another pair's old key. Re-keying a pair
    at a time would file the first row under the second pair's OLD key and then
    read it straight back as the second row, quietly fusing two identities.
    Lifting every moving entry out before putting any back cannot.

    ``mapping`` must hold every old key. ``pair_renames`` only proposes a move
    for a key that is on main and in the base, which is exactly what the merge
    engine passes here — a ``KeyError`` means that invariant broke, and is a
    better answer than a half-applied permutation.
    """
    lifted = {old: mapping.pop(old) for old in renames}
    for old, new in renames.items():
        mapping[new] = lifted[old]
