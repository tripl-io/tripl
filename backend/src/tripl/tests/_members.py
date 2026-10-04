"""Project membership helpers shared by the suite.

Non-members do not see a project at all: every ``/projects/{slug}/...`` route
404s for them. A test that has a second, non-owner user act on a project someone
else created therefore has to make that user a member first — these helpers do
it without going through the member-management API, so a test about something
else does not depend on that surface.

Owners and admins of a project's organization (``organization_members``) see
every project of it and need no row; the creator of a project made through
``project_service.create_project`` is already an editor member. There is no
instance role: the organization and project roles are the only ones.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.domain_enums import OrganizationRole, ProjectMemberRole
from tripl.models.organization import DEFAULT_ORG_ID, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User

PASSWORD_HASH_PLACEHOLDER = "x"


async def add_member(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str = "editor",
    *,
    commit: bool = True,
) -> ProjectMember:
    """Make ``user_id`` a member of ``project_id`` with ``role`` (upsert).

    Idempotent: an existing row (a creator, say) has its role replaced rather
    than tripping ``uq_project_member``.
    """
    member_role = ProjectMemberRole(role)
    existing = await session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if existing is not None:
        existing.role = member_role.value
        member = existing
    else:
        member = ProjectMember(project_id=project_id, user_id=user_id, role=member_role.value)
        session.add(member)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return member


async def add_org_member(
    session: AsyncSession,
    user_id: uuid.UUID,
    org_role: str = OrganizationRole.member.value,
    *,
    org_id: uuid.UUID = DEFAULT_ORG_ID,
    commit: bool = True,
) -> OrganizationMember:
    """Make ``user_id`` a member of ``org_id`` with ``org_role`` (upsert)."""
    role = OrganizationRole(org_role).value
    existing = await session.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    if existing is not None:
        existing.role = role
        member = existing
    else:
        member = OrganizationMember(organization_id=org_id, user_id=user_id, role=role)
        session.add(member)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return member


def org_role_for_legacy(role: str) -> OrganizationRole:
    """The organization role a test's instance-era role name stands for.

    ``owner`` -> owner; ``admin`` -> admin; ``editor`` / ``viewer`` / ``member``
    -> member (their write rights live on the project row).
    """
    if role in (OrganizationRole.owner.value, OrganizationRole.admin.value):
        return OrganizationRole(role)
    return OrganizationRole.member


async def add_member_by_slug(slug: str, email: str, role: str = "editor") -> None:
    """Grant membership by project slug and user email, in its own session.

    For HTTP-driven tests that only hold a slug and a registered user's email.
    """
    from tripl.tests.conftest import TestSessionLocal

    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None, f"no project {slug!r}"
        user_id = await session.scalar(select(User.id).where(User.email == email))
        assert user_id is not None, f"no user {email!r}"
        await add_member(session, project_id, user_id, role)


async def remove_member_by_slug(slug: str, email: str) -> None:
    """Drop a membership row directly (no API, no audit)."""
    from tripl.tests.conftest import TestSessionLocal

    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(ProjectMember)
            .join(Project, Project.id == ProjectMember.project_id)
            .join(User, User.id == ProjectMember.user_id)
            .where(Project.slug == slug, User.email == email)
        )
        if row is not None:
            await session.delete(row)
            await session.commit()


async def persisted_member_user(
    project_id: uuid.UUID,
    *,
    role: str = "viewer",
    member_role: str | None = None,
    email: str | None = None,
) -> User:
    """A real ``users`` row in the default organization who is a member of the project.

    For tests that override ``get_current_user`` with a synthetic user: the
    membership gate looks the caller up in ``project_members`` and
    ``organization_members``, so an unsaved ``User(id=uuid4())`` is a non-member
    and every slug route 404s for it.

    ``role`` keeps the instance-era vocabulary the suites were written in (plain
    strings; the instance role itself is gone) and
    is mapped by :func:`org_role_for_legacy`: ``owner`` (or ``admin``) is an
    organization owner (admin), ``editor`` / ``viewer`` an organization member.
    ``member_role`` (the project row) defaults to ``role`` for editor/viewer and
    to ``editor`` for an owner, who needs no row anyway but gets one harmlessly.
    """
    from tripl.tests.conftest import TestSessionLocal

    org_role = org_role_for_legacy(role)
    resolved_member_role = member_role or (
        role
        if role in (ProjectMemberRole.editor.value, ProjectMemberRole.viewer.value)
        else ProjectMemberRole.editor.value
    )
    async with TestSessionLocal() as session:
        user = User(
            id=uuid.uuid4(),
            email=email or f"member-{uuid.uuid4().hex[:10]}@example.com",
            name="Member",
            password_hash=PASSWORD_HASH_PLACEHOLDER,
        )
        session.add(user)
        await session.flush()
        await add_org_member(session, user.id, org_role.value, commit=False)
        await add_member(session, project_id, user.id, resolved_member_role, commit=False)
        await session.commit()
        return user
