"""Project membership: who may see (and, with ``editor``, change) one project.

Non-members do not see a project at all; the gate that enforces that lives in
``api.deps`` / ``services.project_access``. This module owns the rows: the
member-management API (list, add, change role, remove) and the grants the
creation paths write — a project's creator becomes an ``editor`` member, and a
demo reset carries the previous members over onto the re-created row.

Managing members is reserved to the owners and admins of the project's
organization (project role ``owner``) and the project's creator, the same
"project manager" set that may rename or re-slug a project. Only members of the
project's organization can be added.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import ProjectMemberRole
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_branch_reviewer import PlanBranchReviewer
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.schemas.project_member import ProjectMemberResponse
from tripl.services import project_access

NOT_A_MEMBER_DETAIL = "User is not a member of this project"


@dataclass(frozen=True)
class MemberGrant:
    """One membership, detached from its project row (demo reset re-grants it)."""

    user_id: uuid.UUID
    role: str
    added_by_user_id: uuid.UUID | None


def is_member_manager(project_role: str | None, user: User, project: Project) -> bool:
    """An owner/admin of the project's organization, or whoever created this project.

    ``project_role`` is the caller's role in ``project`` as
    :func:`tripl.services.project_access.member_role` answers it (``"owner"``
    exactly for owners/admins of the project's own organization).
    """
    return project_role == project_access.OWNER or (
        project.created_by_user_id is not None and project.created_by_user_id == user.id
    )


def require_member_manager(project_role: str | None, user: User, project: Project) -> None:
    if is_member_manager(project_role, user, project):
        return
    raise HTTPException(
        status_code=403,
        detail="Only the project creator or an owner can manage members",
    )


def _serialize(member: ProjectMember, user: User) -> ProjectMemberResponse:
    return ProjectMemberResponse(
        user_id=user.id,
        name=user.name or user.email,
        email=user.email,
        role=ProjectMemberRole(member.role),
        added_at=member.created_at,
    )


async def _get_member(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> ProjectMember | None:
    member: ProjectMember | None = await session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    return member


async def _get_member_or_404(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> ProjectMember:
    member = await _get_member(session, project_id, user_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


async def list_members(session: AsyncSession, project_id: uuid.UUID) -> list[ProjectMemberResponse]:
    rows = await session.execute(
        select(ProjectMember, User)
        .join(User, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at.asc(), User.email.asc())
    )
    return [_serialize(member, user) for member, user in rows.all()]


async def is_member(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Whether ``user_id`` holds a membership row (an organization owner/admin may not)."""
    return await _get_member(session, project_id, user_id) is not None


async def grant_membership(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = ProjectMemberRole.editor.value,
    added_by_user_id: uuid.UUID | None = None,
) -> None:
    """Add ``user_id`` to the project if they are not already in it. No commit.

    Used by the creation paths, which run inside their own transaction; an
    existing row is left alone (its role is not downgraded or upgraded).
    """
    if await _get_member(session, project_id, user_id) is not None:
        return
    session.add(
        ProjectMember(
            project_id=project_id,
            user_id=user_id,
            role=role,
            added_by_user_id=added_by_user_id,
        )
    )
    await session.flush()


async def snapshot_grants(session: AsyncSession, project_id: uuid.UUID) -> list[MemberGrant]:
    """Every membership of the project, as plain values that outlive the row."""
    rows = await session.execute(
        select(ProjectMember.user_id, ProjectMember.role, ProjectMember.added_by_user_id)
        .where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at.asc())
    )
    return [
        MemberGrant(user_id=user_id, role=role, added_by_user_id=added_by)
        for user_id, role, added_by in rows.all()
    ]


async def restore_grants(
    session: AsyncSession, project_id: uuid.UUID, grants: list[MemberGrant]
) -> None:
    """Re-grant a snapshot onto a (re-created) project. No commit."""
    for grant in grants:
        await grant_membership(
            session,
            project_id=project_id,
            user_id=grant.user_id,
            role=grant.role,
            added_by_user_id=grant.added_by_user_id,
        )


async def add_member(
    session: AsyncSession,
    project: Project,
    *,
    user_id: uuid.UUID,
    role: ProjectMemberRole,
    added_by: uuid.UUID | None,
) -> ProjectMemberResponse:
    user = await session.get(User, user_id)
    # Only members of the project's organization can join it. A user of another
    # organization answers the same 404 as an unknown id, so the endpoint is no
    # oracle for which accounts exist elsewhere on the instance.
    if (
        user is None
        or await project_access.org_role_of(session, user_id, project.organization_id) is None
    ):
        raise HTTPException(status_code=404, detail="User not found")
    if await _get_member(session, project.id, user_id) is not None:
        raise HTTPException(status_code=409, detail="User is already a member of this project")
    member = ProjectMember(
        project_id=project.id,
        user_id=user_id,
        role=role.value,
        added_by_user_id=added_by,
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return _serialize(member, user)


async def update_member(
    session: AsyncSession,
    project: Project,
    *,
    user_id: uuid.UUID,
    role: ProjectMemberRole,
) -> tuple[ProjectMemberResponse, str]:
    """Change a member's role. Returns the new state and the previous role."""
    member = await _get_member_or_404(session, project.id, user_id)
    user = await session.get(User, user_id)
    if user is None:  # pragma: no cover - the FK cascades a deleted user's rows away
        raise HTTPException(status_code=404, detail="Member not found")
    previous = member.role
    member.role = role.value
    await session.commit()
    await session.refresh(member)
    return _serialize(member, user), previous


async def remove_member(
    session: AsyncSession, project: Project, *, user_id: uuid.UUID
) -> ProjectMemberResponse:
    """Delete a membership. Returns what was removed, for the audit row.

    Removing the last editor is allowed: the organization's owners always reach
    the project, so it can never become unmanageable.

    In the same transaction, drop the per-project grants the membership carried:
    the user's ownership of this project's event types (which would otherwise
    keep gating merges on an approval they can no longer give) and their
    reviewer assignments on this project's branches. An owner/admin of the
    project's organization keeps them: they still reach the project without the
    row.
    """
    member = await _get_member_or_404(session, project.id, user_id)
    user = await session.get(User, user_id)
    if user is None:  # pragma: no cover - the FK cascades a deleted user's rows away
        raise HTTPException(status_code=404, detail="Member not found")
    removed = _serialize(member, user)
    keeps_access = project_access.is_org_admin_role(
        await project_access.org_role_of(session, user_id, project.organization_id)
    )
    await session.delete(member)
    if not keeps_access:
        await _drop_project_grants(session, project.id, user_id)
    await session.commit()
    return removed


async def _drop_project_grants(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Delete ``user_id``'s event-type ownerships and reviewer seats in a project. No commit."""
    await session.execute(
        delete(EventTypeOwner).where(
            EventTypeOwner.user_id == user_id,
            EventTypeOwner.event_type_id.in_(
                select(EventType.id).where(EventType.project_id == project_id)
            ),
        )
    )
    await session.execute(
        delete(PlanBranchReviewer).where(
            PlanBranchReviewer.user_id == user_id,
            PlanBranchReviewer.branch_id.in_(
                select(PlanBranch.id).where(PlanBranch.project_id == project_id)
            ),
        )
    )
