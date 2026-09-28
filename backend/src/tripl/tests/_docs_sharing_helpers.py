"""Shared setup for the note sharing tests (F24, GH #308). Not a test module."""

import uuid
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.models.organization_group import OrganizationGroup, OrganizationGroupMember
from tripl.models.user import User
from tripl.tests._docs_helpers import create_project, register
from tripl.tests._members import add_member_by_slug, add_org_member
from tripl.tests.conftest import TestSessionLocal

SLUG = "vault"
BASE = f"/api/v1/projects/{SLUG}/docs"


class Crew:
    """``owner`` is the organization owner (the ``client`` fixture); the rest are members.

    ``alice``, ``bob`` and ``dave`` are project editors, ``carol`` a project
    viewer, ``stranger`` an account outside the project.
    """

    def __init__(self, owner: AsyncClient) -> None:
        self.owner = owner
        self.alice = _client()
        self.bob = _client()
        self.carol = _client()
        self.dave = _client()
        self.stranger = _client()
        self.ids: dict[str, uuid.UUID] = {}

    def others(self) -> list[AsyncClient]:
        return [self.alice, self.bob, self.carol, self.dave, self.stranger]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def user_id(email: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        found = await session.scalar(select(User.id).where(User.email == email))
    assert found is not None, email
    return found


@pytest_asyncio.fixture
async def crew(client: AsyncClient) -> AsyncGenerator[Crew]:
    team = Crew(client)
    await create_project(client, SLUG)
    members = {
        "alice": (team.alice, "editor"),
        "bob": (team.bob, "editor"),
        "carol": (team.carol, "viewer"),
        "dave": (team.dave, "editor"),
    }
    for name, (member, role) in members.items():
        email = f"{name}@example.com"
        await register(member, email, name.title())
        team.ids[name] = await user_id(email)
        async with TestSessionLocal() as session:
            await add_org_member(session, team.ids[name])
        await add_member_by_slug(SLUG, email, role)
    await register(team.stranger, "stranger@example.com", "Stranger")
    team.ids["stranger"] = await user_id("stranger@example.com")
    team.ids["owner"] = await user_id("test@example.com")
    yield team
    for member in team.others():
        await member.aclose()


async def put(
    who: AsyncClient,
    path: str,
    content: str,
    *,
    scope: str = "project",
    expect: int = 200,
) -> dict[str, Any]:
    resp = await who.put(
        f"{BASE}/file", params={"scope": scope, "path": path}, json={"content": content}
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


async def read(
    who: AsyncClient, path: str, *, scope: str = "project", expect: int = 200
) -> dict[str, Any]:
    resp = await who.get(f"{BASE}/file", params={"scope": scope, "path": path})
    assert resp.status_code == expect, resp.text
    return resp.json()


async def share(
    who: AsyncClient,
    path: str,
    visibility: str,
    shares: Sequence[tuple[str, uuid.UUID, str]] = (),
    *,
    scope: str = "project",
    inherited: bool = False,
    folder: bool = False,
    expect: int = 200,
) -> dict[str, Any]:
    kind = "folder" if folder else "file"
    resp = await who.put(
        f"{BASE}/{kind}/sharing",
        params={"scope": scope, "path": path},
        json={
            "visibility": visibility,
            "inherited": inherited,
            "shares": [
                {"principal_type": ptype, "principal_id": str(pid), "permission": permission}
                for ptype, pid, permission in shares
            ],
        },
    )
    assert resp.status_code == expect, resp.text
    return resp.json()


async def tree_paths(who: AsyncClient) -> set[str]:
    resp = await who.get(BASE)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {item["path"] for item in body["project_docs"] + body["organization_docs"]}


async def make_group(name: str, members: list[uuid.UUID]) -> uuid.UUID:
    async with TestSessionLocal() as session:
        group = OrganizationGroup(organization_id=DEFAULT_ORG_ID, name=name)
        session.add(group)
        await session.flush()
        for member in members:
            session.add(OrganizationGroupMember(group_id=group.id, user_id=member))
        await session.commit()
        return group.id


async def leave_group(group_id: uuid.UUID, member: uuid.UUID) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            delete(OrganizationGroupMember).where(
                OrganizationGroupMember.group_id == group_id,
                OrganizationGroupMember.user_id == member,
            )
        )
        await session.commit()


async def audit_rows(action: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return list(
            await session.scalars(
                select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
            )
        )
