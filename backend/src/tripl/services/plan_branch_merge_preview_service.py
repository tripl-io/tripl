"""``GET /branches/{id}/merge-preview/event``: one event as main will hold it after the merge.

Reads the three sides once, asks the merge's own gate and refusals, and hands
the rest to the pure projection in ``_plan_merge_preview``. A module of its
own because ``plan_branch_conflicts`` and ``plan_branch_merge_service`` both
import ``plan_branch_service`` at module level, so the service cannot import
them back.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.plan_branch import BranchStatus
from tripl.schemas.plan_branch import MergedEventPreview
from tripl.services._plan_branch_sides import read_sides, require_complete_base
from tripl.services._plan_merge_preview import (
    AmbiguousEvent,
    EventNotFound,
    EventTarget,
    project_merged_event,
)
from tripl.services.plan_branch_conflicts import (
    _field_conflicts_event_type,
    _load_resolutions,
    is_behind,
    merge_blocking_conflicts,
)
from tripl.services.plan_branch_service import (
    _get_branch,
    _reject_main,
    _resolve_project,
    ensure_main_branch_id,
)

_LANDED = (BranchStatus.merged.value, BranchStatus.closed.value)


async def merge_preview_event(
    session: AsyncSession,
    slug: str,
    branch_id: uuid.UUID,
    *,
    event_id: uuid.UUID | None = None,
    event_type: str | None = None,
    event_name: str | None = None,
) -> MergedEventPreview:
    """The event by any side's id, or by ``(event_type, event_name)`` on any side.

    Refuses as the merge would: 409 ``incomplete_base_snapshot`` with the
    merge's own detail for a base the merge cannot read. 409 ``branch_not_open``
    for a merged or closed branch, where main already holds the answer, and 409
    ``ambiguous_event`` for a namesake the merge cannot place.
    """
    project = await _resolve_project(session, slug)
    branch = await _get_branch(session, project.id, branch_id)
    _reject_main(branch)
    if branch.status in _LANDED:
        raise HTTPException(
            status_code=409,
            detail={
                "branch_not_open": True,
                "message": "This branch has landed; main already holds the result.",
            },
        )
    main_branch_id = await ensure_main_branch_id(session, project.id)
    sides = await read_sides(session, project.id, branch, main_branch_id)
    require_complete_base(sides.base)
    assert sides.base is not None
    base, main, branch_payload = sides.base, sides.main, sides.branch

    blocking = merge_blocking_conflicts(
        base, main, branch_payload, origins_complete=branch.origin_ids_complete
    )
    # The merge's second refusal: an event-type field conflict with no saved
    # choice. Not this event's (it changes no event attribute), but it still
    # stops the whole merge.
    field_conflicts = _field_conflicts_event_type(base, main, branch_payload)
    unresolved = 0
    if field_conflicts:
        resolutions = await _load_resolutions(session, branch.id)
        unresolved = sum(
            1
            for row in field_conflicts
            if (row["entity_type"], row["name"], row["field"]) not in resolutions
        )

    target = (
        EventTarget(id=str(event_id))
        if event_id is not None
        else EventTarget(key=(event_type or "", event_name or ""))
    )
    try:
        projection = project_merged_event(
            base,
            main,
            branch_payload,
            target=target,
            origins_complete=branch.origin_ids_complete,
            blocking=blocking,
        )
    except EventNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AmbiguousEvent as exc:
        raise HTTPException(
            status_code=409, detail={"ambiguous_event": True, "message": str(exc)}
        ) from exc

    return projection.preview.model_copy(
        update={
            "behind_base": is_behind(base, main),
            "branch_merge_blocked": bool(blocking) or unresolved > 0,
            "other_blocking_count": len(blocking) - len(projection.matched_blocking) + unresolved,
        }
    )
