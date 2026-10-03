"""Planned events (F18, #271): CRUD, tagging, and a planned anomaly raising nothing."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.models.audit_log import AuditLog
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.scan_config import ScanConfig
from tripl.services.planned_event_service import retag_planned_anomalies
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_active_signals_incidents import (
    _anomaly,
    _make_project_with_scan,
    _metric_rows,
)
from tripl.worker.tasks.metrics.signals import _get_latest_active_anomalies


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _window(bucket: datetime, *, before: int = 1, after: int = 1) -> dict[str, str]:
    return {
        "starts_at": _iso(bucket - timedelta(hours=before)),
        "ends_at": _iso(bucket + timedelta(hours=after)),
    }


def _recent_bucket() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


async def _seed_spike(
    slug: str, client: AsyncClient, *, direction: str = "spike"
) -> tuple[str, str, datetime]:
    """A project whose project_total scope holds one fresh, open anomaly."""
    event_type_id, event_id, scan_config_id = await _make_project_with_scan(client, slug)
    bucket = _recent_bucket()
    async with TestSessionLocal() as session:
        for row in _metric_rows(scan_config_id, event_type_id, event_id, bucket):
            session.add(row)
        anomaly = _anomaly(scan_config_id, "project_total", scan_config_id, bucket)
        anomaly.direction = direction
        session.add(anomaly)
        await session.commit()
        anomaly_id = str(anomaly.id)
    return scan_config_id, anomaly_id, bucket


async def _tag_of(anomaly_id: str) -> uuid.UUID | None:
    async with TestSessionLocal() as session:
        anomaly = await session.get(MetricAnomaly, uuid.UUID(anomaly_id))
        assert anomaly is not None
        return anomaly.planned_event_id


async def _signals(client: AsyncClient, slug: str) -> list[dict[str, object]]:
    resp = await client.get(f"/api/v1/projects/{slug}/anomalies/signals")
    assert resp.status_code == 200, resp.text
    return list(resp.json())


async def _alert_candidates(scan_config_id: str) -> dict[tuple[str, str], MetricAnomaly]:
    def _read(session: Session) -> dict[tuple[str, str], MetricAnomaly]:
        config = session.get(ScanConfig, uuid.UUID(scan_config_id))
        assert config is not None
        return _get_latest_active_anomalies(session, config)

    async with TestSessionLocal() as session:
        return await session.run_sync(_read)


@pytest.mark.asyncio
async def test_a_planned_window_hides_the_spike_from_signals_and_alerts(
    client: AsyncClient,
) -> None:
    slug = "planned-hides"
    scan_config_id, anomaly_id, bucket = await _seed_spike(slug, client)
    assert len(await _signals(client, slug)) == 1
    assert len(await _alert_candidates(scan_config_id)) == 1

    resp = await client.post(
        f"/api/v1/projects/{slug}/planned-events",
        json={"label": "Black Friday", "direction": "spike", **_window(bucket)},
    )
    assert resp.status_code == 201, resp.text
    planned = resp.json()

    assert await _tag_of(anomaly_id) == uuid.UUID(planned["id"])
    assert await _signals(client, slug) == []
    assert await _alert_candidates(scan_config_id) == {}

    # Deleting the event makes the spike news again.
    resp = await client.delete(f"/api/v1/projects/{slug}/planned-events/{planned['id']}")
    assert resp.status_code == 204
    assert await _tag_of(anomaly_id) is None
    assert len(await _signals(client, slug)) == 1


@pytest.mark.asyncio
async def test_the_window_end_is_exclusive_and_the_direction_must_match(
    client: AsyncClient,
) -> None:
    slug = "planned-bounds"
    _scan, anomaly_id, bucket = await _seed_spike(slug, client, direction="drop")
    base = f"/api/v1/projects/{slug}/planned-events"

    # Ends exactly on the bucket: the bucket is outside.
    resp = await client.post(base, json={"label": "Before", **_window(bucket, after=0)})
    assert resp.status_code == 201, resp.text
    assert await _tag_of(anomaly_id) is None

    # Covers it, but expects a spike: a drop inside a sale is still news.
    resp = await client.post(base, json={"label": "Sale", "direction": "spike", **_window(bucket)})
    assert resp.status_code == 201, resp.text
    sale_id = resp.json()["id"]
    assert await _tag_of(anomaly_id) is None

    # Editing it to expect either direction tags the drop.
    resp = await client.patch(f"{base}/{sale_id}", json={"direction": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["direction"] is None
    assert await _tag_of(anomaly_id) == uuid.UUID(sale_id)


@pytest.mark.asyncio
async def test_a_scoped_event_tags_only_its_series(client: AsyncClient) -> None:
    slug = "planned-scope"
    scan_config_id, anomaly_id, bucket = await _seed_spike(slug, client)
    base = f"/api/v1/projects/{slug}/planned-events"

    resp = await client.post(
        base,
        json={
            "label": "Other series",
            "scope_type": "event_type",
            "scope_ref": str(uuid.uuid4()),
            **_window(bucket),
        },
    )
    assert resp.status_code == 201, resp.text
    assert await _tag_of(anomaly_id) is None

    resp = await client.post(
        base,
        json={
            "label": "This series",
            "scope_type": "project_total",
            "scope_ref": scan_config_id,
            **_window(bucket),
        },
    )
    assert resp.status_code == 201, resp.text
    assert await _tag_of(anomaly_id) == uuid.UUID(resp.json()["id"])

    # The chart's list for that scope returns it, not the other series' event.
    listed = await client.get(
        base, params={"scope_type": "project_total", "scope_ref": scan_config_id}
    )
    assert [row["label"] for row in listed.json()] == ["This series"]


@pytest.mark.asyncio
async def test_another_projects_event_does_not_tag(client: AsyncClient) -> None:
    _scan, anomaly_id, bucket = await _seed_spike("planned-mine", client)
    await client.post("/api/v1/projects", json={"name": "Other", "slug": "planned-other"})

    resp = await client.post(
        "/api/v1/projects/planned-other/planned-events",
        json={"label": "Elsewhere", **_window(bucket)},
    )
    assert resp.status_code == 201, resp.text
    assert await _tag_of(anomaly_id) is None


@pytest.mark.asyncio
async def test_detection_after_the_event_is_tagged_by_the_retag(client: AsyncClient) -> None:
    """The worker writes the row after the event exists; its retag tags it."""
    slug = "planned-later"
    _type, _event, scan_config_id = await _make_project_with_scan(client, slug)
    bucket = _recent_bucket()
    resp = await client.post(
        f"/api/v1/projects/{slug}/planned-events", json={"label": "Launch", **_window(bucket)}
    )
    planned_id = uuid.UUID(resp.json()["id"])

    async with TestSessionLocal() as session:
        config = await session.get(ScanConfig, uuid.UUID(scan_config_id))
        assert config is not None
        anomaly = _anomaly(scan_config_id, "project_total", scan_config_id, bucket)
        session.add(anomaly)
        await session.flush()

        assert await session.run_sync(retag_planned_anomalies, config.project_id) == 1
        # A second pass changes nothing.
        assert await session.run_sync(retag_planned_anomalies, config.project_id) == 1
        await session.commit()
        await session.refresh(anomaly)
        assert anomaly.planned_event_id == planned_id


@pytest.mark.asyncio
async def test_validation(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "V", "slug": "planned-valid"})
    base = "/api/v1/projects/planned-valid/planned-events"
    now = datetime.now(UTC)

    backwards = {"label": "x", "starts_at": _iso(now), "ends_at": _iso(now - timedelta(hours=1))}
    assert (await client.post(base, json=backwards)).status_code == 422
    half_scope = {"label": "x", "scope_type": "event", **_window(now)}
    assert (await client.post(base, json=half_scope)).status_code == 422
    naive = {"label": "x", "starts_at": "2026-05-01T00:00:00", "ends_at": "2026-05-02T00:00:00"}
    assert (await client.post(base, json=naive)).status_code == 422
    blank = {"label": "   ", **_window(now)}
    assert (await client.post(base, json=blank)).status_code == 422

    created = await client.post(base, json={"label": "ok", **_window(now)})
    assert created.status_code == 201, created.text
    planned_id = created.json()["id"]
    # The merged event is validated: moving only the end before the start fails.
    resp = await client.patch(
        f"{base}/{planned_id}", json={"ends_at": _iso(now - timedelta(hours=5))}
    )
    assert resp.status_code == 422
    assert (await client.delete(f"{base}/{uuid.uuid4()}")).status_code == 404


@pytest.mark.asyncio
async def test_writes_are_audited(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "A", "slug": "planned-audit"})
    base = "/api/v1/projects/planned-audit/planned-events"
    created = await client.post(base, json={"label": "Promo", **_window(datetime.now(UTC))})
    planned_id = created.json()["id"]
    await client.patch(f"{base}/{planned_id}", json={"label": "Promo 2"})
    await client.delete(f"{base}/{planned_id}")

    async with TestSessionLocal() as session:
        actions = set(
            await session.scalars(
                select(AuditLog.action).where(AuditLog.target_type == "planned_event")
            )
        )
    assert actions == {"planned_event.create", "planned_event.update", "planned_event.delete"}


@pytest.mark.asyncio
async def test_the_list_names_the_scoped_series(client: AsyncClient) -> None:
    slug = "planned-names"
    _type, _event, scan_config_id = await _make_project_with_scan(client, slug)
    await client.post(
        f"/api/v1/projects/{slug}/planned-events",
        json={
            "label": "Scoped",
            "scope_type": "project_total",
            "scope_ref": scan_config_id,
            **_window(_recent_bucket()),
        },
    )
    rows = (await client.get(f"/api/v1/projects/{slug}/planned-events")).json()
    assert [row["scope_name"] for row in rows] == ["Production scan"]
