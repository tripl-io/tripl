"""How a shadow candidate is resolved when the plan already has its event.

An accept that finds the identity taken links the candidate instead of creating
a duplicate. These tests pin what that link tells the person who pressed it,
what it records, which event it links to from a working branch, and that two
resolutions of one candidate cannot both land.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select, update

from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_ACCEPTED,
    SHADOW_STATUS_DISMISSED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.services.reconciliation_service import _resolve_candidate
from tripl.tests.conftest import TestSessionLocal

NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


async def _project(client: AsyncClient, slug: str) -> tuple[uuid.UUID, str, uuid.UUID]:
    """A project with one event type and one scan config: (project, type, config)."""
    await client.post("/api/v1/projects", json={"name": "Shadow", "slug": slug})
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "pv", "display_name": "Page"}
    )
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
        source = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
        )
        session.add(source)
        await session.flush()
        config = ScanConfig(
            project_id=project_id, data_source_id=source.id, name="scan", base_query="SELECT 1"
        )
        session.add(config)
        await session.commit()
        return project_id, event_type.json()["id"], config.id


async def _candidate(
    project_id: uuid.UUID,
    config_id: uuid.UUID,
    type_id: str,
    name: str,
    *,
    status: str = SHADOW_STATUS_NEW,
) -> uuid.UUID:
    async with TestSessionLocal() as session:
        candidate = ShadowEventCandidate(
            project_id=project_id,
            scan_config_id=config_id,
            event_type_id=uuid.UUID(type_id),
            event_name=name,
            observed_count=12,
            first_seen_at=NOW - timedelta(days=2),
            last_seen_at=NOW,
            status=status,
        )
        session.add(candidate)
        await session.commit()
        return candidate.id


async def _event(client: AsyncClient, slug: str, type_id: str, name: str, branch: str = "") -> str:
    query = f"?branch={branch}" if branch else ""
    created = await client.post(
        f"/api/v1/projects/{slug}/events{query}", json={"event_type_id": type_id, "name": name}
    )
    assert created.status_code in (200, 201), created.text
    return str(created.json()["id"])


@pytest.mark.asyncio
async def test_a_linking_accept_says_so_and_is_audited_against_the_candidate(
    client: AsyncClient,
) -> None:
    slug = "shadow-link-audit"
    project_id, type_id, config_id = await _project(client, slug)
    single = await _candidate(project_id, config_id, type_id, "late_name")
    batched = await _candidate(project_id, config_id, type_id, "later_name")
    late_id = await _event(client, slug, type_id, "late_name")
    later_id = await _event(client, slug, type_id, "later_name")
    base = f"/api/v1/projects/{slug}/reconciliation/shadow-events"

    accepted = await client.post(f"{base}/{single}/accept", json={})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["event_id"] == late_id
    assert accepted.json()["linked"] is True

    batch = await client.post(
        f"{base}/batch", json={"action": "accept", "items": [{"candidate_id": str(batched)}]}
    )
    assert batch.status_code == 200, batch.text
    [row] = batch.json()["results"]
    assert (row["ok"], row["linked"], row["event_id"]) == (True, True, later_id)

    async with TestSessionLocal() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.project_slug == slug, AuditLog.action == "shadow_event.link"
                )
            )
        ).scalars()
        by_target = {row.target_id: row for row in rows}
    assert set(by_target) == {single, batched}
    assert by_target[single].target_type == "shadow_event_candidate"
    assert by_target[single].target_name == "late_name"
    assert by_target[single].payload["linked_event_id"] == late_id

    # A person pressed Accept on both, so neither reads as the collector's.
    listed = await client.get(f"{base}?status=accepted")
    assert {item["auto_resolved"] for item in listed.json()["items"]} == {False}


@pytest.mark.asyncio
async def test_a_candidate_the_collector_linked_reads_as_auto_resolved(
    client: AsyncClient,
) -> None:
    slug = "shadow-auto-resolved"
    project_id, type_id, config_id = await _project(client, slug)
    await _candidate(project_id, config_id, type_id, "by_scan", status=SHADOW_STATUS_ACCEPTED)
    await _candidate(project_id, config_id, type_id, "by_hand", status=SHADOW_STATUS_DISMISSED)

    accepted = await client.get(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events?status=accepted"
    )
    dismissed = await client.get(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events?status=dismissed"
    )

    assert [(i["event_name"], i["auto_resolved"]) for i in accepted.json()["items"]] == [
        ("by_scan", True)
    ]
    assert [i["auto_resolved"] for i in dismissed.json()["items"]] == [False]


@pytest.mark.asyncio
async def test_on_a_branch_accept_links_mains_event_and_refuses_a_branch_only_one(
    client: AsyncClient,
) -> None:
    """The collector matches traffic against main, so main's event is the answer.

    An event only the branch has would leave the candidate accepted while main
    still has nothing for its traffic, so that accept is refused until the
    branch is merged.
    """
    slug = "shadow-branch-link"
    project_id, type_id, config_id = await _project(client, slug)
    main_event = await _event(client, slug, type_id, "on_main")
    branch = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "wip"})
    assert branch.status_code == 201, branch.text
    branch_id = branch.json()["id"]
    branch_types = await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
    [branch_type] = branch_types.json()
    await _event(client, slug, branch_type["id"], "branch_only", branch=branch_id)
    on_main = await _candidate(project_id, config_id, type_id, "on_main")
    branch_only = await _candidate(project_id, config_id, type_id, "branch_only")
    base = f"/api/v1/projects/{slug}/reconciliation/shadow-events"

    linked = await client.post(f"{base}/{on_main}/accept?branch={branch_id}", json={})
    refused = await client.post(f"{base}/{branch_only}/accept?branch={branch_id}", json={})

    assert linked.status_code == 200, linked.text
    assert (linked.json()["event_id"], linked.json()["linked"]) == (main_event, True)
    assert refused.status_code == 409
    assert "Merge the branch" in refused.json()["detail"]
    async with TestSessionLocal() as session:
        status = await session.scalar(
            select(ShadowEventCandidate.status).where(ShadowEventCandidate.id == branch_only)
        )
    assert status == SHADOW_STATUS_NEW


@pytest.mark.asyncio
async def test_a_resolution_lands_only_on_a_candidate_still_new(client: AsyncClient) -> None:
    """Both routes read ``new`` before writing; the write itself re-checks it.

    Here the candidate is dismissed elsewhere after this request loaded it,
    which is the window two tabs (or a batch and a click) race through.
    """
    slug = "shadow-claim-race"
    project_id, type_id, config_id = await _project(client, slug)
    candidate_id = await _candidate(project_id, config_id, type_id, "raced")

    async with TestSessionLocal() as session:
        candidate = await session.get_one(ShadowEventCandidate, candidate_id)
        assert candidate.status == SHADOW_STATUS_NEW
        async with TestSessionLocal() as other:
            await other.execute(
                update(ShadowEventCandidate)
                .where(ShadowEventCandidate.id == candidate_id)
                .values(status=SHADOW_STATUS_DISMISSED)
            )
            await other.commit()

        with pytest.raises(HTTPException) as refused:
            await _resolve_candidate(
                session, candidate, status=SHADOW_STATUS_ACCEPTED, user_id=uuid.uuid4()
            )
    assert refused.value.status_code == 409

    async with TestSessionLocal() as session:
        stored = await session.get_one(ShadowEventCandidate, candidate_id)
    assert (stored.status, stored.accepted_event_id) == (SHADOW_STATUS_DISMISSED, None)
