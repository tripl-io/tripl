"""Project membership is bounded by the project's organization (F20 PR4).

* ``add_member`` only admits members of the project's organization; a user of
  another organization gets the same 404 as an unknown id (no oracle);
* removing an owner/admin of the project's organization from the member list
  keeps their per-project grants (they still reach the project without the
  row); a plain member loses them;
* the member manager is project role ``owner`` (org owner/admin) or the
  project's creator, never an org role borrowed from another organization;
* a membership row held by someone outside the project's organization grants
  nothing and is not listed.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tripl.models.domain_enums import OrganizationRole, ProjectMemberRole
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.services import project_access, project_member_service
from tripl.tests.conftest import TestSessionLocal


async def _org() -> uuid.UUID:
    org_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(Organization(id=org_id, slug=f"org-{org_id.hex[:10]}", name="Org"))
        await session.commit()
    return org_id


async def _user(org_id: uuid.UUID | None, role: OrganizationRole | None) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(User(id=user_id, email=f"u-{user_id.hex[:10]}@example.com", password_hash="x"))
        await session.flush()
        if org_id is not None and role is not None:
            session.add(
                OrganizationMember(organization_id=org_id, user_id=user_id, role=role.value)
            )
        await session.commit()
    return user_id


async def _project(org_id: uuid.UUID, created_by: uuid.UUID | None = None) -> uuid.UUID:
    project_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            Project(
                id=project_id,
                name="Shop",
                slug=f"shop-{project_id.hex[:10]}",
                organization_id=org_id,
                created_by_user_id=created_by,
            )
        )
        await session.commit()
    return project_id


async def _add(project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with TestSessionLocal() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        await project_member_service.add_member(
            session, project, user_id=user_id, role=ProjectMemberRole.editor, added_by=None
        )


async def _owned_type(project_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    type_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            EventType(
                id=type_id, project_id=project_id, name=f"t{type_id.hex[:6]}", display_name="T"
            )
        )
        await session.flush()
        session.add(EventTypeOwner(event_type_id=type_id, user_id=user_id))
        await session.commit()
    return type_id


async def _still_owns(type_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    async with TestSessionLocal() as session:
        row = await session.scalar(
            select(EventTypeOwner.id).where(
                EventTypeOwner.event_type_id == type_id, EventTypeOwner.user_id == user_id
            )
        )
    return row is not None


async def _remove(project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with TestSessionLocal() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        await project_member_service.remove_member(session, project, user_id=user_id)


# ── add_member ──────────────────────────────────────────────────────────────


async def test_add_member_refuses_a_user_of_another_org_with_404() -> None:
    org_a, org_b = await _org(), await _org()
    project_id = await _project(org_a)
    foreigner = await _user(org_b, OrganizationRole.owner)
    homeless = await _user(None, None)

    for user_id in (foreigner, homeless, uuid.uuid4()):
        with pytest.raises(HTTPException) as refused:
            await _add(project_id, user_id)
        # Same status and detail as an unknown id: no oracle across organizations.
        assert refused.value.status_code == 404
        assert refused.value.detail == "User not found"

    async with TestSessionLocal() as session:
        rows = await session.scalars(
            select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)
        )
        assert set(rows.all()) == set()


async def test_add_member_admits_a_member_of_the_projects_org() -> None:
    org_a = await _org()
    project_id = await _project(org_a)
    colleague = await _user(org_a, OrganizationRole.member)

    await _add(project_id, colleague)

    async with TestSessionLocal() as session:
        assert await project_member_service.is_member(session, project_id, colleague)


# ── remove_member and the per-project grants ────────────────────────────────


async def test_removing_an_org_admin_keeps_their_grants() -> None:
    org_a = await _org()
    project_id = await _project(org_a)
    admin = await _user(org_a, OrganizationRole.admin)
    await _add(project_id, admin)
    type_id = await _owned_type(project_id, admin)

    await _remove(project_id, admin)

    assert await _still_owns(type_id, admin)
    async with TestSessionLocal() as session:
        assert not await project_member_service.is_member(session, project_id, admin)
        # Still reaches the project through the organization role.
        assert (
            await project_access._member_role(session, admin, project_id, fenced=False)
            == project_access.OWNER
        )


async def test_removing_a_plain_member_drops_their_grants() -> None:
    org_a = await _org()
    project_id = await _project(org_a)
    member = await _user(org_a, OrganizationRole.member)
    await _add(project_id, member)
    type_id = await _owned_type(project_id, member)

    await _remove(project_id, member)

    assert not await _still_owns(type_id, member)


async def test_an_admin_of_another_org_loses_grants_like_anyone_else() -> None:
    """Grants on org A's project are dropped even if the user administers org B."""
    org_a, org_b = await _org(), await _org()
    project_id = await _project(org_a)
    user_id = await _user(org_a, OrganizationRole.member)
    async with TestSessionLocal() as session:
        session.add(
            OrganizationMember(
                organization_id=org_b, user_id=user_id, role=OrganizationRole.owner.value
            )
        )
        await session.commit()
    await _add(project_id, user_id)
    type_id = await _owned_type(project_id, user_id)

    await _remove(project_id, user_id)

    assert not await _still_owns(type_id, user_id)


