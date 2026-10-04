"""Demo integration + zero-network-egress capstone.

Consolidates the cross-cutting guarantees: provisioning rollback, full cleanup,
cache coherence, repeated-create safety, and — the security capstone — that a full
provision makes ZERO outbound network calls. Per-feature depth (two runtime ticks,
retention, metric collection, alert dispatch no-network) lives in the task suites
(test_demo_runtime / test_demo_metric_collection / test_demo_alert_sink).
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.data_source import DataSource
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.services import demo_service
from tripl.tests.conftest import TestSessionLocal


async def _slugs_in_list(client: AsyncClient) -> set[str]:
    resp = await client.get("/api/v1/projects")
    assert resp.status_code == 200
    return {p["slug"] for p in resp.json()}


@pytest.mark.asyncio
async def test_provisioning_rollback_leaves_nothing_visible(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected seed failure")

    monkeypatch.setattr(demo_service, "_seed_demo_content", _boom)
    assert (await client.post("/api/v1/projects/demo")).status_code == 500

    # Not in the normal list, and no seeded content leaked.
    assert await _slugs_in_list(client) == set()
    async with TestSessionLocal() as session:
        demos = (
            (await session.execute(select(Project).where(Project.is_demo.is_(True))))
            .scalars()
            .all()
        )
        assert len(demos) == 1 and demos[0].generation_status == "failed"
        event_types = (
            (await session.execute(select(EventType).where(EventType.project_id == demos[0].id)))
            .scalars()
            .all()
        )
        assert event_types == []


@pytest.mark.asyncio
async def test_delete_cleans_owned_source_and_spares_real(client: AsyncClient) -> None:
    real_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            DataSource(
                id=real_id,
                project_id=None,
                name="Real warehouse",
                db_type="clickhouse",
                host="warehouse.example.com",
                port=8123,
                database_name="analytics",
            )
        )
        await session.commit()

    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    assert (await client.delete(f"/api/v1/projects/demo/{slug}")).status_code == 204

    async with TestSessionLocal() as session:
        assert (
            await session.execute(select(Project).where(Project.slug == slug))
        ).scalar_one_or_none() is None
        assert (await session.get(DataSource, real_id)) is not None
        synthetic = (
            (await session.execute(select(DataSource).where(DataSource.db_type == "synthetic")))
            .scalars()
            .all()
        )
        assert synthetic == []


@pytest.mark.asyncio
async def test_cache_coherence_across_create_and_delete(client: AsyncClient) -> None:
    # Prime the (cached) list, then create — the list must reflect the new demo,
    # proving create invalidated the cache. Delete must drop it again.
    await _slugs_in_list(client)
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    assert slug in await _slugs_in_list(client)

    await client.delete(f"/api/v1/projects/demo/{slug}")
    assert slug not in await _slugs_in_list(client)


@pytest.mark.asyncio
async def test_repeated_creates_are_distinct_and_uncorrupted(client: AsyncClient) -> None:
    first = (await client.post("/api/v1/projects/demo")).json()
    second = (await client.post("/api/v1/projects/demo")).json()
    assert first["slug"] != second["slug"]
    assert first["generation_status"] == second["generation_status"] == "ready"
    listed = await _slugs_in_list(client)
    assert {first["slug"], second["slug"]} <= listed


@pytest.mark.asyncio
async def test_demo_enabled_flag_disables_only_demo_provisioning(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tripl.config import settings

    monkeypatch.setattr(settings, "demo_enabled", False)
    # The rollback kill switch blocks demo provisioning...
    assert (await client.post("/api/v1/projects/demo")).status_code == 403
    # ...and never touches real projects: create/delete still work.
    created = await client.post(
        "/api/v1/projects", json={"name": "Real", "slug": "real-flag", "description": ""}
    )
    assert created.status_code == 201
    assert (await client.delete("/api/v1/projects/real-flag")).status_code == 204


@pytest.mark.asyncio
async def test_provisioning_makes_zero_network_egress(deny_network: None) -> None:
    # Capstone: a full provision (synthetic warehouse, real detector/PSI, catalog
    # collection, local alert sink) runs entirely in-process. The tripwire raises
    # on ANY socket/httpx/smtplib use; the in-memory SQLite opens no socket, so a
    # clean run proves the demo stack never reaches the network. Called directly
    # (not via the ASGI client, which itself rides httpx) so only genuine egress trips.
    async with TestSessionLocal() as session:
        response = await demo_service.create_demo_project(session)
    assert response.is_demo is True
    assert response.generation_status.value == "ready"
