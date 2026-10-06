"""The three snapshots a branch is read against: its base, main now, the branch now.

Shared by "Update from main" and the merge preview, so both read the base the
same way — with ``with_snapshot_defaults`` applied, as the merge reads it — and
refuse an old base in the same words. ``require_complete_base`` is the merge's
own refusal: ``merge_branch`` raises it, and the merge preview raises it too,
so a branch the merge refuses never shows a projection of a merge that cannot
happen.

Imports nothing from the other ``plan_branch_*`` services, which import each
other; anything may import this.
"""

from __future__ import annotations

import uuid
from typing import Any, NamedTuple

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_revision import PlanRevision
from tripl.services.plan_revision_service import (
    PLAN_SNAPSHOT_VERSION,
    build_plan_snapshot,
    with_snapshot_defaults,
)

INCOMPLETE_BASE_MERGE_MESSAGE = (
    "This branch predates the complete merge baseline. "
    "Recreate it from current main before merging."
)


class Sides(NamedTuple):
    base: dict[str, Any] | None
    main: dict[str, Any]
    branch: dict[str, Any]


def base_is_complete(base: dict[str, Any] | None) -> bool:
    """Whether the base is a snapshot an update can read three ways."""
    return base is not None and base.get("snapshot_version") == PLAN_SNAPSHOT_VERSION


def require_complete_base(base: dict[str, Any] | None) -> None:
    """Refuse a branch whose base the merge cannot read three ways (409)."""
    if not base_is_complete(base):
        raise HTTPException(
            status_code=409,
            detail={
                "incomplete_base_snapshot": True,
                "message": INCOMPLETE_BASE_MERGE_MESSAGE,
            },
        )


async def read_base(session: AsyncSession, branch: PlanBranch) -> dict[str, Any] | None:
    """The branch's stored base snapshot, defaults filled; None when it has none."""
    if branch.base_revision_id is None:
        return None
    revision = await session.get(PlanRevision, branch.base_revision_id)
    if revision is None:
        return None
    return with_snapshot_defaults(revision.payload or {})


async def read_sides(
    session: AsyncSession, project_id: uuid.UUID, branch: PlanBranch, main_branch_id: uuid.UUID
) -> Sides:
    return Sides(
        base=await read_base(session, branch),
        main=await build_plan_snapshot(session, project_id, branch_id=main_branch_id),
        branch=await build_plan_snapshot(session, project_id, branch_id=branch.id),
    )
