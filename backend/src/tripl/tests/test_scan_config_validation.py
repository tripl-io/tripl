import uuid
from typing import Any, cast

import pytest
from httpx import AsyncClient

from tripl.models.event_type import EventType
from tripl.models.plan_branch import PlanBranch
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.services import scan_service
from tripl.services.version_activation import compile_prerelease_pattern
from tripl.tests.conftest import TestSessionLocal


async def _project(client: AsyncClient, name: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/projects", json={"name": name, "slug": name.lower(), "description": ""}
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


async def _source(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Batch 12 source",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def _scan_payload(source: dict[str, Any], name: str, **extra: object) -> dict[str, Any]:
    return {
        "data_source_id": source["id"],
        "name": name,
        "base_query": "SELECT * FROM events",
        **extra,
    }


@pytest.mark.asyncio
async def test_scan_name_unique_on_shared_source_across_projects_and_patch(
    client: AsyncClient,
) -> None:
    first = await _project(client, "First")
    second = await _project(client, "Second")
    source = await _source(client)
    base_first = f"/api/v1/projects/{first['slug']}/scans"
    base_second = f"/api/v1/projects/{second['slug']}/scans"
    created = await client.post(base_first, json=_scan_payload(source, "existing"))
    assert created.status_code == 201
    duplicate = await client.post(base_second, json=_scan_payload(source, "existing"))
    assert duplicate.status_code == 409
    other = await client.post(base_second, json=_scan_payload(source, "other"))
    assert other.status_code == 201
    renamed = await client.patch(f"{base_second}/{other.json()['id']}", json={"name": "existing"})
    assert renamed.status_code == 409


@pytest.mark.asyncio
async def test_scan_event_type_must_be_main_in_project_on_create_patch_and_dry_run(
    client: AsyncClient,
) -> None:
    project = await _project(client, "Scope")
    source = await _source(client)
    base = f"/api/v1/projects/{project['slug']}/scans"
    other = await _project(client, "Outside")
    outside_type = await client.post(
        f"/api/v1/projects/{other['slug']}/event-types",
        json={"name": "outside", "display_name": "Outside"},
    )
    assert outside_type.status_code == 201
    async with TestSessionLocal() as session:
        branch = PlanBranch(project_id=uuid.UUID(project["id"]), name="Draft")
        session.add(branch)
        await session.flush()
        branch_type = EventType(
            project_id=uuid.UUID(project["id"]),
            branch_id=branch.id,
            name="draft_type",
            display_name="Draft type",
        )
        session.add(branch_type)
        await session.commit()
        branch_type_id = str(branch_type.id)
    for invalid_id in (outside_type.json()["id"], branch_type_id):
        created = await client.post(
            base, json=_scan_payload(source, f"bad-{invalid_id}", event_type_id=invalid_id)
        )
        assert created.status_code == 422
        dry_run = await client.post(
            f"{base}/dry-run",
            json={
                "data_source_id": source["id"],
                "base_query": "SELECT * FROM events",
                "event_type_id": invalid_id,
            },
        )
        assert dry_run.status_code == 422
    valid = await client.post(base, json=_scan_payload(source, "valid"))
    assert valid.status_code == 201
    patched = await client.patch(
        f"{base}/{valid.json()['id']}", json={"event_type_id": branch_type_id}
    )
    assert patched.status_code == 422


@pytest.mark.asyncio
async def test_prerelease_pattern_invalid_on_create_and_patch(client: AsyncClient) -> None:
    project = await _project(client, "Regex")
    source = await _source(client)
    base = f"/api/v1/projects/{project['slug']}/scans"
    invalid = await client.post(
        base, json=_scan_payload(source, "invalid", app_version_prerelease_pattern="(")
    )
    assert invalid.status_code == 422
    valid = await client.post(base, json=_scan_payload(source, "valid"))
    assert valid.status_code == 201
    patched = await client.patch(
        f"{base}/{valid.json()['id']}", json={"app_version_prerelease_pattern": "("}
    )
    assert patched.status_code == 422


def test_legacy_invalid_prerelease_pattern_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert compile_prerelease_pattern("(") is None
    assert "invalid prerelease pattern" in caplog.text.lower()


@pytest.mark.asyncio
async def test_live_job_blocks_replay_and_apply(client: AsyncClient) -> None:
    project = await _project(client, "Jobs")
    source = await _source(client)
    base = f"/api/v1/projects/{project['slug']}/scans"
    created = await client.post(
        base,
        json=_scan_payload(
            source,
            "busy",
            time_column="created_at",
            interval="1h",
            event_group_rules=[
                {"name": "group", "conditions": [{"field": "event_name", "pattern": "^x"}]}
            ],
        ),
    )
    assert created.status_code == 201
    scan_id = created.json()["id"]
    async with TestSessionLocal() as session:
        session.add(ScanJob(scan_config_id=uuid.UUID(scan_id), status=ScanJobStatus.pending.value))
        await session.commit()
    replay = await client.post(
        f"{base}/{scan_id}/metrics/replay",
        json={"time_from": "2026-04-01T00:00:00Z", "time_to": "2026-04-02T00:00:00Z"},
    )
    assert replay.status_code == 409
    applied = await client.post(f"{base}/{scan_id}/event-groups/apply")
    assert applied.status_code == 409


@pytest.mark.asyncio
async def test_cancel_with_celery_task_id_uses_dispatch(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await _project(client, "Cancel")
    source = await _source(client)
    base = f"/api/v1/projects/{project['slug']}/scans"
    created = await client.post(base, json=_scan_payload(source, "cancelled"))
    assert created.status_code == 201
    scan_id = created.json()["id"]
    async with TestSessionLocal() as session:
        job = ScanJob(
            scan_config_id=uuid.UUID(scan_id),
            status=ScanJobStatus.pending.value,
            celery_task_id="celery-task-1",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    calls: list[str] = []

    async def fake_dispatch(callback: object, task_id: str) -> None:
        calls.append(task_id)

    monkeypatch.setattr(scan_service, "dispatch", fake_dispatch)
    response = await client.post(f"{base}/{scan_id}/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert calls == ["celery-task-1"]
