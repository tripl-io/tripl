"""Move or copy rows of one plan branch's diff onto another open branch.

Analysts open one branch per tracker task; when review splits a task, the
rows that belong to the new task move to its branch instead of being authored
again. A MOVE applies the rows on the target and undoes them on the source; a
COPY only applies them.

Built from two writers that already exist, so the rules each learned hold
here unchanged:

* the target side is "Update from main"'s writer
  (``_plan_branch_update_apply.apply_update_plan``), fed the source's items
  through ``_TransferApplier``, which resolves the source ids those items
  carry and gives every created row the MAIN origin of the row it copies;
* the source side of a move is the revert (``apply_revert_entry``), run row by
  row exactly as "Undo" on each would run it.

What to write is decided first, on snapshots
(``_plan_branch_transfer_deps``): the closure of what the selection needs,
each row as an op, and every refusal at once. A refusal is never a question;
the user changes the target and retries.

Both branches must have been cut from the same main content: then the
source's base is the target's too, and a field the source changed can be
checked against the target by one base. Otherwise the call is refused with the
branch to update named.

One transaction, both branch rows held ``FOR NO KEY UPDATE`` in id order
(``hold_branches_for_transfer``; the DEADLOCK AUDIT in ``_plan_branch_locks``
says why not ``FOR SHARE``). A dry run makes every write of the real call —
the target's and, on a move, the source's reverts — and rolls them back, so a
refusal the revert would raise shows in the preview, not first on confirm.
A preview against a branch that does not exist yet (``target_branch_id``
null) cuts a throwaway branch from main inside the same transaction and rolls
it back with everything else.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.models.event_photo import EventPhoto
from tripl.models.event_photo_comment import EventPhotoComment
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.plan_revision import PlanRevision, PlanRevisionKind
from tripl.schemas.plan_branch import (
    BranchRevertRequest,
    BranchTransferConflict,
    BranchTransferItem,
    BranchTransferRequest,
    BranchTransferResult,
    PlanBranchDiff,
)
from tripl.schemas.plan_revision import PlanDiffEntry
from tripl.services import plan_branch_service
from tripl.services._plan_branch_locks import hold_branches_for_transfer
from tripl.services._plan_branch_transfer_apply import _TransferApplier, _TransferRefusal
from tripl.services._plan_branch_transfer_deps import (
    Sides,
    Unit,
    build_units,
    plan_transfer,
    revert_order,
)
from tripl.services._plan_branch_update_apply import apply_update_plan
from tripl.services._plan_branch_update_checks import identity_clashes
from tripl.services.plan_branch_conflicts import is_behind
from tripl.services.plan_branch_revert_service import (
    apply_revert_entry,
    base_payload_for,
    find_diff_entry,
    row_renamed_from,
)
from tripl.services.plan_branch_service import (
    _resolve_project,
    deep_copy_plan_to_branch,
    ensure_main_branch_id,
)
from tripl.services.plan_revision_service import (
    build_plan_snapshot,
    compute_plan_diff_entries,
    with_snapshot_defaults,
)

logger = logging.getLogger(__name__)

# Module-level so a test can stand in for the target's writes.
_apply = apply_update_plan

# The throwaway branch a null-target preview cuts; never committed.
_PREVIEW_BRANCH_PREFIX = "transfer-preview-"

# SQLSTATEs of a transaction the database aborted to break a cycle or an
# unserializable read: worth one more try, not a 500.
_RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})


class TransferOutcome(NamedTuple):
    result: BranchTransferResult
    # For the two audit rows the router writes after a real call.
    source_name: str


def _request(entry: PlanDiffEntry) -> BranchRevertRequest:
    return BranchRevertRequest(
        entity_type=entry.entity_type,
        name=entry.name,
        parent=entry.parent,
        entity_id=entry.entity_id,
    )


def _items(
    units: Iterable[Unit], needed_by: dict[tuple[int, ...], str] | None = None
) -> list[BranchTransferItem]:
    out: list[BranchTransferItem] = []
    for unit in units:
        for entry in unit.entries:
            out.append(
                BranchTransferItem(
                    entity_type=entry.entity_type,
                    name=entry.name,
                    parent=entry.parent,
                    entity_id=entry.entity_id,
                    kind=entry.kind,
                    needed_by=(needed_by or {}).get(unit.uid),
                )
            )
    return out


def _conflicts_error(conflicts: Sequence[dict[str, Any]]) -> HTTPException:
    rows = [BranchTransferConflict.model_validate(row).model_dump() for row in conflicts]
    return HTTPException(
        status_code=409,
        detail={"transfer_conflicts": rows, "message": rows[0]["message"]},
    )


def _is_retryable(exc: DBAPIError) -> bool:
    codes = (getattr(exc.orig, "sqlstate", None), getattr(exc.orig, "pgcode", None))
    return any(code in _RETRYABLE_SQLSTATES for code in codes)


# --- the branches ---------------------------------------------------------------


def _check_statuses(source: PlanBranch, target: PlanBranch | None, mode: str) -> None:
    if source.kind == BranchKind.main.value or (
        target is not None and target.kind == BranchKind.main.value
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Changes reach main by merging their branch, and main has no branch changes "
                "to move. Pick two working branches."
            ),
        )
    landed = (BranchStatus.merged.value, BranchStatus.closed.value)
    if target is not None and target.status in landed:
        raise HTTPException(
            status_code=409,
            detail=f"Branch '{target.name}' is {target.status}, so it cannot receive changes.",
        )
    if source.status == BranchStatus.merged.value:
        raise HTTPException(
            status_code=409,
            detail=f"Branch '{source.name}' is merged; its changes are on main already.",
        )
    if mode == "move" and source.status == BranchStatus.closed.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Branch '{source.name}' is closed, so nothing can be taken off it. Copy the "
                "changes instead, or reopen it first."
            ),
        )


async def _refuse_base_mismatch(
    session: AsyncSession,
    project_id: uuid.UUID,
    main_branch_id: uuid.UUID,
    base_source: dict[str, Any],
    base_target: dict[str, Any] | None,
    source: tuple[uuid.UUID, str],
    target: tuple[uuid.UUID, str] | None,
) -> HTTPException:
    """The 409 for two branches cut from different main content, naming whom to update.

    Only here is main read: which side is behind decides what helps. Updating
    the target cannot help a source cut before main last moved.
    """
    main = await build_plan_snapshot(session, project_id, branch_id=main_branch_id)
    source_behind = is_behind(base_source, main)
    target_behind = base_target is not None and is_behind(base_target, main)
    source_id, source_name = source
    behind: list[uuid.UUID]
    if target is None:
        message = (
            f"'{source_name}' was cut from an older main. Run Update from main on "
            f"'{source_name}', then retry."
        )
        behind = [source_id]
    else:
        target_id, target_name = target
        if source_behind and target_behind:
            message = (
                f"'{source_name}' and '{target_name}' were cut from different versions of main. "
                "Run Update from main on both, then retry."
            )
            behind = [source_id, target_id]
        elif source_behind:
            message = (
                f"'{source_name}' was cut from an older main than '{target_name}'. Run Update "
                f"from main on '{source_name}', then retry."
            )
            behind = [source_id]
        else:
            message = (
                f"'{target_name}' was cut from an older main than '{source_name}'. Run Update "
                f"from main on '{target_name}', then retry."
            )
            behind = [target_id]
    return HTTPException(
        status_code=409,
        detail={
            "transfer_base_mismatch": True,
            "message": message,
            "behind_branch_ids": [str(branch_id) for branch_id in behind],
        },
    )


async def _cut_preview_branch(
    session: AsyncSession,
    project_id: uuid.UUID,
    main_branch_id: uuid.UUID,
    main_payload: dict[str, Any],
    user_id: uuid.UUID | None,
) -> uuid.UUID:
    """A branch cut from main now, inside this transaction, for a preview to write on.

    What ``create_branch`` makes — base revision, branch row, deep copy — minus
    its commit: the dry run rolls it back with every other write. The copy
    gives every row main's origin, so the preview pairs rows exactly as a
    branch created a moment later will.
    """
    revision = PlanRevision(
        project_id=project_id,
        created_by=user_id,
        summary="Transfer preview base (never committed)",
        kind=PlanRevisionKind.branch_base.value,
        payload=main_payload,
    )
    session.add(revision)
    await session.flush()
    branch = PlanBranch(
        project_id=project_id,
        name=f"{_PREVIEW_BRANCH_PREFIX}{uuid.uuid4().hex}",
        kind=BranchKind.working.value,
        status=BranchStatus.draft.value,
        description="",
        base_revision_id=revision.id,
        created_by=user_id,
    )
    session.add(branch)
    await session.flush()
    revision.branch_id = branch.id
    await deep_copy_plan_to_branch(
        session, project_id=project_id, source_branch_id=main_branch_id, target_branch_id=branch.id
    )
    await session.flush()
    return branch.id


# --- selection ------------------------------------------------------------------


def _selected_indices(diff: PlanBranchDiff, data: BranchTransferRequest) -> list[int]:
    positions = {id(entry): index for index, entry in enumerate(diff.entries)}
    selected: list[int] = []
    for ref in data.entries:
        entry = find_diff_entry(
            diff,
            BranchRevertRequest(
                entity_type=ref.entity_type,
                name=ref.name,
                parent=ref.parent,
                entity_id=ref.entity_id,
            ),
        )
        if entry.housekeeping:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{entry.name}' is housekeeping ({entry.housekeeping}), not a change of "
                    "this branch's, so there is nothing to move or copy."
                ),
            )
        index = positions[id(entry)]
        if index not in selected:
            selected.append(index)
    return selected


async def _rename_pairs(
    session: AsyncSession,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    entries: Sequence[PlanDiffEntry],
    base_payload: dict[str, Any],
    selected: Sequence[int],
) -> tuple[list[tuple[int, int]], list[dict[str, Any]]]:
    """The rename pairs of the source's diff, by the revert's branch-only rule.

    Not ``PlanBranchDiff.renames``: that is the merge's pairing and consults
    main, and a move undoes the rename on the source the way the revert does
    (``_row_renamed_from``: by origin for an event, by ``source_name`` for
    either). For each removal of a variable or event, the row it was renamed
    into, read by its current name, must be exactly one addition of the same
    type and parent. A row two removals claim, or one the diff shows as a
    change or not at all, is ``ambiguous_rename`` — refused only when the
    selection touches it.
    """
    chosen = set(selected)
    claims: dict[uuid.UUID, tuple[Any, list[int]]] = {}
    for index, entry in enumerate(entries):
        if entry.kind != "removed" or entry.entity_type not in ("variable", "event"):
            continue
        if entry.housekeeping:
            continue
        try:
            row = await row_renamed_from(
                session, project_id, source_id, _request(entry), base_payload
            )
        except HTTPException:
            if index in chosen:
                raise
            continue
        if row is not None:
            claims.setdefault(row.id, (row, []))[1].append(index)
    pairs: list[tuple[int, int]] = []
    ambiguous: list[dict[str, Any]] = []
    for row_id, (row, removed_at) in claims.items():
        first = entries[removed_at[0]]
        halves = [
            index
            for index, entry in enumerate(entries)
            if entry.entity_type == first.entity_type
            and entry.parent == first.parent
            and entry.name == row.name
            and entry.kind != "removed"
        ]
        added = [
            index
            for index in halves
            if entries[index].kind == "added"
            and (entries[index].entity_id is None or entries[index].entity_id == str(row_id))
        ]
        if len(removed_at) == 1 and len(added) == 1 and len(halves) == 1:
            pairs.append((removed_at[0], added[0]))
            continue
        if not chosen & {*removed_at, *halves}:
            continue
        old_names = ", ".join(f"'{entries[index].name}'" for index in removed_at)
        ambiguous.append(
            {
                "entity_type": first.entity_type,
                "name": first.name,
                "parent": first.parent,
                "field": None,
                "reason": "ambiguous_rename",
                "message": (
                    f"{old_names} and '{row.name}' look like one renamed row, but the branch "
                    "cannot tell which changes belong together. Undo the rename on the branch, "
                    "then transfer the rows."
                ),
            }
        )
    return pairs, ambiguous


async def _discussion_conflicts(
    session: AsyncSession, units: Iterable[Unit], source_name: str
) -> list[dict[str, Any]]:
    """Moved additions that carry review comments, which undoing them would delete.

    An added event has no main twin to hand its threads to
    (``rescue_branch_event_threads``), so a move would destroy them. Anchored
    on the event or on one of its photos.
    """
    by_id: dict[uuid.UUID, Unit] = {}
    for unit in units:
        if unit.entity_type == "event" and unit.kind == "added" and unit.item is not None:
            by_id[uuid.UUID(str(unit.item["id"]))] = unit
    if not by_id:
        return []
    photo_event = dict(
        (
            await session.execute(
                select(EventPhoto.id, EventPhoto.event_id).where(
                    EventPhoto.event_id.in_(list(by_id))
                )
            )
        )
        .tuples()
        .all()
    )
    clauses = [EventPhotoComment.event_id.in_(list(by_id))]
    if photo_event:
        clauses.append(EventPhotoComment.photo_id.in_(list(photo_event)))
    rows = (
        await session.execute(
            select(EventPhotoComment.event_id, EventPhotoComment.photo_id).where(or_(*clauses))
        )
    ).all()
    discussed: set[uuid.UUID] = set()
    for event_id, photo_id in rows:
        # Exactly one anchor is set, so ``photo_id`` is there when ``event_id``
        # is not; its check only tells the type checker so.
        owner: uuid.UUID | None = event_id
        if owner is None and photo_id is not None:
            owner = photo_event.get(photo_id)
        if owner is not None:
            discussed.add(owner)
    out: list[dict[str, Any]] = []
    for event_id in discussed:
        unit = by_id[event_id]
        out.append(
            {
                "entity_type": "event",
                "name": unit.label,
                "parent": unit.parent,
                "field": None,
                "reason": "has_discussion",
                "message": (
                    f"'{unit.label}' has review comments. Moving it would delete them, because "
                    "an added event has no main twin to hand them to. Copy it instead (the "
                    f"comments stay on '{source_name}'), or resolve and delete the comments first."
                ),
            }
        )
    return out


def _clash_conflicts(clashes: Iterable[dict[str, Any]], target_name: str) -> list[dict[str, Any]]:
    return [
        {
            "entity_type": clash["entity_type"],
            "name": clash["name"],
            "parent": None,
            "field": None,
            "reason": "identity_clash",
            "message": (
                f"After the transfer two {str(clash['entity_type']).replace('_', ' ')}s on "
                f"'{target_name}' would share '{clash['name']}' (a name or scan identity). "
                "Rename one of them, then retry."
            ),
        }
        for clash in clashes
    ]


# --- the source side of a move ------------------------------------------------------


def _without_photos(entry: PlanDiffEntry) -> PlanDiffEntry | None:
    """``entry`` minus its photo change, which a revert cannot undo; None if nothing is left."""
    kept = [change for change in entry.field_changes if change.field != "photos"]
    if len(kept) == len(entry.field_changes):
        return entry
    if not kept:
        return None
    return entry.model_copy(update={"field_changes": kept})


def _photos_stay(entry: PlanDiffEntry, source_name: str) -> str | None:
    if any(change.field == "photos" for change in entry.field_changes):
        return (
            f"'{entry.name}' keeps its photo changes on '{source_name}': undoing a change does "
            "not restore photos. Remove them there by hand if they should go."
        )
    return None


async def _revert_on_source(
    session: AsyncSession,
    slug: str,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    units: Sequence[Unit],
    base_payload: dict[str, Any],
    source_name: str,
) -> list[str]:
    """Undo every moved unit on the source, the way "Undo" on each row would."""
    warnings: list[str] = []

    async def revert(entry: PlanDiffEntry) -> None:
        if entry.kind == "changed":
            note = _photos_stay(entry, source_name)
            if note:
                warnings.append(note)
            trimmed = _without_photos(entry)
            if trimmed is None:
                return
            entry = trimmed
        await apply_revert_entry(
            session, project_id, source_id, _request(entry), entry, base_payload
        )
        await session.flush()

    for unit in revert_order(units):
        # A rename's added half is the same row as its removed half: only the
        # removal is undone, which moves the name back (and a variable's
        # ``${new}`` back to ``${old}``).
        await revert(unit.entries[0])

    renamed = [unit for unit in units if unit.kind == "renamed"]
    if renamed:
        # The rename arm puts back only the name. Every other key the branch
        # edited on the row now shows as a change under the old name: undone
        # whole, so the source is back at its base for the row.
        diff = await plan_branch_service.diff_branch(session, slug, source_id)
        for unit in renamed:
            old, new = unit.entries
            leftover = next(
                (
                    entry
                    for entry in diff.entries
                    if entry.kind == "changed"
                    and entry.entity_type == old.entity_type
                    and entry.name == old.name
                    and entry.parent == old.parent
                    and (entry.entity_type != "event" or entry.entity_id == new.entity_id)
                ),
                None,
            )
            if leftover is not None:
                await revert(leftover)
    return warnings


# --- after the commit -------------------------------------------------------------


async def _refresh_derived(
    session: AsyncSession, slug: str, project_id: uuid.UUID, branch_ids: Iterable[uuid.UUID]
) -> None:
    for prefix in (cache.prefix_event_types(project_id), cache.prefix_meta_fields(project_id)):
        await cache.delete_prefix(prefix)
    from tripl.services.plan_branch_update_service import _worker_reindexes
    from tripl.services.search_service import _queue_branch_reindex, reindex_project_branch

    for branch_id in branch_ids:
        try:
            queued = _worker_reindexes(session) and await _queue_branch_reindex(
                project_id, branch_id
            )
            if not queued:
                async with AsyncSession(session.bind, expire_on_commit=False) as reindex_session:
                    await reindex_project_branch(
                        reindex_session, project_id=project_id, branch_id=branch_id, slug=slug
                    )
        except Exception:  # noqa: BLE001 — search staleness must never fail a transfer
            logger.exception("Failed to reindex search after a transfer on branch %s", branch_id)


# --- the call ---------------------------------------------------------------------


async def transfer_changes(
    session: AsyncSession,
    slug: str,
    source_id: uuid.UUID,
    data: BranchTransferRequest,
    *,
    user_id: uuid.UUID | None,
) -> TransferOutcome:
    project = await _resolve_project(session, slug)
    project_id = project.id
    if data.target_branch_id == source_id:
        raise HTTPException(
            status_code=400, detail="Pick another branch: a branch cannot receive its own changes."
        )
    held = await hold_branches_for_transfer(
        session, [source_id, *([data.target_branch_id] if data.target_branch_id else [])]
    )
    source = held.get(source_id)
    target = held.get(data.target_branch_id) if data.target_branch_id else None
    if source is None or source.project_id != project_id:
        raise HTTPException(status_code=404, detail="Branch not found")
    if data.target_branch_id is not None and (target is None or target.project_id != project_id):
        raise HTTPException(status_code=404, detail="Target branch not found")
    _check_statuses(source, target, data.mode)

    # Plain locals BEFORE the first write: a rollback expires every ORM
    # state, primary keys included (``revert_change`` explains why).
    mode = data.mode
    dry_run = data.dry_run
    source_name = source.name
    source_origins_complete = source.origin_ids_complete
    target_id: uuid.UUID | None = target.id if target is not None else None
    target_name = target.name if target is not None else None
    target_origins_complete = target.origin_ids_complete if target is not None else True

    base_source = await base_payload_for(session, source)
    main_branch_id = await ensure_main_branch_id(session, project_id)
    main_now: dict[str, Any] | None = None
    if target is not None:
        base_target: dict[str, Any] = await base_payload_for(session, target)
    else:
        main_now = await build_plan_snapshot(session, project_id, branch_id=main_branch_id)
        base_target = with_snapshot_defaults(main_now)
    if compute_plan_diff_entries(base_source, base_target, origins_complete=True):
        raise await _refuse_base_mismatch(
            session,
            project_id,
            main_branch_id,
            base_source,
            base_target if target is not None else None,
            (source_id, source_name),
            (target.id, target.name) if target is not None else None,
        )

    try:
        if target_id is None:
            assert main_now is not None
            target_id = await _cut_preview_branch(
                session, project_id, main_branch_id, main_now, user_id
            )
        shown_target = target_name or "the new branch"

        diff = await plan_branch_service.diff_branch(session, slug, source_id)
        selected_at = _selected_indices(diff, data)
        pairs, ambiguous = await _rename_pairs(
            session, project_id, source_id, diff.entries, base_source, selected_at
        )
        source_payload = await build_plan_snapshot(session, project_id, branch_id=source_id)
        target_payload = await build_plan_snapshot(session, project_id, branch_id=target_id)
        sides = Sides(
            source=source_payload,
            base=base_source,
            target=target_payload,
            source_origins_complete=source_origins_complete,
            target_origins_complete=target_origins_complete,
            target_name=shown_target,
            source_name=source_name,
        )
        units = [
            unit
            for unit in build_units(diff.entries, pairs, sides)
            if not any(entry.housekeeping for entry in unit.entries)
        ]
        chosen = set(selected_at)
        selected = [unit for unit in units if chosen & set(unit.uid)]
        plan = plan_transfer(selected, units, sides, mode=mode)
        if plan.not_transferable:
            raise HTTPException(
                status_code=400,
                detail={
                    "removed_not_transferable": True,
                    "message": " ".join(plan.not_transferable),
                },
            )
        conflicts = [
            *ambiguous,
            *plan.conflicts,
            *_clash_conflicts(identity_clashes(target_payload, plan.ops), shown_target),
        ]
        # A row the target already holds is still taken off the source by a
        # move: the target says the same, so the source stops saying it.
        moved = [
            *plan.applied,
            *(unit for unit, _ in plan.carried),
            *(unit for unit, _ in plan.skipped),
        ]
        if mode == "move":
            conflicts.extend(await _discussion_conflicts(session, moved, source_name))
        if conflicts:
            raise _conflicts_error(conflicts)

        needed_by = {unit.uid: why for unit, why in plan.carried}
        applied_items = _items(plan.applied)
        carried_items = _items((unit for unit, _ in plan.carried), needed_by)
        skipped_items = _items(unit for unit, _ in plan.skipped)
        warnings = list(plan.warnings)

        applier = _TransferApplier(
            session,
            project_id,
            target_id,
            source_payload,
            source_branch_id=source_id,
            target_origins_complete=target_origins_complete,
            source_origins_complete=source_origins_complete,
            target_name=shown_target,
        )
        counts = await _apply(
            session,
            project_id,
            target_id,
            plan.ops,
            source_payload,
            origins_complete=target_origins_complete,
            applier=applier,
        )
        if mode == "move":
            warnings.extend(
                await _revert_on_source(
                    session, slug, project_id, source_id, moved, base_source, source_name
                )
            )
        await session.flush()
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    except _TransferRefusal as exc:
        await session.rollback()
        raise _conflicts_error([exc.conflict]) from exc
    except IntegrityError as exc:
        await session.rollback()
        logger.exception(
            "Transfer from branch %s to %s was rejected by a database constraint",
            source_id,
            target_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "transfer_constraint_violation": True,
                "message": (
                    "The transfer would leave two rows with the same name or the same scan "
                    "identity on one of the branches. Reload both branches and try again; if it "
                    "persists, rename the clashing entity first."
                ),
            },
        ) from exc
    except DBAPIError as exc:
        await session.rollback()
        if not _is_retryable(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail={
                "transfer_retry": True,
                "message": (
                    "Another change to one of these branches landed at the same time. Try again."
                ),
            },
        ) from exc
    except HTTPException:
        await session.rollback()
        raise

    result = BranchTransferResult(
        mode=mode,
        dry_run=dry_run,
        target_branch_id=data.target_branch_id,
        target_branch_name=target_name,
        applied=applied_items,
        carried=carried_items,
        skipped=skipped_items,
        warnings=warnings,
        target_counts=counts,
    )
    if dry_run:
        return TransferOutcome(result, source_name)

    assert data.target_branch_id is not None
    await _refresh_derived(
        session,
        slug,
        project_id,
        [data.target_branch_id, *([source_id] if mode == "move" else [])],
    )
    result.source_diff = await plan_branch_service.diff_branch(session, slug, source_id)
    return TransferOutcome(result, source_name)
