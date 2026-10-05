"""What PR4's wider gates must not reach across organizations (F20 PR4, GH #273).

PR4 turned "the instance owner" into "an owner/admin of the bound
organization", so every surface that used to trust the single owner must now be
fenced to the bound organization:

* data sources: another organization's source is "not found" on every route
  (read, update, delete, test, stats, schema) and absent from the list;
* the audit feed: list and detail read only the bound organization's rows.

That hosted sign-up never grants platform admin, whatever
``PLATFORM_ADMIN_EMAILS`` says, is pinned in ``test_hosted_signup.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tripl.main import app
from tripl.models.audit_log import AuditLog
from tripl.models.organization import DEFAULT_ORG_ID, Organization
from tripl.tests._members import add_org_member
from tripl.tests.conftest import TestSessionLocal

PASSWORD = "Password123!"
ACME_ID = uuid.UUID("00000000-0000-0000-0000-00000000fe0c")
ACME_SLUG = "fence-acme"
ACME = f"/api/v1/orgs/{ACME_SLUG}"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _register(client: AsyncClient, name: str) -> uuid.UUID:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{name}@example.com", "password": PASSWORD, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


class Stand:
    """``boss`` owns the default organization; ``xavier`` administers acme."""

    def __init__(self) -> None:
        self.boss = _new_client()
        self.xavier = _new_client()


@pytest.fixture
async def stand() -> AsyncIterator[Stand]:
    s = Stand()
    await _register(s.boss, "fence-boss")
    xavier_id = await _register(s.xavier, "fence-xavier")
    async with TestSessionLocal() as session:
        session.add(Organization(id=ACME_ID, slug=ACME_SLUG, name="Fence Acme"))
        await session.commit()
        await add_org_member(session, xavier_id, "admin", org_id=ACME_ID)
    yield s
    await s.boss.aclose()
    await s.xavier.aclose()


async def _default_org_source(boss: AsyncClient) -> str:
    created = await boss.post(
        "/api/v1/data-sources",
        json={
            "name": "fence-warehouse",
            "db_type": "clickhouse",
            "host": "clickhouse.internal.example.com",
            "port": 9440,
            "database_name": "analytics",
            "username": "tripl_ro",
            "password": "hunter2",
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.mark.asyncio
async def test_another_orgs_admin_cannot_reach_a_data_source(stand: Stand) -> None:
    ds_id = await _default_org_source(stand.boss)

    listed = await stand.xavier.get(f"{ACME}/data-sources")
    assert listed.status_code == 200, listed.text
    assert ds_id not in {row["id"] for row in listed.json()}

    base = f"{ACME}/data-sources/{ds_id}"
    assert (await stand.xavier.get(base)).status_code == 404
    assert (await stand.xavier.get(f"{base}/stats")).status_code == 404
    assert (await stand.xavier.get(f"{base}/schema")).status_code == 404
    assert (await stand.xavier.post(f"{base}/test")).status_code == 404
    assert (await stand.xavier.patch(base, json={"name": "stolen"})).status_code == 404
    assert (await stand.xavier.delete(base)).status_code == 404

    # Still there, unchanged, for its own organization.
    own = await stand.boss.get(f"/api/v1/data-sources/{ds_id}")
    assert own.status_code == 200, own.text
    assert own.json()["name"] == "fence-warehouse"


@pytest.mark.asyncio
async def test_a_project_audit_history_is_per_organization(stand: Stand) -> None:
    """Another organization's admin asking for the same slug in their own
    organization finds no such project, and no entry of it."""
    created = await stand.boss.post("/api/v1/projects", json={"name": "Fenced", "slug": "fenced"})
    assert created.status_code == 201, created.text
    async with TestSessionLocal() as session:
        entry_id = await session.scalar(
            select(AuditLog.id).where(
                AuditLog.organization_id == DEFAULT_ORG_ID,
                AuditLog.project_slug == "fenced",
            )
        )
    assert entry_id is not None

    feed = await stand.xavier.get(f"{ACME}/projects/fenced/audit")
    assert feed.status_code == 404, feed.text
    assert (await stand.xavier.get(f"{ACME}/projects/fenced/audit/{entry_id}")).status_code == 404

    own = await stand.boss.get(f"/api/v1/projects/fenced/audit/{entry_id}")
    assert own.status_code == 200, own.text
