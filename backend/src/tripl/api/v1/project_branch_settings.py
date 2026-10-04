from fastapi import APIRouter

from tripl.api.deps import OwnerUserDep, SessionDep
from tripl.models.project_branch_settings import ProjectBranchSettings
from tripl.schemas.project_branch_settings import (
    ProjectBranchSettingsResponse,
    ProjectBranchSettingsUpdate,
)
from tripl.services import audit_service, project_branch_settings_service

router = APIRouter(
    prefix="/projects/{slug}/branch-settings",
    tags=["branch-settings"],
)


@router.get("", response_model=ProjectBranchSettingsResponse)
async def get_project_branch_settings(
    session: SessionDep, slug: str
) -> ProjectBranchSettingsResponse:
    return await project_branch_settings_service.get_project_branch_settings(session, slug)


@router.patch("", response_model=ProjectBranchSettingsResponse)
async def update_project_branch_settings(
    session: SessionDep,
    current_user: OwnerUserDep,
    slug: str,
    data: ProjectBranchSettingsUpdate,
) -> ProjectBranchSettings:
    """Owner-only: min_approvals/block_self_approval govern what editors may
    merge, so editors must not be able to loosen the policy on themselves.

    This protects the merge POLICY, not the plan: editors can still manage
    event-type owners and write ``main`` directly, so the owner-approval gate
    is a branch-review convention rather than an access control
    ."""
    settings = await project_branch_settings_service.update_project_branch_settings(
        session,
        slug,
        data,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="plan_branch_settings.update",
        target_type="project_branch_settings",
        target_id=settings.id,
        target_name=slug,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True, exclude_none=True),
    )
    return settings
