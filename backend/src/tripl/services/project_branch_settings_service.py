import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.project_branch_settings import ProjectBranchSettings
from tripl.schemas.project_branch_settings import (
    ProjectBranchSettingsResponse,
    ProjectBranchSettingsUpdate,
)
from tripl.services._project_settings_rows import get_or_create_project_row
from tripl.services.project_lookup import resolve_project

DEFAULT_MIN_APPROVALS = 1
DEFAULT_BLOCK_SELF_APPROVAL = False


@dataclass(frozen=True)
class BranchMergePolicy:
    """Resolved merge policy for a project — falls back to defaults when the
    project has never persisted a settings row."""

    min_approvals: int = DEFAULT_MIN_APPROVALS
    block_self_approval: bool = DEFAULT_BLOCK_SELF_APPROVAL


async def read_branch_merge_policy(
    session: AsyncSession, project_id: uuid.UUID
) -> BranchMergePolicy:
    """Read-only policy lookup for enforcement paths (approve/merge).

    Unlike ``get_or_create_project_row`` this never writes, so it is safe to
    call inside a caller-owned transaction without committing it.
    """
    settings = await session.scalar(
        select(ProjectBranchSettings).where(ProjectBranchSettings.project_id == project_id)
    )
    if settings is None:
        return BranchMergePolicy()
    return BranchMergePolicy(
        min_approvals=settings.min_approvals,
        block_self_approval=settings.block_self_approval,
    )


async def get_project_branch_settings(
    session: AsyncSession,
    slug: str,
) -> ProjectBranchSettingsResponse:
    """Read-only: projects that never customized the policy get the defaults
    back without a row being written (GETs must not mutate the database)."""
    project = await resolve_project(session, slug)
    settings = await session.scalar(
        select(ProjectBranchSettings).where(ProjectBranchSettings.project_id == project.id)
    )
    if settings is None:
        return ProjectBranchSettingsResponse(
            project_id=project.id,
            min_approvals=DEFAULT_MIN_APPROVALS,
            block_self_approval=DEFAULT_BLOCK_SELF_APPROVAL,
        )
    return ProjectBranchSettingsResponse.model_validate(settings)


async def update_project_branch_settings(
    session: AsyncSession,
    slug: str,
    data: ProjectBranchSettingsUpdate,
) -> ProjectBranchSettings:
    project = await resolve_project(session, slug)
    settings = await get_or_create_project_row(session, ProjectBranchSettings, project.id)
    # exclude_none: an explicit JSON null is not a valid value for either
    # NOT NULL column — treat it the same as omitting the field.
    for key, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(settings, key, value)
    await session.commit()
    await session.refresh(settings)
    return settings
