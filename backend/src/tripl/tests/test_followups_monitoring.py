"""Monitoring follow-ups (lane F3): row sparkline series, collection timing on
the drilldown response, and the Events tab's rolling-week totals.

* ``POST /anomalies/signals/series``: a batched per-scope series for the
  Anomalies row sparklines, kept out of the 30 s signals cache.
* L4 — ``last_collected_at`` / ``next_collection_at`` on ``EventMetricsResponse``,
  answered by the scheduler's own pure due-check.
* ``week_total`` / ``prior_week_total`` on the Events tab's series.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.models.event_metric import EventMetric
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.metrics.schedule import (
    scan_config_collection_progress,
    scan_config_collection_schedule,
)
from tripl.worker.tasks.metrics.tasks import METRICS_COLLECTION_MODE

HOUR = timedelta(hours=1)


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def _project_with_scan(client: AsyncClient, slug: str) -> dict[str, str]:
    """Project + event type + event + hourly scan config; returns their ids."""
    created = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert created.status_code == 201, created.text
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "page_view", "display_name": "Page View"},
    )
    assert event_type.status_code == 201, event_type.text
    event = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": event_type.json()["id"],
            "name": "Landing Viewed",
            "status": "implemented",
        },
    )
    assert event.status_code == 201, event.text
    data_source = await client.post(
        "/api/v1/data-sources",
        json={
            "name": f"Warehouse {slug}",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "analytics",
            "username": "default",
            "password": "",
        },
    )
    assert data_source.status_code == 201, data_source.text
    scan = await client.post(
        f"/api/v1/projects/{slug}/scans",
        json={
            "data_source_id": data_source.json()["id"],
            "name": "Production scan",
            "base_query": "SELECT 1",
            "interval": "1h",
            # The dispatcher collects only a config with a time column, so the
            # drilldown reports a next collection only for one that has it.
            "time_column": "event_time",
        },
    )
    assert scan.status_code == 201, scan.text
    return {
        "event_type_id": event_type.json()["id"],
        "event_id": event.json()["id"],
        "scan_config_id": scan.json()["id"],
    }


# --------------------------------------------------------------------------- #
# L4: the scheduler's due-check as pure functions
# --------------------------------------------------------------------------- #


def test_collection_schedule_is_due_now_before_the_first_collection() -> None:
    now = datetime(2026, 9, 26, 12, 20, tzinfo=UTC)
    assert scan_config_collection_schedule(
        last_bucket=None, watermark=None, delta=HOUR, now=now
    ) == (now, True)


def test_collection_schedule_waits_for_the_next_boundary_once_caught_up() -> None:
    now = datetime(2026, 9, 26, 12, 20, tzinfo=UTC)
    # The newest bucket (11:00) ends at the current boundary (12:00): caught up.
    next_at, due = scan_config_collection_schedule(
        last_bucket=datetime(2026, 9, 26, 11, tzinfo=UTC), watermark=None, delta=HOUR, now=now
    )
    assert (next_at, due) == (datetime(2026, 9, 26, 13, tzinfo=UTC), False)


def test_collection_schedule_counts_an_empty_collection_watermark_as_progress() -> None:
    now = datetime(2026, 9, 26, 12, 20, tzinfo=UTC)
    # No bucket stored since 08:00, but a collection completed up to 12:00.
    next_at, due = scan_config_collection_schedule(
        last_bucket=datetime(2026, 9, 26, 8, tzinfo=UTC),
        watermark=datetime(2026, 9, 26, 12, tzinfo=UTC),
        delta=HOUR,
        now=now,
    )
    assert not due
    assert next_at == datetime(2026, 9, 26, 13, tzinfo=UTC)


def test_collection_progress_reads_only_dispatcher_collection_jobs() -> None:
    newest = datetime(2026, 9, 26, 12, 5, tzinfo=UTC)
    jobs: list[tuple[object, datetime | None]] = [
        # A catalog scan and a replay are not progress on the live grid.
        ({"mode": "catalog_scan", "time_to": "2026-09-26T13:00:00Z"}, newest + HOUR),
        (None, newest),
        # Newest collection: completed, but its window stamp is unreadable.
        ({"mode": METRICS_COLLECTION_MODE, "time_to": "garbage"}, newest),
        ({"mode": METRICS_COLLECTION_MODE, "time_to": "2026-09-26T11:00:00Z"}, newest - HOUR),
    ]
    last_collected_at, watermark = scan_config_collection_progress(jobs)
    assert last_collected_at == newest
    assert watermark == datetime(2026, 9, 26, 11, tzinfo=UTC)


def test_collection_progress_is_empty_without_a_collection() -> None:
    assert scan_config_collection_progress([]) == (None, None)


@pytest.mark.asyncio
async def test_project_total_response_carries_last_and_next_collection(
    client: AsyncClient,
) -> None:
    slug = "f3-collection-timing"
    ids = await _project_with_scan(client, slug)
    scan_config_id = uuid.UUID(ids["scan_config_id"])
    now = datetime.now(UTC)
    boundary = now.replace(minute=0, second=0, microsecond=0)
    completed_at = boundary + timedelta(minutes=2)
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                event_id=None,
                event_type_id=uuid.UUID(ids["event_type_id"]),
                bucket=boundary - HOUR,
                count=10,
            )
        )
        session.add(
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                status=ScanJobStatus.completed.value,
                completed_at=completed_at,
                result_summary={
                    "mode": METRICS_COLLECTION_MODE,
                    "time_to": boundary.isoformat(),
                },
            )
        )
        await session.commit()

    response = await client.get(
        f"/api/v1/projects/{slug}/metrics/total",
        params={"scan_config_id": str(scan_config_id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert _parse(body["last_collected_at"]) == completed_at
    # Caught up to the current boundary: the next dispatch is due at the next one.
    assert _parse(body["next_collection_at"]) == boundary + HOUR


@pytest.mark.asyncio
async def test_a_scope_far_behind_the_grid_is_due_now(client: AsyncClient) -> None:
    slug = "f3-collection-due"
    ids = await _project_with_scan(client, slug)
    # Collected once, ten days ago, and never since: far behind the grid.
    async with TestSessionLocal() as session:
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(ids["scan_config_id"]),
                event_id=None,
                event_type_id=uuid.UUID(ids["event_type_id"]),
                bucket=datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
                - timedelta(days=10),
                count=5,
            )
        )
        await session.commit()
    before = datetime.now(UTC)
    response = await client.get(
        f"/api/v1/projects/{slug}/event-types/{ids['event_type_id']}/metrics"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_collected_at"] is None
    assert _parse(body["next_collection_at"]) >= before - timedelta(seconds=1)
    assert _parse(body["next_collection_at"]) <= datetime.now(UTC) + timedelta(seconds=1)


# --------------------------------------------------------------------------- #
# batched sparkline series for open signals
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_signal_series_windows_each_scope_and_fills_interior_gaps(
    client: AsyncClient,
) -> None:
    slug = "f3-signal-series"
    ids = await _project_with_scan(client, slug)
    scan_config_id = uuid.UUID(ids["scan_config_id"])
    event_type_id = uuid.UUID(ids["event_type_id"])
    # Recent: only a bucket an open signal can carry is drawn.
    flagged = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - 12 * HOUR
    async with TestSessionLocal() as session:
        # Outside the window on both sides, then 10:00, (11:00 missing), 12:00, 14:00.
        for offset, count in ((-30, 1), (-2, 10), (0, 90), (2, 12), (10, 1)):
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=None,
                    event_type_id=event_type_id,
                    bucket=flagged + offset * HOUR,
                    count=count,
                )
            )
        await session.commit()

    response = await client.post(
        f"/api/v1/projects/{slug}/anomalies/signals/series",
        json={
            "scopes": [
                {
                    "scan_config_id": str(scan_config_id),
                    "scope_type": "event_type",
                    "scope_ref": str(event_type_id),
                    "bucket": flagged.isoformat(),
                },
                # No scan series behind a catalog metric: omitted, not failed.
                {
                    "scan_config_id": str(scan_config_id),
                    "scope_type": "metric",
                    "scope_ref": str(uuid.uuid4()),
                    "bucket": flagged.isoformat(),
                },
                # A scan outside the project: omitted.
                {
                    "scan_config_id": str(uuid.uuid4()),
                    "scope_type": "event_type",
                    "scope_ref": str(event_type_id),
                    "bucket": flagged.isoformat(),
                },
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 1
    series = body[0]
    assert series["scope_ref"] == str(event_type_id)
    assert series["interval"] == "1h"
    points = [(_parse(point["bucket"]), point["count"]) for point in series["data"]]
    assert points == [
        (flagged - 2 * HOUR, 10),
        (flagged - HOUR, 0),
        (flagged, 90),
        (flagged + HOUR, 0),
        (flagged + 2 * HOUR, 12),
    ]


@pytest.mark.asyncio
async def test_signal_series_sums_the_project_total(client: AsyncClient) -> None:
    slug = "f3-signal-series-total"
    ids = await _project_with_scan(client, slug)
    scan_config_id = uuid.UUID(ids["scan_config_id"])
    # Recent: only a bucket an open signal can carry is drawn.
    flagged = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - 12 * HOUR
    second_type = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "purchase", "display_name": "Purchase"},
    )
    assert second_type.status_code == 201, second_type.text
    async with TestSessionLocal() as session:
        for type_id, count in ((ids["event_type_id"], 30), (second_type.json()["id"], 12)):
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=None,
                    event_type_id=uuid.UUID(type_id),
                    bucket=flagged,
                    count=count,
                )
            )
        # An event-level row is not part of the project total.
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                event_id=uuid.UUID(ids["event_id"]),
                # Event rows carry no type: (config, type, bucket) is unique,
                # and the type row for this bucket is seeded above.
                event_type_id=None,
                bucket=flagged,
                count=500,
            )
        )
        await session.commit()

    response = await client.post(
        f"/api/v1/projects/{slug}/anomalies/signals/series",
        json={
            "scopes": [
                {
                    "scan_config_id": str(scan_config_id),
                    "scope_type": "project_total",
                    "scope_ref": str(scan_config_id),
                    "bucket": flagged.isoformat(),
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    [series] = response.json()
    assert [point["count"] for point in series["data"]] == [42]


@pytest.mark.asyncio
async def test_signal_series_rejects_an_oversized_batch(client: AsyncClient) -> None:
    slug = "f3-signal-series-cap"
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    scope = {
        "scan_config_id": str(uuid.uuid4()),
        "scope_type": "event",
        "scope_ref": str(uuid.uuid4()),
        "bucket": "2026-09-20T12:00:00Z",
    }
    response = await client.post(
        f"/api/v1/projects/{slug}/anomalies/signals/series",
        json={"scopes": [scope] * 501},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# rolling-week totals on the Events tab's series
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_events_series_carries_week_and_prior_week_totals(client: AsyncClient) -> None:
    slug = "f3-week-totals"
    ids = await _project_with_scan(client, slug)
    scan_config_id = uuid.UUID(ids["scan_config_id"])
    week_end = datetime(2026, 9, 26, 0, tzinfo=UTC)
    async with TestSessionLocal() as session:
        # This week: 100 + 4; the week before: 100; three weeks ago: ignored.
        for age, count in (
            (timedelta(hours=1), 100),
            (timedelta(days=6), 4),
            (timedelta(days=8), 100),
            (timedelta(days=20), 999),
        ):
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=uuid.UUID(ids["event_id"]),
                    event_type_id=uuid.UUID(ids["event_type_id"]),
                    bucket=week_end - age,
                    count=count,
                )
            )
        await session.commit()

    # A 3-day chart still reports the full week and the week before it.
    response = await client.get(
        f"/api/v1/projects/{slug}/events-metrics",
        params={
            "from": (week_end - timedelta(days=3)).isoformat(),
            "to": week_end.isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [point["count"] for point in body["data"]] == [100]
    assert body["week_total"] == 104
    assert body["prior_week_total"] == 100
