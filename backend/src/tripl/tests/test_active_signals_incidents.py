"""Active-signal incident de-duplication.

One real spike/drop trips the project total AND its child event_type/event
scopes on the same scan, bucket and direction. That is one incident, but it
used to surface as several stacked active signals. ``get_active_signals`` now
keeps the parent ``project_total`` signal and suppresses the co-firing children
(the per-scope anomaly rows stay in the DB for drill-down).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.tests.conftest import TestSessionLocal


async def _make_project_with_scan(
    client: AsyncClient, slug: str, *, interval: str | None = None
) -> tuple[str, str, str]:
    """Create a project + event_type + event + scan config; return their ids."""
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    event_type_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    event_type_id = event_type_resp.json()["id"]
    event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": event_type_id, "name": "Landing Viewed", "status": "implemented"},
    )
    event_id = event_resp.json()["id"]
    data_source_resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Warehouse",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    scan_body: dict[str, object] = {
        "data_source_id": data_source_resp.json()["id"],
        "name": "Production scan",
        "base_query": "SELECT 1",
    }
    if interval is not None:
        scan_body["interval"] = interval
    scan_resp = await client.post(f"/api/v1/projects/{slug}/scans", json=scan_body)
    scan_config_id = scan_resp.json()["id"]
    return event_type_id, event_id, scan_config_id


def _metric_rows(scan_config_id: str, event_type_id: str, event_id: str, bucket):
    """Two EventMetric rows so all three scopes classify against a live bucket.

    Row A (event_id NULL, event_type_id set) anchors the project_total and
    event_type metric maxes; Row B (event_id set, event_type_id NULL) anchors
    the event metric max. Kept on distinct (event_type_id / event_id) keys so
    the per-scope unique constraints are not violated on the same bucket.
    """
    return [
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=uuid.UUID(scan_config_id),
            event_id=None,
            event_type_id=uuid.UUID(event_type_id),
            bucket=bucket,
            count=480,
        ),
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=uuid.UUID(scan_config_id),
            event_id=uuid.UUID(event_id),
            event_type_id=None,
            bucket=bucket,
            count=480,
        ),
    ]


def _anomaly(scan_config_id, scope_type, scope_ref, bucket, *, event_id=None, event_type_id=None):
    return MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=uuid.UUID(scan_config_id),
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=uuid.UUID(event_id) if event_id else None,
        event_type_id=uuid.UUID(event_type_id) if event_type_id else None,
        bucket=bucket,
        actual_count=480,
        expected_count=120,
        stddev=40,
        z_score=9,
        direction="spike",
        created_at=bucket,
    )


@pytest.mark.asyncio
async def test_project_total_and_child_anomaly_surface_as_one_signal(client: AsyncClient):
    slug = "incident-dedup"
    event_type_id, event_id, scan_config_id = await _make_project_with_scan(client, slug)

    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        for row in _metric_rows(scan_config_id, event_type_id, event_id, bucket):
            session.add(row)
        # Same scan, same bucket, same direction across three scopes = ONE incident.
        session.add(_anomaly(scan_config_id, "project_total", scan_config_id, bucket))
        session.add(
            _anomaly(
                scan_config_id, "event_type", event_type_id, bucket, event_type_id=event_type_id
            )
        )
        session.add(
            _anomaly(
                scan_config_id,
                "event",
                event_id,
                bucket,
                event_id=event_id,
                event_type_id=event_type_id,
            )
        )
        await session.commit()

    # POST variant so the EVENT scope is included alongside project_total/event_type.
    resp = await client.post(
        f"/api/v1/projects/{slug}/anomalies/signals/query",
        json={"event_ids": [event_id]},
    )
    assert resp.status_code == 200
    signals = resp.json()

    # Three stacked anomalies collapse to the single parent project_total signal.
    assert len(signals) == 1, signals
    assert signals[0]["scope_type"] == "project_total"


@pytest.mark.asyncio
async def test_expanded_view_keeps_children_tagged(client: AsyncClient):
    """Expanded AnomaliesPage view lists every flagged scope — project_total,
    event_type AND event — and tags the children folded under the total, rather
    than collapsing the incident into a single project_total row."""
    slug = "incident-expanded"
    event_type_id, event_id, scan_config_id = await _make_project_with_scan(client, slug)

    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        for row in _metric_rows(scan_config_id, event_type_id, event_id, bucket):
            session.add(row)
        # One incident tripping all three scopes on the same scan/bucket/direction.
        session.add(_anomaly(scan_config_id, "project_total", scan_config_id, bucket))
        session.add(
            _anomaly(
                scan_config_id, "event_type", event_type_id, bucket, event_type_id=event_type_id
            )
        )
        session.add(
            _anomaly(
                scan_config_id,
                "event",
                event_id,
                bucket,
                event_id=event_id,
                event_type_id=event_type_id,
            )
        )
        await session.commit()

    # expanded=true adds per-event scope even without an event-id filter and keeps
    # the children (tagged) instead of collapsing them.
    resp = await client.get(f"/api/v1/projects/{slug}/anomalies/signals?expanded=true")
    assert resp.status_code == 200, resp.text
    signals = resp.json()

    by_scope = {signal["scope_type"]: signal for signal in signals}
    assert sorted(by_scope) == ["event", "event_type", "project_total"], signals
    # Parent stays untagged; the co-firing children are kept but flagged.
    assert by_scope["project_total"]["incident_child"] is False
    assert by_scope["event_type"]["incident_child"] is True
    assert by_scope["event"]["incident_child"] is True


@pytest.mark.asyncio
async def test_signals_carry_the_resolved_scope_name(client: AsyncClient):
    """Each signal names its own scope, so a client needs no catalog to label it.

    Without ``scope_name`` the AnomaliesPage had to download the whole event
    catalog (2641 rows / 1.7s on acme-ios) purely to build an id -> name map,
    and rendered "Spike on Event d4c684dd" until it landed — a different name for
    the same incident than the activity rail was showing.
    """
    slug = "signal-scope-names"
    event_type_id, event_id, scan_config_id = await _make_project_with_scan(client, slug)

    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        for row in _metric_rows(scan_config_id, event_type_id, event_id, bucket):
            session.add(row)
        session.add(_anomaly(scan_config_id, "project_total", scan_config_id, bucket))
        session.add(
            _anomaly(
                scan_config_id, "event_type", event_type_id, bucket, event_type_id=event_type_id
            )
        )
        session.add(
            _anomaly(
                scan_config_id,
                "event",
                event_id,
                bucket,
                event_id=event_id,
                event_type_id=event_type_id,
            )
        )
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}/anomalies/signals?expanded=true")
    assert resp.status_code == 200, resp.text
    by_scope = {signal["scope_type"]: signal for signal in resp.json()}

    assert by_scope["event"]["scope_name"] == "Landing Viewed"
    assert by_scope["event_type"]["scope_name"] == "Page View"
    # project_total is named by the project, not by a lookup — there is nothing
    # to resolve, and the client renders its own "Project total" for it.
    assert by_scope["project_total"]["scope_name"] is None


@pytest.mark.asyncio
async def test_deleted_scope_reports_no_name_rather_than_its_ref(client: AsyncClient):
    """An event deleted out from under its anomaly resolves to NULL, not a uuid.

    ``MetricAnomaly.event_id`` is ``ondelete=SET NULL`` while ``scope_ref`` keeps
    the old uuid, so this is the shape a deleted scope actually leaves behind.
    Falling back to ``scope_ref`` would put a hex prefix where a name goes, which
    is the whole defect.
    """
    slug = "signal-orphan-scope"
    event_type_id, event_id, scan_config_id = await _make_project_with_scan(client, slug)

    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        for row in _metric_rows(scan_config_id, event_type_id, event_id, bucket):
            session.add(row)
        session.add(_anomaly(scan_config_id, "event", event_id, bucket, event_id=None))
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}/anomalies/signals?expanded=true")
    assert resp.status_code == 200, resp.text
    signals = [signal for signal in resp.json() if signal["scope_type"] == "event"]

    assert len(signals) == 1, signals
    assert signals[0]["scope_ref"] == event_id
    assert signals[0]["scope_name"] is None


@pytest.mark.asyncio
async def test_children_surface_when_no_parent_project_total(client: AsyncClient):
    """Guard against over-suppression: with no project_total parent flagging the
    same bucket, the event_type and event children each remain a live signal."""
    slug = "incident-no-parent"
    event_type_id, event_id, scan_config_id = await _make_project_with_scan(client, slug)

    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    async with TestSessionLocal() as session:
        for row in _metric_rows(scan_config_id, event_type_id, event_id, bucket):
            session.add(row)
        session.add(
            _anomaly(
                scan_config_id, "event_type", event_type_id, bucket, event_type_id=event_type_id
            )
        )
        session.add(
            _anomaly(
                scan_config_id,
                "event",
                event_id,
                bucket,
                event_id=event_id,
                event_type_id=event_type_id,
            )
        )
        await session.commit()

    resp = await client.post(
        f"/api/v1/projects/{slug}/anomalies/signals/query",
        json={"event_ids": [event_id]},
    )
    assert resp.status_code == 200
    signals = resp.json()

    scope_types = sorted(signal["scope_type"] for signal in signals)
    assert scope_types == ["event", "event_type"], signals


@pytest.mark.asyncio
async def test_daily_scan_stale_anomaly_stays_open_within_interval_horizon(client: AsyncClient):
    """A daily-scan anomaly older than 24h but within 3*interval still classifies open.

    ``get_active_signals`` must pass each scan's interval into
    ``classify_signal_state`` so a long-interval (daily/weekly) scan uses the
    widened ``max(24h, 3*interval)`` freshness horizon. This project_total anomaly
    sits 48h back — stale under the default 24h window but fresh under a 1d scan's
    72h horizon. Without the interval it would be dropped (default 24h → closed).
    """
    slug = "daily-scan-horizon"
    event_type_id, event_id, scan_config_id = await _make_project_with_scan(
        client, slug, interval="1d"
    )

    # 48h old: > 24h (default window) but < 3*1d = 72h (daily-scan horizon).
    bucket = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=48)
    async with TestSessionLocal() as session:
        for row in _metric_rows(scan_config_id, event_type_id, event_id, bucket):
            session.add(row)
        session.add(_anomaly(scan_config_id, "project_total", scan_config_id, bucket))
        await session.commit()

    resp = await client.get(f"/api/v1/projects/{slug}/anomalies/signals")
    assert resp.status_code == 200, resp.text
    signals = resp.json()

    assert len(signals) == 1, signals
    assert signals[0]["scope_type"] == "project_total"
    # Inside the widened horizon the anomaly is an open "latest_scan" signal.
    assert signals[0]["state"] == "latest_scan", signals
