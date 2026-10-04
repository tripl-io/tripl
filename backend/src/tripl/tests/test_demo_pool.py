"""The pre-seeded demo pool: refill, claim, and that a claim leaves nothing behind."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update

from tripl.config import settings
from tripl.models import Base
from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.event_type import EventType
from tripl.models.organization import DEFAULT_ORG_ID, DEMO_POOL_ORG_ID, Organization
from tripl.models.project import Project
from tripl.models.user import User
from tripl.services import demo_pool, org_idle_service
from tripl.tests.conftest import TestSessionLocal

PoolSize = Callable[[int], None]


@pytest.fixture
def pool_of(monkeypatch: pytest.MonkeyPatch) -> PoolSize:
    def _size(n: int) -> None:
        monkeypatch.setattr(settings, "demo_pool_size", n)

    return _size


async def _refill() -> dict[str, int]:
    async with TestSessionLocal() as session:
        return await demo_pool.refill_demo_pool(session)


async def _pool_entries() -> list[Project]:
    async with TestSessionLocal() as session:
        rows = await session.scalars(
            select(Project).where(Project.organization_id == DEMO_POOL_ORG_ID)
        )
        return list(rows.all())


async def _references(target: str, value: uuid.UUID) -> dict[str, int]:
    """Rows per ``table.column`` whose foreign key to ``target`` holds ``value``."""
    counts: dict[str, int] = {}
    async with TestSessionLocal() as session:
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if any(fk.column.table.name == target for fk in column.foreign_keys):
                    n = await session.scalar(
                        select(func.count()).select_from(table).where(column == value)
                    )
                    if n:
                        counts[f"{table.name}.{column.name}"] = n
    return counts


async def _me(client: AsyncClient) -> uuid.UUID:
    return uuid.UUID((await client.get("/api/v1/auth/me")).json()["id"])


@pytest.mark.asyncio
async def test_the_pool_fills_to_its_size_and_no_further(pool_of: PoolSize) -> None:
    pool_of(2)
    assert await _refill() == {"discarded": 0, "started": 2}
    entries = await _pool_entries()
    assert len(entries) == 2
    assert {e.generation_status for e in entries} == {ProjectGenerationStatus.ready.value}

    # Full: a second tick seeds nothing.
    assert await _refill() == {"discarded": 0, "started": 0}


@pytest.mark.asyncio
async def test_an_empty_size_seeds_nothing(pool_of: PoolSize) -> None:
    pool_of(0)
    assert await _refill() == {"discarded": 0, "started": 0}
    assert await _pool_entries() == []


@pytest.mark.asyncio
async def test_a_claim_hands_out_a_ready_demo_at_once(
    client: AsyncClient, pool_of: PoolSize
) -> None:
    pool_of(1)
    await _refill()
    [entry] = await _pool_entries()

    resp = await client.post("/api/v1/projects/demo")

    assert resp.status_code == 202
    body = resp.json()
    assert body["id"] == str(entry.id)
    assert body["generation_status"] == "ready"
    assert body["name"] == "Demo Project"
    assert body["created_by_user_id"] == str(await _me(client))
    # The visitor sees it like any demo of theirs.
    listed = {p["slug"] for p in (await client.get("/api/v1/projects")).json()}
    assert body["slug"] in listed
    events = await client.get(f"/api/v1/projects/{body['slug']}/events")
    assert events.status_code == 200
    assert await _pool_entries() == []


@pytest.mark.asyncio
async def test_a_claim_moves_every_reference_and_leaves_none_behind(
    client: AsyncClient, pool_of: PoolSize
) -> None:
    pool_of(1)
    await _refill()
    [entry] = await _pool_entries()
    assert entry.created_by_user_id is not None
    pool_user = entry.created_by_user_id
    visitor = await _me(client)

    identity = demo_pool._IDENTITY_TABLES
    pool_user_refs = {
        key: n
        for key, n in (await _references("users", pool_user)).items()
        if key.split(".")[0] not in identity
    }
    assert pool_user_refs, "the recipe filed nothing under its creator"
    visitor_before = await _references("users", visitor)

    assert (await client.post("/api/v1/projects/demo")).status_code == 202

    # Every column that named the throwaway user now names the visitor...
    visitor_after = await _references("users", visitor)
    for key, n in pool_user_refs.items():
        assert visitor_after.get(key, 0) == visitor_before.get(key, 0) + n, key
    # ...the throwaway user is gone...
    async with TestSessionLocal() as session:
        assert await session.get(User, pool_user) is None
    # ...and nothing points at the service organization any more.
    assert await _references("organizations", DEMO_POOL_ORG_ID) == {}

    # The demo's audit trail is the visitor organization's now.
    async with TestSessionLocal() as session:
        orgs = set(
            (
                await session.scalars(
                    select(AuditLog.organization_id).where(AuditLog.project_id == entry.id)
                )
            ).all()
        )
    assert orgs == {DEFAULT_ORG_ID}


@pytest.mark.asyncio
async def test_an_empty_pool_falls_back_to_seeding(client: AsyncClient, pool_of: PoolSize) -> None:
    pool_of(1)  # enabled, but never refilled
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 202
    assert resp.json()["generation_status"] == "ready"
    async with TestSessionLocal() as session:
        project = await session.get(Project, uuid.UUID(resp.json()["id"]))
        assert project is not None
        assert project.organization_id == DEFAULT_ORG_ID


@pytest.mark.asyncio
async def test_a_claim_respects_the_per_creator_limit(
    client: AsyncClient, pool_of: PoolSize
) -> None:
    pool_of(4)
    await _refill()
    for _ in range(3):
        assert (await client.post("/api/v1/projects/demo")).status_code == 202
    assert (await client.post("/api/v1/projects/demo")).status_code == 409
    assert len(await _pool_entries()) == 1
    names = sorted(p["name"] for p in (await client.get("/api/v1/projects")).json())
    assert names == ["Demo Project", "Demo Project 2", "Demo Project 3"]


@pytest.mark.asyncio
async def test_stale_entries_are_reseeded(pool_of: PoolSize) -> None:
    pool_of(1)
    await _refill()
    [old] = await _pool_entries()
    async with TestSessionLocal() as session:
        await session.execute(
            update(Project)
            .where(Project.id == old.id)
            .values(
                demo_seeded_at=datetime.now(tz=UTC)
                - timedelta(hours=settings.demo_pool_max_age_hours + 1)
            )
        )
        await session.commit()

    assert await _refill() == {"discarded": 1, "started": 1}
    [fresh] = await _pool_entries()
    assert fresh.id != old.id
    async with TestSessionLocal() as session:
        assert old.created_by_user_id is not None
        assert await session.get(User, old.created_by_user_id) is None
        leftover = await session.scalar(
            select(func.count()).select_from(EventType).where(EventType.project_id == old.id)
        )
    assert leftover == 0


@pytest.mark.asyncio
async def test_a_stale_entry_is_never_handed_out(client: AsyncClient, pool_of: PoolSize) -> None:
    pool_of(1)
    await _refill()
    [old] = await _pool_entries()
    async with TestSessionLocal() as session:
        await session.execute(
            update(Project)
            .where(Project.id == old.id)
            .values(demo_seeded_at=datetime.now(tz=UTC) - timedelta(days=2))
        )
        await session.commit()

    resp = await client.post("/api/v1/projects/demo")
    assert resp.json()["id"] != str(old.id)


@pytest.mark.asyncio
async def test_the_pool_org_is_never_retired_as_idle(
    monkeypatch: pytest.MonkeyPatch, pool_of: PoolSize
) -> None:
    pool_of(1)
    await _refill()
    monkeypatch.setattr(settings, "idle_org_retention_days", 1)
    async with TestSessionLocal() as session:
        await session.execute(
            update(Organization)
            .where(Organization.id == DEMO_POOL_ORG_ID)
            .values(created_at=datetime.now(tz=UTC) - timedelta(days=30))
        )
        await session.commit()
        retired = await org_idle_service.retire_idle_organizations(session)
    assert DEMO_POOL_ORG_ID not in retired
