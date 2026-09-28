"""A small world for the platform console suites (F20 PR14). Not a test module.

Self-hosted, as the suite runs by default:

* ``operator`` — the first account: owner of the default organization and the
  instance's platform admin;
* ``alice`` — owner of ``globex``, a second organization with one project
  (``shop``, created through the org-qualified API);
* ``bob`` — a plain member of the default organization, and of nothing else.

Names and emails are synthetic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
from tripl.models.organization import Organization, OrganizationMember
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"
API = "/api/v1"
GLOBEX_ID = uuid.UUID("00000000-0000-0000-0000-0000006c0be4")
GLOBEX = "globex"
SHOP = "shop"


def new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def register(client: AsyncClient, name: str) -> uuid.UUID:
    resp = await client.post(
        f"{API}/auth/register",
        json={"email": f"{name}@example.com", "password": PASSWORD, "name": name.title()},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def add_org(org_id: uuid.UUID, slug: str, name: str, owner_id: uuid.UUID) -> None:
    async with TestSessionLocal() as session:
        session.add(Organization(id=org_id, slug=slug, name=name))
        await session.flush()
        session.add(
            OrganizationMember(
                organization_id=org_id, user_id=owner_id, role=OrganizationRole.owner.value
            )
        )
        await session.commit()


async def set_org_status(org_id: uuid.UUID, org_status: OrganizationStatus) -> None:
    async with TestSessionLocal() as session:
        await session.execute(
            update(Organization).where(Organization.id == org_id).values(status=org_status.value)
        )
        await session.commit()


async def audit_rows(action: str) -> list[AuditLog]:
    async with TestSessionLocal() as session:
        return list(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
                )
            ).all()
        )


@dataclass
class World:
    operator: AsyncClient = field(default_factory=new_client)
    alice: AsyncClient = field(default_factory=new_client)
    bob: AsyncClient = field(default_factory=new_client)
    operator_id: uuid.UUID = field(default_factory=uuid.uuid4)
    alice_id: uuid.UUID = field(default_factory=uuid.uuid4)
    bob_id: uuid.UUID = field(default_factory=uuid.uuid4)

    async def close(self) -> None:
        for client in (self.operator, self.alice, self.bob):
            await client.aclose()


async def build_world() -> World:
    world = World()
    world.operator_id = await register(world.operator, "operator")
    world.alice_id = await register(world.alice, "alice")
    world.bob_id = await register(world.bob, "bob")
    await add_org(GLOBEX_ID, GLOBEX, "Globex", world.alice_id)
    created = await world.alice.post(
        f"{API}/orgs/{GLOBEX}/projects", json={"name": "Shop", "slug": SHOP}
    )
    assert created.status_code == 201, created.text
    return world
