"""Update a plan branch from main: a three-way merge of main INTO the branch (PL-8).

Before this, a branch opened a minute before an unrelated edit to main read
"behind" for the rest of its life, and the only advice the product had was
"recreate the branch" — redo the work. The update brings main's changes onto
the branch instead, and asks the user to pick a value only where both sides
changed the same field: keep the branch's (``theirs``) or take main's
(``ours``). Afterwards the branch's base IS main, so the branch diff shows the
branch's own work and nothing else, and the next merge has nothing to refuse.

What overlaps and what gets written comes from ``_plan_branch_three_way``,
which the conflicts endpoint reads too, so the list the user resolves is the
list the update applies. The writes themselves reuse the revert's snapshot
writers (``plan_branch_revert_service``): the revert writes the base's state
onto branch rows, the update writes main's.

One transaction, in the merge's lock order — the branch FOR UPDATE, then main
— so a merge or a plan write racing the update either lands first and is seen,
or waits. Nothing is written unless every conflict has a choice.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from typing import Any, NamedTuple

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_branch_merge_resolution import PlanBranchMergeResolution
from tripl.models.plan_revision import PlanRevision, PlanRevisionKind
from tripl.schemas.plan_branch import (
    BranchConflictsResponse,
    EntityChangeCount,
    UpdateBlocker,
    UpdateFromMainPreview,
    UpdateFromMainRequest,
    UpdateFromMainResult,
)
from tripl.services._plan_branch_locks import lock_main_plan_for_merge
from tripl.services._plan_branch_three_way import plan_three_way
from tripl.services._plan_branch_three_way_model import ENTITY_TYPES, ThreeWay
from tripl.services._plan_branch_update_apply import apply_update_plan
from tripl.services.plan_branch_conflicts import (
    _load_resolutions,
    conflicts_response,
    is_behind,
    merge_blocked_by,
    upsert_resolution,
)
from tripl.services.plan_branch_merge_service import _lock_branch_for_merge
from tripl.services.plan_branch_service import (
    _DIFF_COUNTED_STATUSES,
    _get_branch,
    _reject_main,
    _resolve_project,
    _to_detail,
    ensure_main_branch_id,
)
from tripl.services.plan_revision_service import (
    PLAN_SNAPSHOT_VERSION,
    build_plan_snapshot,
    plan_snapshot_hash,
    with_snapshot_defaults,
)

logger = logging.getLogger(__name__)

# Module-level so a test can stand in for the writes (the constraint-race test).
_apply = apply_update_plan


class UpdateOutcome(NamedTuple):
    result: UpdateFromMainResult
    # How many conflict rows each choice settled, for the audit record.
    resolution_counts: dict[str, int]


class _Sides(NamedTuple):
    base: dict[str, Any] | None
    main: dict[str, Any]
    branch: dict[str, Any]


def _counts_list(counts: dict[str, dict[str, int]]) -> list[EntityChangeCount]:
    return [
        EntityChangeCount(entity_type=entity_type, **counts[entity_type])
        for entity_type in ENTITY_TYPES
        if entity_type in counts and any(counts[entity_type].values())
    ]


_INCOMPLETE_BASE_MESSAGE = (
    "This branch predates complete merge baselines, so it cannot be "
    "updated from main. Copy your changes to a new branch."
)


def _worker_reindexes(session: AsyncSession) -> bool:
    """A worker serves this database: PostgreSQL, never the SQLite of the tests."""
    return session.bind.dialect.name == "postgresql"


def base_is_complete(base: dict[str, Any] | None) -> bool:
    """Whether the base is a snapshot an update can read three ways."""
    return base is not None and base.get("snapshot_version") == PLAN_SNAPSHOT_VERSION


def _blockers(plan: ThreeWay) -> list[UpdateBlocker]:
    return [UpdateBlocker.model_validate(blocker) for blocker in plan.blockers]


def _main_moved(plan: ThreeWay) -> bool:
    return any(any(counts.values()) for counts in plan.main_changes.values())


async def _read_sides(
    session: AsyncSession, project_id: uuid.UUID, branch: PlanBranch, main_branch_id: uuid.UUID
) -> _Sides:
    base: dict[str, Any] | None = None
    if branch.base_revision_id is not None:
        revision = await session.get(PlanRevision, branch.base_revision_id)
        if revision is not None:
            base = with_snapshot_defaults(revision.payload or {})
    return _Sides(
        base=base,
        main=await build_plan_snapshot(session, project_id, branch_id=main_branch_id),
        branch=await build_plan_snapshot(session, project_id, branch_id=branch.id),
    )


async def preview_update(
    session: AsyncSession, slug: str, branch_id: uuid.UUID
) -> UpdateFromMainPreview:
    """What "Update from main" would bring, and which fields need a choice."""
    project = await _resolve_project(session, slug)
    branch = await _get_branch(session, project.id, branch_id)
    _reject_main(branch)
    main_branch_id = await ensure_main_branch_id(session, project.id)
    sides = await _read_sides(session, project.id, branch, main_branch_id)
    main_hash = plan_snapshot_hash(sides.main)
    if sides.base is None or not base_is_complete(sides.base):
        # POST refuses such a base outright, so the preview says so up front
        # instead of offering choices that can never be applied.
        behind = sides.base is not None and is_behind(sides.base, sides.main)
        return UpdateFromMainPreview(
            behind=behind,
            updatable=False,
            blockers=[
                UpdateBlocker(kind="incomplete_base_snapshot", message=_INCOMPLETE_BASE_MESSAGE)
            ],
            base_revision_id=branch.base_revision_id,
            main_hash=main_hash,
            main_changes=[],
            conflicts=BranchConflictsResponse(
                entities=[], unresolved_count=0, behind=behind, updatable=False
            ),
        )
    stored = {
        key: resolution.choice
        for key, resolution in (await _load_resolutions(session, branch.id)).items()
    }
    plan = plan_three_way(
        sides.base,
        sides.main,
        sides.branch,
        origins_complete=branch.origin_ids_complete,
        resolutions=stored,
    )
    behind = is_behind(sides.base, sides.main)
    blockers = _blockers(plan)
    return UpdateFromMainPreview(
        behind=behind or _main_moved(plan),
        updatable=not blockers,
        blockers=blockers,
        base_revision_id=branch.base_revision_id,
        main_hash=main_hash,
        main_changes=_counts_list(plan.main_changes),
        conflicts=conflicts_response(
            plan.rows,
            stored,
            behind=behind or _main_moved(plan),
            merge_blocked=merge_blocked_by(
                sides.base,
                sides.main,
                sides.branch,
                origins_complete=branch.origin_ids_complete,
            ),
        ),
    )


async def update_from_main(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    data: UpdateFromMainRequest,
    *,
    user_id: uuid.UUID | None,
) -> UpdateOutcome:
    project = await _resolve_project(session, slug)
    project_id = project.id
    branch = await _lock_branch_for_merge(session, project_id, branch_id)
    _reject_main(branch)
    if branch.status not in _DIFF_COUNTED_STATUSES:
        raise HTTPException(
            status_code=409, detail="Branch is merged/closed; there is nothing to update."
        )
    main_branch_id = await ensure_main_branch_id(session, project_id)
    # The merge's lock order: the branch above, main here, both to the commit.
    await lock_main_plan_for_merge(session, main_branch_id)
    sides = await _read_sides(session, project_id, branch, main_branch_id)
    base = sides.base
    if base is None or not base_is_complete(base):
        raise HTTPException(
            status_code=409,
            detail={"incomplete_base_snapshot": True, "message": _INCOMPLETE_BASE_MESSAGE},
        )
    main_hash = plan_snapshot_hash(sides.main)
    if data.expected_main_hash is not None and data.expected_main_hash != main_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "main_moved": True,
                "message": "Main changed again since this update was previewed.",
            },
        )

    # Plain locals BEFORE the first write: a failed flush expires every ORM
    # state, primary keys included (``_commit_merged_plan`` explains why).
    branch_row_id = branch.id
    branch_name = branch.name
    origins_complete = branch.origin_ids_complete
    previous_base_id = branch.base_revision_id

    probe = plan_three_way(base, sides.main, sides.branch, origins_complete=origins_complete)
    if not (is_behind(base, sides.main) or _main_moved(probe)):
        await session.commit()
        return UpdateOutcome(
            UpdateFromMainResult(
                updated=False,
                branch=await _to_detail(session, branch),
                applied=[],
                previous_base_revision_id=previous_base_id,
                base_revision_id=previous_base_id,
            ),
            {},
        )

    try:
        for resolution in data.resolutions:
            await upsert_resolution(session, branch_row_id, resolution, user_id)
        await session.flush()
        # A stored choice names a side, not the values it was made against, so
        # it only counts for a caller who previewed main as it is now (the
        # hash matched above). Without the hash, only this call's choices do:
        # an old "ours" must not settle a field main has changed again since.
        choices: dict[tuple[str, str, str], str] = (
            {
                key: str(resolution.choice)
                for key, resolution in (await _load_resolutions(session, branch_row_id)).items()
            }
            if data.expected_main_hash is not None
            else {
                (r.entity_type, r.entity_name, r.field_name): str(r.choice)
                for r in data.resolutions
            }
        )
    except HTTPException:
        await session.rollback()
        raise
    plan = plan_three_way(
        base, sides.main, sides.branch, origins_complete=origins_complete, resolutions=choices
    )
    if plan.blockers:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "update_blocked": [blocker.model_dump() for blocker in _blockers(plan)],
                "message": plan.blockers[0]["message"],
            },
        )
    if plan.unresolved:
        conflicts = conflicts_response(
            plan.rows,
            choices,
            behind=True,
            merge_blocked=merge_blocked_by(
                base, sides.main, sides.branch, origins_complete=origins_complete
            ),
        )
        # The inline choices go with the refusal: nothing of this call stays.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "unresolved_conflicts": plan.unresolved,
                "conflicts": conflicts.model_dump(mode="json"),
            },
        )

    try:
        applied = await _apply(
            session,
            project_id,
            branch_row_id,
            plan.ops,
            sides.main,
            origins_complete=origins_complete,
        )
        revision = PlanRevision(
            project_id=project_id,
            created_by=user_id,
            summary=f"Base snapshot for branch '{branch_name}' (updated from main)",
            kind=PlanRevisionKind.branch_base.value,
            branch_id=branch_row_id,
            payload=sides.main,
        )
        session.add(revision)
        await session.flush()
        new_base_id = revision.id
        branch.base_revision_id = new_base_id
        # Every stored choice was made against the old base, which is gone.
        await session.execute(
            delete(PlanBranchMergeResolution).where(
                PlanBranchMergeResolution.branch_id == branch_row_id
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        logger.exception(
            "Update of branch %s from main was rejected by a database constraint", branch_row_id
        )
        raise HTTPException(
            status_code=409,
            detail={
                "update_constraint_violation": True,
                "message": (
                    "Updating this branch from main would break a uniqueness rule — most "
                    "often two rows ending up with the same name or the same scan "
                    "identity. Rename the clashing entity on the branch and try again."
                ),
            },
        ) from exc
    except HTTPException:
        await session.rollback()
        raise

    await session.refresh(branch)
    for prefix in (cache.prefix_event_types(project_id), cache.prefix_meta_fields(project_id)):
        await cache.delete_prefix(prefix)
    try:
        from tripl.services.search_service import _queue_branch_reindex, reindex_project_branch

        # The worker rebuilds the branch's search documents: inline, that was
        # two thirds of the call, with the person who resolved the overlaps
        # watching "Updating…". A working branch is searched seconds later at
        # the earliest. Inline only where no worker runs (SQLite) or the broker
        # refused, so the branch never stays stale.
        queued = _worker_reindexes(session) and await _queue_branch_reindex(
            project_id, branch_row_id
        )
        if not queued:
            async with AsyncSession(session.bind, expire_on_commit=False) as reindex_session:
                await reindex_project_branch(
                    reindex_session, project_id=project_id, branch_id=branch_row_id, slug=slug
                )
    except Exception:  # noqa: BLE001 — search staleness must never fail an update
        logger.exception("Failed to reindex search after updating branch %s", branch_row_id)

    choice_counts = Counter(
        choices[(row["entity_type"], row["name"], row["field"])] for row in plan.rows
    )
    return UpdateOutcome(
        UpdateFromMainResult(
            updated=True,
            branch=await _to_detail(session, branch),
            applied=applied,
            previous_base_revision_id=previous_base_id,
            base_revision_id=new_base_id,
        ),
        {"ours": choice_counts.get("ours", 0), "theirs": choice_counts.get("theirs", 0)},
    )
