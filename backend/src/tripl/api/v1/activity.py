from typing import Annotated

from fastapi import APIRouter, Query

from tripl.api.deps import CurrentUserDep, SessionDep
from tripl.schemas.activity import ActivityItemResponse
from tripl.services import activity_service
from tripl.services.project_access import member_project_ids

router = APIRouter(tags=["activity"])

ActivityLimit = Annotated[int, Query(ge=1, le=100)]


@router.get("/activity", response_model=list[ActivityItemResponse])
async def list_workspace_activity(
    session: SessionDep,
    current_user: CurrentUserDep,
    limit: ActivityLimit = 20,
) -> list[ActivityItemResponse]:
    """The workspace rail: activity from the projects the caller is a member of."""
    return await activity_service.list_activity(
        session,
        visible_project_ids=await member_project_ids(session, current_user),
        limit=limit,
    )


@router.get("/activity/projects/{slug}", response_model=list[ActivityItemResponse])
async def list_project_activity(
    session: SessionDep,
    current_user: CurrentUserDep,
    slug: str,
    limit: ActivityLimit = 20,
) -> list[ActivityItemResponse]:
    """One project's rail; a non-member gets "Project not found"."""
    return await activity_service.list_activity(
        session,
        visible_project_ids=await member_project_ids(session, current_user),
        slug=slug,
        limit=limit,
    )
