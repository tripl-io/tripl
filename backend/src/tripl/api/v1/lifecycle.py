"""Lifecycle enforcement reads (#258).

Both routes are reads: no write gate, so a viewer member may call them, and the
membership gate mounted in ``api.v1.router`` answers 404 to a non-member.
"""

import uuid

from fastapi import APIRouter

from tripl.api.deps import BranchIdDep, SessionDep
from tripl.schemas.lifecycle import EventMigrationResponse, LifecycleFindingListResponse
from tripl.services import lifecycle_service

router = APIRouter(prefix="/projects/{slug}", tags=["lifecycle"])


@router.get("/lifecycle-findings", response_model=LifecycleFindingListResponse)
async def list_lifecycle_findings(
    session: SessionDep,
    slug: str,
    include_resolved: bool = False,
) -> LifecycleFindingListResponse:
    """Open findings of the daily sunset watch; ``?include_resolved=true`` adds the closed ones."""
    items = await lifecycle_service.list_findings(session, slug, include_resolved=include_resolved)
    return LifecycleFindingListResponse(items=items, total=len(items))


@router.get("/events/{event_id}/migration", response_model=EventMigrationResponse)
async def get_event_migration(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    branch_id: BranchIdDep,
) -> EventMigrationResponse:
    """Successor adoption: the old and the new event's 7-day daily average.

    404 when the event names no successor.
    """
    return await lifecycle_service.event_migration(session, slug, event_id, branch_id)