# ── a row outside the project's organization ────────────────────────────────


async def _stray_row(project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """A membership row ``add_member`` would refuse: what a project moved to
    another organization, or a removal that missed a row, leaves behind."""
    async with TestSessionLocal() as session:
        session.add(
            ProjectMember(
                project_id=project_id, user_id=user_id, role=ProjectMemberRole.editor.value
            )
        )
        await session.commit()


async def test_a_row_held_outside_the_projects_org_grants_nothing() -> None:
    org_a, org_b = await _org(), await _org()
    project_id = await _project(org_a)
    # An owner of another organization, and someone in no organization at all.
    foreigner = await _user(org_b, OrganizationRole.owner)
    homeless = await _user(None, None)
    colleague = await _user(org_a, OrganizationRole.member)
    for user_id in (foreigner, homeless):
        await _stray_row(project_id, user_id)
    await _add(project_id, colleague)

    async with TestSessionLocal() as session:
        for user_id in (foreigner, homeless):
            assert (
                await project_access._member_role(session, user_id, project_id, fenced=False)
                is None
            )
        assert (
            await project_access._member_role(session, colleague, project_id, fenced=False)
            == project_access.EDITOR
        )
        # The SQL twin, as the fan-outs and lists ask it.
        assert await project_access.members_among(
            session, project_id, [foreigner, homeless, colleague]
        ) == {colleague}
        for user_id in (foreigner, homeless):
            user = await session.get(User, user_id)
            assert user is not None
            assert project_id not in await project_access.member_project_ids(session, user, org_a)


async def test_a_row_held_outside_the_projects_org_is_not_listed() -> None:
    org_a, org_b = await _org(), await _org()
    project_id = await _project(org_a)
    foreigner = await _user(org_b, OrganizationRole.member)
    colleague = await _user(org_a, OrganizationRole.member)
    await _stray_row(project_id, foreigner)
    await _add(project_id, colleague)

    async with TestSessionLocal() as session:
        listed = await project_member_service.list_members(session, project_id)

    # Project settings > Access shows no stranger's name or email.
    assert [member.user_id for member in listed] == [colleague]


# ── who manages members ─────────────────────────────────────────────────────


def test_member_manager_is_project_owner_or_creator() -> None:
    creator = User(id=uuid.uuid4(), email="c@example.com")
    other = User(id=uuid.uuid4(), email="o@example.com")
    project = Project(id=uuid.uuid4(), name="P", slug="p", created_by_user_id=creator.id)
    orphan = Project(id=uuid.uuid4(), name="Q", slug="q", created_by_user_id=None)

    assert project_member_service.is_member_manager(project_access.OWNER, other, project)
    assert project_member_service.is_member_manager(project_access.EDITOR, creator, project)
    assert project_member_service.is_member_manager(None, creator, project)
    assert not project_member_service.is_member_manager(project_access.EDITOR, other, project)
    assert not project_member_service.is_member_manager(project_access.VIEWER, other, project)
    assert not project_member_service.is_member_manager(None, other, orphan)
    assert project_member_service.is_member_manager(project_access.OWNER, other, orphan)

    with pytest.raises(HTTPException) as refused:
        project_member_service.require_member_manager(project_access.EDITOR, other, project)
    assert refused.value.status_code == 403
