"""Duplicate detection and naming lint routes (GH #265, F12).

``POST /events/duplicate-check`` is a read: no write gate, so a viewer member
and a ``read``-scope API key may call it; it is a POST only to carry up to 500
candidates (listed with the other read-like POSTs in
``tests/test_rbac.READ_LIKE_MUTATING_PATHS``). The membership gate mounted in
``api.v1.router`` answers 404 to a non-member on every route here.

There is deliberately no merge endpoint: "merge" in the UI sets the successor
and deprecates through the existing event update.
"""

from fastapi import APIRouter

from tripl.api.deps import BranchIdDep, EditorUserDep, SessionDep
from tripl.schemas.duplicates import (
    DuplicateCheckRequest,
    DuplicateCheckResponse,
    DuplicateClusterPage,
    DuplicateDismissRequest,
    DuplicateDismissResponse,
)
from tripl.services import audit_service, duplicate_service
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id

# Mounted BEFORE the events router: ``/events/duplicate-check`` must not be
# read as ``/events/{event_id}``.
check_router = APIRouter(prefix="/projects/{slug}/events", tags=["duplicates"])
router = APIRouter(prefix="/projects/{slug}/duplicates", tags=["duplicates"])


@check_router.post("/duplicate-check", response_model=DuplicateCheckResponse)
async def duplicate_check(
    session: SessionDep,
    slug: str,
    data: DuplicateCheckRequest,
    branch_id: BranchIdDep,
) -> DuplicateCheckResponse:
    """Likely duplicates and naming-convention lint for would-be events.

    Up to 500 candidates, answered in order. Changes nothing.
    """
    project_id = await resolve_project_id(session, slug)
    resolved = await resolve_branch_id(session, project_id, branch_id)
    return await duplicate_service.check_candidates(session, project_id, resolved, data.candidates)


@router.get("", response_model=DuplicateClusterPage)
async def list_duplicate_clusters(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    cursor: str | None = None,
) -> DuplicateClusterPage:
    """Clusters of existing events that look like one event spelled several ways."""
    project_id = await resolve_project_id(session, slug)
    resolved = await resolve_branch_id(session, project_id, branch_id)
    return await duplicate_service.list_clusters(session, project_id, resolved, cursor=cursor)


@router.post("/dismiss", response_model=DuplicateDismissResponse)
async def dismiss_duplicate_pair(
    session: SessionDep,
    slug: str,
    data: DuplicateDismissRequest,
    current_user: EditorUserDep,
) -> DuplicateDismissResponse:
    """Mark two events as not duplicates; the pair is never clustered again."""
    project_id = await resolve_project_id(session, slug)
    result = await duplicate_service.dismiss_pair(
        session, project_id, data.event_a_id, data.event_b_id, user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event.duplicate_dismiss",
        target_type="event",
        target_id=result.event_a_id,
        project_slug=slug,
        payload={
            "event_a_id": str(result.event_a_id),
            "event_b_id": str(result.event_b_id),
            "created": result.created,
        },
    )
    return result
