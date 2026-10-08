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

A row with role ``none`` opts one organization member out of the project: it
wins over the organization's ``default_project_role`` (F20). An owner or admin
of the organization always reaches every project, so a ``none`` row for one is
refused (422) rather than stored as a rule that would not hold.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import ProjectMemberRole
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.organization import OrganizationMember
from tripl.models.plan_branch import PlanBranch
from tripl.models.plan_branch_reviewer import PlanBranchReviewer
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.schemas.project_member import ProjectMemberResponse
from tripl.services import auth_service, project_access

NOT_A_MEMBER_DETAIL = "User is not a member of this project"
ORG_ADMIN_ALWAYS_HAS_ACCESS = (
    "Organization owners and admins always have access to every project; "
    "'none' cannot be set for them"
)


@dataclass(frozen=True)
class MemberGrant:
    """One membership, detached from its project row (demo reset re-grants it)."""

    user_id: uuid.UUID
    role: str
    added_by_user_id: uuid.UUID | None


MEMBER_MANAGER_REQUIRED = "Only the project creator or an owner can manage members"


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
    raise HTTPException(status_code=403, detail=MEMBER_MANAGER_REQUIRED)


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
    """The project's membership rows, for members of its organization only.

    A row held by anyone else grants nothing (``project_access``), and listing
    it would show a stranger's name and email on the project's Access page.
    """
    rows = await session.execute(
        select(ProjectMember, User)
        .join(User, ProjectMember.user_id == User.id)
        .join(Project, Project.id == ProjectMember.project_id)
        .join(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == Project.organization_id,
                OrganizationMember.user_id == ProjectMember.user_id,
            ),
        )
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
    """Re-grant a snapshot onto a (re-created) project. No commit.

    Only to members of the project's organization: a row its holder kept after
    leaving counts for nothing, and copying it would only carry it forward.
    """
    org_id = await session.scalar(select(Project.organization_id).where(Project.id == project_id))
    members = set(
        await session.scalars(
            select(OrganizationMember.user_id).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id.in_([grant.user_id for grant in grants]),
            )
        )
    )
    for grant in grants:
        if grant.user_id not in members:
            continue
        await grant_membership(
            session,
            project_id=project_id,
            user_id=grant.user_id,
            role=grant.role,
            added_by_user_id=grant.added_by_user_id,
        )


def _refuse_no_access_for_org_admin(role: ProjectMemberRole, org_role: str | None) -> None:
    """422 on a ``none`` row for an organization owner/admin: they always have access."""
    if role == ProjectMemberRole.none and project_access.is_org_admin_role(org_role):
        raise HTTPException(status_code=422, detail=ORG_ADMIN_ALWAYS_HAS_ACCESS)


async def _drop_grants_unless_still_member(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    """Drop ``user_id``'s per-project grants once they no longer reach the project.

    Read after the membership change is flushed, through the one access rule
    (``project_access.members_among``): an org owner/admin, or a member the
    organization default still admits, keeps them. No commit.
    """
    await session.flush()
    if user_id not in await project_access.members_among(session, project_id, [user_id]):
        await _drop_project_grants(session, project_id, user_id)


async def add_member(
    session: AsyncSession,
    project: Project,
    *,
    user_id: uuid.UUID,
    role: ProjectMemberRole,
    added_by: uuid.UUID | None,
) -> ProjectMemberResponse:
    # The lock a removal from the organization holds (``org_service.remove_member``),
    # so the membership read below still stands at the insert: a removal landing
    # between the two left a row for someone outside the organization.
    await auth_service.acquire_owner_set_xact_lock(session, project.organization_id)
    # The caller's right to manage members, read again under that lock: a
    # manager removed from the organization while this request waited on it
    # no longer has it.
    # The creator clause alone is not enough: a creator who left holds no role.
    actor = await session.get(User, added_by) if added_by is not None else None
    if actor is not None:
        # Unfenced: the endpoint resolved ``project`` in the request's organization.
        actor_role = await project_access._member_role(session, actor.id, project.id, fenced=False)
        if actor_role is None or not is_member_manager(actor_role, actor, project):
            raise HTTPException(status_code=403, detail=MEMBER_MANAGER_REQUIRED)
    user = await session.get(User, user_id)
    # Only members of the project's organization can join it. A user of another
    # organization answers the same 404 as an unknown id, so the endpoint is no
    # oracle for which accounts exist elsewhere on the instance.
    org_role = (
        None
        if user is None
        else await project_access.org_role_of(session, user_id, project.organization_id)
    )
    if user is None or org_role is None:
        raise HTTPException(status_code=404, detail="User not found")
    _refuse_no_access_for_org_admin(role, org_role)
    if await _get_member(session, project.id, user_id) is not None:
        raise HTTPException(status_code=409, detail="User is already a member of this project")
    member = ProjectMember(
        project_id=project.id,
        user_id=user_id,
        role=role.value,
        added_by_user_id=added_by,
    )
    session.add(member)
    if role == ProjectMemberRole.none:
        # An opt-out: whatever the default gave them in this project goes too.
        await _drop_grants_unless_still_member(session, project.id, user_id)
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
    """Change a member's role. Returns the new state and the previous role.

    Moving a member to ``none`` takes the project away from them, with the
    event-type ownerships and reviewer seats they held in it.
    """
    member = await _get_member_or_404(session, project.id, user_id)
    user = await session.get(User, user_id)
    if user is None:  # pragma: no cover - the FK cascades a deleted user's rows away
        raise HTTPException(status_code=404, detail="Member not found")
    _refuse_no_access_for_org_admin(
        role, await project_access.org_role_of(session, user_id, project.organization_id)
    )
    previous = str(member.role)
    member.role = role.value
    if role == ProjectMemberRole.none:
        await _drop_grants_unless_still_member(session, project.id, user_id)
    await session.commit()
    await session.refresh(member)
    return _serialize(member, user), previous


async def remove_member(
    session: AsyncSession, project: Project, *, user_id: uuid.UUID
) -> ProjectMemberResponse:
    """Delete a membership. Returns what was removed, for the audit row.

    Removing the last editor is allowed: the organization's owners always reach
    the project, so it can never become unmanageable. Removing a row puts the
    member back on the organization's ``default_project_role``: removing a
    ``none`` row may give them access, removing an ``editor`` row under an
    ``editor`` default changes nothing. To take a member out of a project whose
    organization default admits them, set ``none`` instead.

    In the same transaction, drop the per-project grants the membership carried
    if the user no longer reaches the project: their ownership of this
    project's event types (which would otherwise keep gating merges on an
    approval they can no longer give) and their reviewer assignments on this
    project's branches. An owner/admin of the project's organization, or a
    member the organization default admits, keeps them.
    """
    member = await _get_member_or_404(session, project.id, user_id)
    user = await session.get(User, user_id)
    if user is None:  # pragma: no cover - the FK cascades a deleted user's rows away
        raise HTTPException(status_code=404, detail="Member not found")
    removed = _serialize(member, user)
    await session.delete(member)
    await _drop_grants_unless_still_member(session, project.id, user_id)
    await session.commit()
    return removed


async def drop_grants_in_projects(
    session: AsyncSession, project_ids: list[uuid.UUID], user_id: uuid.UUID
) -> None:
    """:func:`_drop_project_grants` over several projects: a member leaving an organization.

    No commit. Used by ``org_service.remove_member`` (critique #28), which takes
    the user out of every project of the organization at once.
    """
    for project_id in project_ids:
        await _drop_project_grants(session, project_id, user_id)


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
