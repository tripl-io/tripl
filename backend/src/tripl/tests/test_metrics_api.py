import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest
from httpx import AsyncClient

from tripl.models.data_source import DataSource
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.release_regression import ReleaseComparability, ReleaseRegression
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_project_lookup_perf import captured_sql


class EventMetricSeedRow(TypedDict):
    event_id: str
    counts: list[int]


def _plain_point(bucket: str, count: int) -> dict[str, object]:
    return {
        "bucket": bucket,
        "count": count,
        "expected_count": None,
        "stddev": None,
        "detector_kind": None,
        "is_anomaly": False,
        "anomaly_direction": None,
        "z_score": None,
    }


async def _setup_metrics_project(
    client: AsyncClient,
    slug: str = "metrics-api",
) -> dict[str, str]:
    project_resp = await client.post(
        "/api/v1/projects",
        json={"name": "Metrics API", "slug": slug},
    )
    project_id = project_resp.json()["id"]

    event_types: list[tuple[str, str]] = []
    for name, display_name in [("page", "Page"), ("track", "Track")]:
        et_resp = await client.post(
            f"/api/v1/projects/{slug}/event-types",
            json={"name": name, "display_name": display_name},
        )
        et_id = et_resp.json()["id"]
        field_resp = await client.post(
            f"/api/v1/projects/{slug}/event-types/{et_id}/fields",
            json={
                "name": "screen",
                "display_name": "Screen",
                "field_type": "string",
                "is_required": True,
            },
        )
        event_types.append((et_id, field_resp.json()["id"]))

    return {
        "project_id": project_id,
        "page_type_id": event_types[0][0],
        "page_field_id": event_types[0][1],
        "track_type_id": event_types[1][0],
        "track_field_id": event_types[1][1],
    }


async def _seed_group_metrics(
    project_id: str,
    event_rows: list[EventMetricSeedRow],
    *,
    name: str = "Metrics Config",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> uuid.UUID:
    """Seed one scan config plus its per-event hourly metrics; returns the scan
    config id. Pass ``created_at`` to make default-scan resolution
    (``_get_default_scan_config`` orders by ``created_at`` desc, ``id`` desc)
    deterministic when a project has more than one scan. ``updated_at`` is
    accepted so a test can prove it does NOT influence the pick."""
    buckets = [
        datetime(2026, 1, 1, 10, tzinfo=UTC),
        datetime(2026, 1, 1, 11, tzinfo=UTC),
    ]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Metrics DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(project_id),
            event_type_id=None,
            name=name,
            base_query="SELECT time, event_name FROM events",
            event_type_column="event_name",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        if created_at is not None:
            scan_config.created_at = created_at
        if updated_at is not None:
            scan_config.updated_at = updated_at
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()

        for event_row in event_rows:
            for bucket, count in zip(buckets, event_row["counts"], strict=True):
                session.add(
                    EventMetric(
                        id=uuid.uuid4(),
                        scan_config_id=scan_config.id,
                        event_id=uuid.UUID(event_row["event_id"]),
                        event_type_id=None,
                        bucket=bucket,
                        count=count,
                    )
                )

        await session.commit()

    return scan_config.id


# The "latest_scan" monitoring fixture below seeds an anomaly on the newest
# bucket, so its bucket must stay inside the wall-clock freshness horizon
# (classify keeps a signal "latest_scan" only while newer than now - 24h).
# Anchored to recent hour-aligned buckets; the 1h stable->anomaly spacing is
# preserved and the enriched-series assertions derive their bucket strings from
# these same constants.
_MONITORING_STABLE_BUCKET = datetime.now(UTC).replace(
    minute=0, second=0, microsecond=0
) - timedelta(hours=2)
_MONITORING_ANOMALY_BUCKET = _MONITORING_STABLE_BUCKET + timedelta(hours=1)


async def _seed_monitoring_metrics(
    *,
    project_id: str,
    page_type_id: str,
    event_id: str,
) -> str:
    stable_bucket = _MONITORING_STABLE_BUCKET
    anomaly_bucket = _MONITORING_ANOMALY_BUCKET

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Monitoring DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(project_id),
            event_type_id=uuid.UUID(page_type_id),
            name="Monitoring Config",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all(
            [
                data_source,
                scan_config,
                ProjectAnomalySettings(
                    project_id=uuid.UUID(project_id),
                    anomaly_detection_enabled=True,
                ),
            ]
        )
        # Flush parents before their metric/anomaly children (see _seed_group_metrics).
        await session.flush()
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=stable_bucket,
                    count=10,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=None,
                    event_type_id=uuid.UUID(page_type_id),
                    bucket=stable_bucket,
                    count=15,
                ),
            ]
        )
        session.add_all(
            [
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    scope_type="event",
                    scope_ref=event_id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=anomaly_bucket,
                    actual_count=0,
                    expected_count=10,
                    stddev=0,
                    z_score=-10,
                    direction="drop",
                ),
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    scope_type="event_type",
                    scope_ref=page_type_id,
                    event_id=None,
                    event_type_id=uuid.UUID(page_type_id),
                    bucket=anomaly_bucket,
                    actual_count=0,
                    expected_count=15,
                    stddev=0,
                    z_score=-15,
                    direction="drop",
                ),
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    scope_type="project_total",
                    scope_ref=str(scan_config.id),
                    event_id=None,
                    event_type_id=None,
                    bucket=anomaly_bucket,
                    actual_count=0,
                    expected_count=15,
                    stddev=0,
                    z_score=-15,
                    direction="drop",
                ),
            ]
        )
        await session.commit()
        return str(scan_config.id)


async def _seed_recent_monitoring_metrics(
    *,
    project_id: str,
    page_type_id: str,
    event_id: str,
) -> str:
    latest_bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=1
    )
    anomaly_bucket = latest_bucket - timedelta(hours=1)
    stable_bucket = latest_bucket - timedelta(hours=2)

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Recent Monitoring DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(project_id),
            event_type_id=uuid.UUID(page_type_id),
            name="Recent Monitoring Config",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all(
            [
                data_source,
                scan_config,
                ProjectAnomalySettings(
                    project_id=uuid.UUID(project_id),
                    anomaly_detection_enabled=True,
                ),
            ]
        )
        # Flush parents before their metric/anomaly children (see _seed_group_metrics).
        await session.flush()
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=stable_bucket,
                    count=10,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=latest_bucket,
                    count=10,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=None,
                    event_type_id=uuid.UUID(page_type_id),
                    bucket=stable_bucket,
                    count=15,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=None,
                    event_type_id=uuid.UUID(page_type_id),
                    bucket=latest_bucket,
                    count=15,
                ),
            ]
        )
        session.add_all(
            [
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    scope_type="event",
                    scope_ref=event_id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=anomaly_bucket,
                    actual_count=0,
                    expected_count=10,
                    stddev=0,
                    z_score=-10,
                    direction="drop",
                ),
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    scope_type="event_type",
                    scope_ref=page_type_id,
                    event_id=None,
                    event_type_id=uuid.UUID(page_type_id),
                    bucket=anomaly_bucket,
                    actual_count=0,
                    expected_count=15,
                    stddev=0,
                    z_score=-15,
                    direction="drop",
                ),
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    scope_type="project_total",
                    scope_ref=str(scan_config.id),
                    event_id=None,
                    event_type_id=None,
                    bucket=anomaly_bucket,
                    actual_count=0,
                    expected_count=15,
                    stddev=0,
                    z_score=-15,
                    direction="drop",
                ),
            ]
        )
        await session.commit()
        return str(scan_config.id)


@pytest.mark.asyncio
async def test_get_events_metrics_aggregates_matching_events(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "metrics-group")

    event_1 = await client.post(
        "/api/v1/projects/metrics-group/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Alpha Viewed",
            "status": "implemented",
            "tags": ["mobile"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_2 = await client.post(
        "/api/v1/projects/metrics-group/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Beta Review",
            "status": "in_review",
            "tags": ["web"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "pricing"}],
        },
    )
    event_3 = await client.post(
        "/api/v1/projects/metrics-group/events",
        json={
            "event_type_id": setup["track_type_id"],
            "name": "Gamma Archived",
            "status": "archived",
            "tags": ["mobile"],
            "field_values": [{"field_definition_id": setup["track_field_id"], "value": "checkout"}],
        },
    )

    await _seed_group_metrics(
        setup["project_id"],
        [
            {"event_id": event_1.json()["id"], "counts": [10, 15]},
            {"event_id": event_2.json()["id"], "counts": [5, 7]},
            {"event_id": event_3.json()["id"], "counts": [20, 30]},
        ],
    )

    resp = await client.get("/api/v1/projects/metrics-group/events-metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["interval"] == "1h"
    assert body["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 35),
        _plain_point("2026-01-01T11:00:00Z", 52),
    ]


@pytest.mark.asyncio
async def test_get_events_metrics_applies_event_filters(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "metrics-filters")

    event_1 = await client.post(
        "/api/v1/projects/metrics-filters/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Alpha Viewed",
            "status": "implemented",
            "tags": ["mobile"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_2 = await client.post(
        "/api/v1/projects/metrics-filters/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Beta Review",
            "status": "in_review",
            "tags": ["web"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "pricing"}],
        },
    )
    event_3 = await client.post(
        "/api/v1/projects/metrics-filters/events",
        json={
            "event_type_id": setup["track_type_id"],
            "name": "Gamma Archived",
            "status": "archived",
            "tags": ["mobile"],
            "field_values": [{"field_definition_id": setup["track_field_id"], "value": "checkout"}],
        },
    )

    await _seed_group_metrics(
        setup["project_id"],
        [
            {"event_id": event_1.json()["id"], "counts": [10, 15]},
            {"event_id": event_2.json()["id"], "counts": [5, 7]},
            {"event_id": event_3.json()["id"], "counts": [20, 30]},
        ],
    )

    reviewed_resp = await client.get(
        "/api/v1/projects/metrics-filters/events-metrics?status=in_review"
    )
    assert reviewed_resp.status_code == 200
    assert reviewed_resp.json()["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 5),
        _plain_point("2026-01-01T11:00:00Z", 7),
    ]

    archived_resp = await client.get(
        "/api/v1/projects/metrics-filters/events-metrics?status=archived"
    )
    assert archived_resp.status_code == 200
    assert archived_resp.json()["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 20),
        _plain_point("2026-01-01T11:00:00Z", 30),
    ]

    type_resp = await client.get(
        f"/api/v1/projects/metrics-filters/events-metrics?event_type_id={setup['page_type_id']}"
    )
    assert type_resp.status_code == 200
    assert type_resp.json()["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 15),
        _plain_point("2026-01-01T11:00:00Z", 22),
    ]


@pytest.mark.asyncio
async def test_get_events_metrics_scopes_to_default_scan_config(client: AsyncClient) -> None:
    """The "<Tab> Dynamics" series must reflect a single scan's hourly volume
    (the most-recently-created configured scan, matching the project-total sparkline)
    rather than a cross-scan sum. Regression: a legacy "Old events" backfill scan
    that also collected the same events used to be summed in, inflating one bucket
    into an outlier that dominated the chart's y-axis. The series also names the
    scan it is scoped to, so the chart can stop implying project-wide coverage
    (tripl-jfm3.20)."""
    setup = await _setup_metrics_project(client, "metrics-scoping")

    event = await client.post(
        "/api/v1/projects/metrics-scoping/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Alpha Viewed",
            "status": "implemented",
            "tags": ["mobile"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event.json()["id"]

    # Legacy backfill scan (older created_at) with hugely inflated counts — the
    # source of the outlier bucket before scoping was fixed.
    await _seed_group_metrics(
        setup["project_id"],
        [{"event_id": event_id, "counts": [500_000, 600_000]}],
        name="Old events",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    # Current scan (newer created_at) — the default the resolver must select.
    await _seed_group_metrics(
        setup["project_id"],
        [{"event_id": event_id, "counts": [10, 15]}],
        name="Metrics Config",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    resp = await client.get("/api/v1/projects/metrics-scoping/events-metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["interval"] == "1h"
    # Only the default scan's counts — NOT the cross-scan sum (500_010 / 600_015).
    assert body["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 10),
        _plain_point("2026-01-01T11:00:00Z", 15),
    ]
    # …and the response names the scan so the chart title can stop claiming to
    # cover the whole project (tripl-jfm3.20).
    assert body["scan_config_name"] == "Metrics Config"
    assert body["scan_config_id"] is not None


@pytest.mark.asyncio
async def test_events_metrics_charts_the_scan_that_owns_the_tab(client: AsyncClient) -> None:
    """A tab whose event type is collected by some scan OTHER than the
    newest-created one must still chart.

    Scoping the series to a single scan (tripl-jfm3.20) fixed cross-scan double
    counting, but it pinned EVERY tab to the project-wide default scan. On
    acme-ios that silently emptied two tabs out of three: the "se" tab holds 366
    live events collected by "Snowplow Events (iOS)" (created 14:43), while
    ``_get_default_scan_config`` returns "Snowplow Pageviews (iOS)" (created
    15:12) — so the query asked the newest scan for rows it had never written and
    the card rendered "No recent volume to chart" (tripl-g77e). Verified against
    production: ``events-metrics?event_type_id=<se>`` -> 0 points, the same call
    without the filter -> 167.

    The scan is now resolved from the rows the query will actually sum, so one
    scan still owns the series — no double counting — but it is the scan that has
    data for THIS tab.
    """
    setup = await _setup_metrics_project(client, "metrics-tab-scan")

    page_event = await client.post(
        "/api/v1/projects/metrics-tab-scan/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Home Viewed",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    track_event = await client.post(
        "/api/v1/projects/metrics-tab-scan/events",
        json={
            "event_type_id": setup["track_type_id"],
            "name": "Button Clicked",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["track_field_id"], "value": "home"}],
        },
    )

    # The OLDER scan is the only one collecting the "track" type.
    await _seed_group_metrics(
        setup["project_id"],
        [{"event_id": track_event.json()["id"], "counts": [7, 9]}],
        name="Snowplow Events",
        created_at=datetime(2026, 6, 1, 14, 43, tzinfo=UTC),
    )
    # The NEWEST scan collects only "page" — this is what _get_default_scan_config
    # picks, and pinning to it is what emptied the other tab.
    await _seed_group_metrics(
        setup["project_id"],
        [{"event_id": page_event.json()["id"], "counts": [10, 15]}],
        name="Snowplow Pageviews",
        created_at=datetime(2026, 6, 1, 15, 12, tzinfo=UTC),
    )

    tab = await client.get(
        "/api/v1/projects/metrics-tab-scan/events-metrics",
        params={"event_type_id": setup["track_type_id"]},
    )

    assert tab.status_code == 200
    body = tab.json()
    assert body["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 7),
        _plain_point("2026-01-01T11:00:00Z", 9),
    ]
    # …and it names the scan it charted, which is NOT the project default.
    assert body["scan_config_name"] == "Snowplow Events"

    # Control: the tab that does belong to the newest scan is unaffected, and
    # still sees only its own scan's counts rather than a cross-scan sum.
    control = await client.get(
        "/api/v1/projects/metrics-tab-scan/events-metrics",
        params={"event_type_id": setup["page_type_id"]},
    )

    assert control.status_code == 200
    assert control.json()["scan_config_name"] == "Snowplow Pageviews"
    assert control.json()["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 10),
        _plain_point("2026-01-01T11:00:00Z", 15),
    ]


@pytest.mark.asyncio
async def test_default_scan_config_ignores_updated_at_and_breaks_ties_stably(
    client: AsyncClient,
) -> None:
    """Default-scan resolution must not move when an unrelated scan config is
    edited, and must be deterministic when two configs share a timestamp
    (tripl-jfm3.21). Before the fix the resolver ordered by ``updated_at`` desc
    with no tiebreak: two acme-ios configs carried byte-identical ``updated_at``
    values, so a 32x swing in the headline volume hung on Postgres row order,
    and touching any config re-pointed the chart."""
    setup = await _setup_metrics_project(client, "metrics-tiebreak")

    event = await client.post(
        "/api/v1/projects/metrics-tiebreak/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Alpha Viewed",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event.json()["id"]

    # Newest by created_at but oldest by updated_at: the pick must follow
    # created_at, so this is the scan the volume series charts.
    await _seed_group_metrics(
        setup["project_id"],
        [{"event_id": event_id, "counts": [10, 15]}],
        name="Current scan",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # A stale scan that someone renamed five minutes ago: `onupdate=func.now()`
    # bumped updated_at, which used to hijack the headline chart.
    await _seed_group_metrics(
        setup["project_id"],
        [{"event_id": event_id, "counts": [500_000, 600_000]}],
        name="Old events",
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 20, tzinfo=UTC),
    )

    first = await client.get("/api/v1/projects/metrics-tiebreak/metrics/total")
    assert first.status_code == 200
    assert first.json()["scan_config_name"] == "Current scan"

    # Stable across calls — no dependence on row order.
    second = await client.get("/api/v1/projects/metrics-tiebreak/metrics/total")
    assert second.json()["scan_config_id"] == first.json()["scan_config_id"]


@pytest.mark.asyncio
async def test_project_total_metrics_names_its_scan_config(client: AsyncClient) -> None:
    """The Overview volume card charts ONE scan config, so the response must
    name it — the card used to be labelled "project total" while plotting 2.4 %
    of acme-ios's volume (tripl-jfm3.20)."""
    setup = await _setup_metrics_project(client, "metrics-total-name")
    await _seed_group_metrics(setup["project_id"], [], name="Snowplow Pageviews (iOS)")

    resp = await client.get("/api/v1/projects/metrics-total-name/metrics/total")

    assert resp.status_code == 200
    assert resp.json()["scan_config_name"] == "Snowplow Pageviews (iOS)"


@pytest.mark.asyncio
async def test_get_events_window_metrics_returns_last_day_counts_per_event(
    client: AsyncClient,
) -> None:
    setup = await _setup_metrics_project(client, "metrics-window")

    event_1 = await client.post(
        "/api/v1/projects/metrics-window/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Alpha Viewed",
            "status": "implemented",
            "tags": ["mobile"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_2 = await client.post(
        "/api/v1/projects/metrics-window/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Beta Review",
            "status": "in_review",
            "tags": ["web"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "pricing"}],
        },
    )

    await _seed_group_metrics(
        setup["project_id"],
        [
            {"event_id": event_1.json()["id"], "counts": [10, 15]},
            {"event_id": event_2.json()["id"], "counts": [5, 7]},
        ],
    )

    resp = await client.post(
        "/api/v1/projects/metrics-window/events/window-metrics",
        json={
            "event_ids": [event_1.json()["id"], event_2.json()["id"]],
            "time_from": "2026-01-01T09:00:00Z",
            "time_to": "2026-01-01T12:00:00Z",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [(item["event_id"], item["total_count"]) for item in body] == [
        (event_1.json()["id"], 25),
        (event_2.json()["id"], 12),
    ]
    assert body[0]["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 10),
        _plain_point("2026-01-01T11:00:00Z", 15),
    ]


async def _seed_event_metrics_at(
    project_id: str,
    event_id: str,
    *,
    name: str,
    points: list[tuple[datetime, int]],
) -> uuid.UUID:
    """Seed one scan config plus explicit (bucket, count) rows for one event."""
    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Window DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(project_id),
            event_type_id=None,
            name=name,
            base_query="SELECT time, event_name FROM events",
            event_type_column="event_name",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        await session.flush()
        for bucket, count in points:
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    count=count,
                )
            )
        await session.commit()
        return scan_config.id


@pytest.mark.asyncio
async def test_get_events_window_metrics_uses_the_scan_with_the_newest_bucket(
    client: AsyncClient,
) -> None:
    """The series must come from whichever scan reported the event most recently.

    Also pins the SHAPE of that lookup (tripl-jfm3.79): it used to select every
    metric row the project had ever recorded for the requested events and keep
    the first per event in Python, which cost 5.4 s of a 6.1 s response on a
    real project. Every ``event_metrics`` read here must therefore be bounded —
    either by ``LIMIT`` (the newest-scan probe) or by the requested bucket range
    (the series pull).
    """
    setup = await _setup_metrics_project(client, "window-latest-scan")

    event_resp = await client.post(
        "/api/v1/projects/window-latest-scan/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Alpha Viewed",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event_resp.json()["id"]

    # The stale scan is seeded LAST so insertion order cannot be what decides.
    newest_scan_id = await _seed_event_metrics_at(
        setup["project_id"],
        event_id,
        name="Current Scan",
        points=[
            (datetime(2026, 1, 2, 10, tzinfo=UTC), 100),
            (datetime(2026, 1, 2, 11, tzinfo=UTC), 200),
        ],
    )
    await _seed_event_metrics_at(
        setup["project_id"],
        event_id,
        name="Retired Scan",
        points=[
            (datetime(2026, 1, 1, 10, tzinfo=UTC), 1),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), 2),
        ],
    )

    with captured_sql() as statements:
        resp = await client.post(
            "/api/v1/projects/window-latest-scan/events/window-metrics",
            json={
                "event_ids": [event_id],
                "time_from": "2026-01-01T00:00:00Z",
                "time_to": "2026-01-03T00:00:00Z",
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["scan_config_id"] == str(newest_scan_id)
    assert body[0]["total_count"] == 300
    assert body[0]["data"] == [
        _plain_point("2026-01-02T10:00:00Z", 100),
        _plain_point("2026-01-02T11:00:00Z", 200),
    ]

    unbounded = [
        statement
        for statement in statements
        if "FROM event_metrics" in statement
        and "LIMIT" not in statement.upper()
        and "event_metrics.bucket >=" not in statement
    ]
    assert unbounded == [], f"unbounded event_metrics scan: {unbounded}"


@pytest.mark.asyncio
async def test_get_event_metrics_returns_enriched_monitoring_series(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "monitoring-event")

    event_resp = await client.post(
        "/api/v1/projects/monitoring-event/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Login",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event_resp.json()["id"]

    await _seed_monitoring_metrics(
        project_id=setup["project_id"],
        page_type_id=setup["page_type_id"],
        event_id=event_id,
    )

    resp = await client.get(f"/api/v1/projects/monitoring-event/events/{event_id}/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "event"
    assert body["latest_signal"]["scope_type"] == "event"
    assert body["latest_signal"]["state"] == "latest_scan"
    assert body["data"] == [
        {
            "bucket": _MONITORING_STABLE_BUCKET.isoformat().replace("+00:00", "Z"),
            "count": 10,
            "expected_count": None,
            "stddev": None,
            "detector_kind": None,
            "is_anomaly": False,
            "anomaly_direction": None,
            "z_score": None,
        },
        {
            "bucket": _MONITORING_ANOMALY_BUCKET.isoformat().replace("+00:00", "Z"),
            "count": 0,
            "expected_count": 10.0,
            "stddev": 0.0,
            "detector_kind": "phase",
            "is_anomaly": True,
            "anomaly_direction": "drop",
            "z_score": -10.0,
        },
    ]


@pytest.mark.asyncio
async def test_get_event_metric_breakdowns_returns_series(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "breakdown-event")

    event_resp = await client.post(
        "/api/v1/projects/breakdown-event/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Login",
            "status": "implemented",
            "metric_breakdown_columns": ["country"],
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event_resp.json()["id"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Breakdown DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Breakdown Config",
            base_query="SELECT time, event_name, country FROM events",
            time_column="time",
            metric_breakdown_columns=["platform"],
            platform_column="country",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        for bucket, country, count, is_other in [
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "US", 10, False),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), "US", 12, False),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "Other", 5, True),
        ]:
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="country",
                    breakdown_value=country,
                    is_other=is_other,
                    count=count,
                )
            )
        session.add(
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=datetime(2026, 1, 1, 11, tzinfo=UTC),
                count=17,
            )
        )
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=datetime(2026, 1, 1, 10, tzinfo=UTC),
                breakdown_column="country",
                breakdown_value="Other",
                is_other=True,
                actual_count=5,
                expected_count=20,
                stddev=2,
                z_score=-7.5,
                direction="drop",
            )
        )
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=datetime(2026, 1, 1, 10, tzinfo=UTC),
                breakdown_column="country",
                breakdown_value="US",
                is_other=False,
                kind="parity",
                actual_count=0.25,
                expected_count=0.5,
                stddev=0.02,
                z_score=-12.5,
                direction="drop",
            )
        )
        await session.commit()

    resp = await client.get(
        f"/api/v1/projects/breakdown-event/events/{event_id}/metrics/breakdowns"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["columns"] == ["platform", "country"]
    assert body["selected_column"] == "country"
    assert [series["breakdown_value"] for series in body["series"]] == ["US", "Other"]
    assert body["series"][0]["total_count"] == 22
    assert body["series"][0]["data"][0]["is_anomaly"] is False
    assert body["series"][0]["parity_anomalies"] == [
        {
            "bucket": "2026-01-01T10:00:00Z",
            "actual_share": 0.25,
            "expected_share": 0.5,
            "stddev": 0.02,
            "z_score": -12.5,
            "direction": "drop",
        }
    ]
    other_series = body["series"][1]
    assert other_series["is_other"] is True
    assert other_series["data"][0]["is_anomaly"] is True
    assert other_series["data"][0]["anomaly_direction"] == "drop"


@pytest.mark.asyncio
async def test_get_app_version_series_returns_semver_ordered_versions(
    client: AsyncClient,
) -> None:
    setup = await _setup_metrics_project(client, "version-series")

    event_resp = await client.post(
        "/api/v1/projects/version-series/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Login",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event_resp.json()["id"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Version DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Version Config",
            base_query="SELECT time, event_name, app_version FROM events",
            time_column="time",
            app_version_column="app_version",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        for bucket, version, count, is_other in [
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "2.9.0", 90, False),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "2.10.0", 100, False),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "beta", 10, False),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "Other", 40, True),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), "2.10.0", 200, False),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), "2.9.0", 80, False),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), "beta", 20, False),
        ]:
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="app_version",
                    breakdown_value=version,
                    is_other=is_other,
                    count=count,
                )
            )
        # Legacy rows may survive until the next scheduled recomputation after
        # upgrading. App-version APIs are observational and must not serve them.
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=datetime(2026, 1, 1, 11, tzinfo=UTC),
                breakdown_column="app_version",
                breakdown_value="2.10.0",
                is_other=False,
                actual_count=200,
                expected_count=400,
                stddev=2,
                z_score=-10,
                direction="drop",
            )
        )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/version-series/scans/{scan_config_id}/app-versions",
        params={"scope_type": "event", "scope_ref": event_id},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["app_version_column"] == "app_version"
    assert body["latest_version"] == "2.10.0"
    assert [item["version"] for item in body["versions"]] == [
        "2.10.0",
        "2.9.0",
        "beta",
        "Other",
    ]
    assert [item["is_latest"] for item in body["versions"]] == [True, False, False, False]
    assert [series["version"] for series in body["series"]] == [
        "2.10.0",
        "2.9.0",
        "beta",
        "Other",
    ]
    assert body["series"][0]["is_latest"] is True
    assert body["series"][0]["total_count"] == 300
    assert body["series"][-1]["is_other"] is True
    assert all(
        point["is_anomaly"] is False for series in body["series"] for point in series["data"]
    )


@pytest.mark.asyncio
async def test_get_app_version_series_retention_is_window_stable(
    client: AsyncClient,
) -> None:
    # Regression: latest-N retention + "Other" rollup must be decided once over
    # the whole window, not per collection chunk. A high-volume older version
    # must fold into "Other" consistently — even in buckets where the newer
    # version is absent — instead of flipping between its own series and "Other".
    setup = await _setup_metrics_project(client, "version-stable")
    policy_resp = await client.patch(
        "/api/v1/projects/version-stable",
        json={"app_version_keep_releases": 1},
    )
    assert policy_resp.status_code == 200

    event_resp = await client.post(
        "/api/v1/projects/version-stable/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Login",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event_resp.json()["id"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Version Stable DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Version Stable Config",
            base_query="SELECT time, event_name, app_version FROM events",
            time_column="time",
            app_version_column="app_version",
            # Deliberately conflicts with the project policy: runtime retention
            # must no longer depend on the legacy per-scan value.
            app_version_keep_releases=5,
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        # Every version stored verbatim (is_other=False), as the write path now does.
        # 2.0.0 (newest) appears only in the first bucket; 1.0.0 dominates both.
        for bucket, version, count in [
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "1.0.0", 100),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "2.0.0", 5),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), "1.0.0", 80),
        ]:
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="app_version",
                    breakdown_value=version,
                    is_other=False,
                    count=count,
                )
            )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/version-stable/scans/{scan_config_id}/app-versions",
        params={"scope_type": "event", "scope_ref": event_id},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_version"] == "2.0.0"
    # Project keep_releases=1 → only the latest (2.0.0) stays explicit; 1.0.0 folds into
    # "Other" in BOTH buckets, including the one where 2.0.0 is absent.
    assert [item["version"] for item in body["versions"]] == ["2.0.0", "Other"]
    series_by_version = {series["version"]: series for series in body["series"]}
    assert set(series_by_version) == {"2.0.0", "Other"}
    assert series_by_version["2.0.0"]["total_count"] == 5
    assert series_by_version["Other"]["is_other"] is True
    assert series_by_version["Other"]["total_count"] == 180


@pytest.mark.asyncio
async def test_get_app_version_adoption_returns_project_totals(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "version-adoption")

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Version Adoption DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Version Adoption Config",
            base_query="SELECT time, event_name, app_version FROM events",
            time_column="time",
            app_version_column="app_version",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        for bucket, version, count, is_other in [
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "2.9.0", 9, False),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "2.10.0", 10, False),
            (datetime(2026, 1, 1, 10, tzinfo=UTC), "Other", 4, True),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), "2.10.0", 20, False),
            (datetime(2026, 1, 1, 11, tzinfo=UTC), "2.9.0", 8, False),
        ]:
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=None,
                    event_type_id=uuid.UUID(setup["page_type_id"]),
                    bucket=bucket,
                    breakdown_column="app_version",
                    breakdown_value=version,
                    is_other=is_other,
                    count=count,
                )
            )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/version-adoption/scans/{scan_config_id}/version-adoption"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope_type"] == "project_total"
    assert body["scope_ref"] == scan_config_id
    assert body["latest_version"] == "2.10.0"
    assert [series["version"] for series in body["series"]] == ["2.10.0", "2.9.0", "Other"]
    assert body["totals"] == [
        {"bucket": "2026-01-01T10:00:00Z", "count": 23},
        {"bucket": "2026-01-01T11:00:00Z", "count": 28},
    ]


@pytest.mark.asyncio
async def test_get_app_version_endpoints_are_empty_without_version_column(
    client: AsyncClient,
) -> None:
    setup = await _setup_metrics_project(client, "version-empty")

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Version Empty DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="No Version Config",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        await session.commit()
        scan_config_id = str(scan_config.id)

    series_resp = await client.get(
        f"/api/v1/projects/version-empty/scans/{scan_config_id}/app-versions"
    )
    adoption_resp = await client.get(
        f"/api/v1/projects/version-empty/scans/{scan_config_id}/version-adoption"
    )

    assert series_resp.status_code == 200
    assert adoption_resp.status_code == 200
    series_body = series_resp.json()
    adoption_body = adoption_resp.json()
    assert series_body["app_version_column"] is None
    assert series_body["latest_version"] is None
    assert series_body["versions"] == []
    assert series_body["series"] == []
    assert adoption_body["totals"] == []
    assert adoption_body["series"] == []


@pytest.mark.asyncio
async def test_get_release_regressions_lists_missing_first(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "release-regressions")

    event_resp = await client.post(
        "/api/v1/projects/release-regressions/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Login",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event_resp.json()["id"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Regression DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Regression Config",
            base_query="SELECT time, event_name, app_version FROM events",
            time_column="time",
            app_version_column="app_version",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        # A volume_drop (event-type scope) and a missing event — missing should
        # sort first regardless of insertion order.
        session.add(
            ReleaseRegression(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event_type",
                scope_ref=setup["page_type_id"],
                event_id=None,
                event_type_id=uuid.UUID(setup["page_type_id"]),
                app_version_column="app_version",
                version="2.1.0",
                previous_version="2.0.0",
                kind="volume_drop",
                observed_count=40,
                expected_count=100.0,
                ratio=0.4,
                share_prev=0.2,
                share_new=0.08,
                release_share=0.33,
                window_from=datetime(2026, 1, 1, 10, tzinfo=UTC),
                window_to=datetime(2026, 1, 1, 11, tzinfo=UTC),
            )
        )
        session.add(
            ReleaseRegression(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                app_version_column="app_version",
                version="2.1.0",
                previous_version="2.0.0",
                kind="missing",
                observed_count=0,
                expected_count=200.0,
                ratio=0.0,
                share_prev=0.1,
                share_new=0.0,
                release_share=0.33,
                window_from=datetime(2026, 1, 1, 10, tzinfo=UTC),
                window_to=datetime(2026, 1, 1, 11, tzinfo=UTC),
            )
        )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/release-regressions/scans/{scan_config_id}/release-regressions"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["app_version_column"] == "app_version"
    assert body["latest_version"] == "2.1.0"
    assert [item["kind"] for item in body["items"]] == ["missing", "volume_drop"]
    missing = body["items"][0]
    assert missing["scope_type"] == "event"
    assert missing["scope_name"] == "event_name=Login"
    assert missing["version"] == "2.1.0"
    assert missing["previous_version"] == "2.0.0"
    assert missing["observed_count"] == 0
    assert missing["expected_count"] == 200.0
    assert body["items"][1]["scope_type"] == "event_type"

    # scope_type filter narrows the result set.
    filtered = await client.get(
        f"/api/v1/projects/release-regressions/scans/{scan_config_id}/release-regressions",
        params={"scope_type": "event"},
    )
    assert [item["kind"] for item in filtered.json()["items"]] == ["missing"]


@pytest.mark.asyncio
async def test_get_release_regressions_serves_a_withheld_verdict_with_no_rows(
    client: AsyncClient,
) -> None:
    """Zero rows is two different facts. Without the verdict on the response a
    suppressed comparison and a healthy one are byte-identical, and the panel
    prints "No events regressed" for both. The release pair is only known from
    the verdict here, since no row carries it."""
    setup = await _setup_metrics_project(client, "regression-withheld")

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Regression Withheld DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Withheld Regression Config",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            app_version_column="app_version",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        await session.flush()
        session.add(
            ReleaseComparability(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                app_version_column="app_version",
                version="2.1.0",
                previous_version="2.0.0",
                comparable=False,
                reason="population_mismatch",
                emerging_share=0.66,
                max_emerging_share=0.25,
            )
        )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/regression-withheld/scans/{scan_config_id}/release-regressions"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert len(body["comparability"]) == 1
    verdict = body["comparability"][0]
    assert verdict["comparable"] is False
    assert verdict["reason"] == "population_mismatch"
    assert verdict["version"] == "2.1.0"
    assert verdict["previous_version"] == "2.0.0"
    assert verdict["emerging_share"] == 0.66
    assert verdict["max_emerging_share"] == 0.25
    # No regression row carries the pair, so this can only come from the verdict.
    assert body["latest_version"] == "2.1.0"


@pytest.mark.asyncio
async def test_get_release_regressions_empty_without_version_column(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "regression-empty")

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Regression Empty DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="No Version Regression Config",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/regression-empty/scans/{scan_config_id}/release-regressions"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["app_version_column"] is None
    assert body["latest_version"] is None
    assert body["items"] == []


@pytest.mark.asyncio
async def test_get_project_total_metrics_and_active_signals(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "monitoring-total")

    event_resp = await client.post(
        "/api/v1/projects/monitoring-total/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Checkout",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "pricing"}],
        },
    )
    event_id = event_resp.json()["id"]

    scan_config_id = await _seed_monitoring_metrics(
        project_id=setup["project_id"],
        page_type_id=setup["page_type_id"],
        event_id=event_id,
    )

    total_resp = await client.get(
        f"/api/v1/projects/monitoring-total/metrics/total?scan_config_id={scan_config_id}"
    )
    assert total_resp.status_code == 200
    total_body = total_resp.json()
    assert total_body["scope"] == "project_total"
    assert total_body["latest_signal"]["scope_ref"] == scan_config_id
    assert total_body["latest_signal"]["state"] == "latest_scan"
    assert total_body["data"][-1]["is_anomaly"] is True

    type_resp = await client.get(
        f"/api/v1/projects/monitoring-total/event-types/{setup['page_type_id']}/metrics"
    )
    assert type_resp.status_code == 200
    assert type_resp.json()["latest_signal"]["scope_type"] == "event_type"
    assert type_resp.json()["latest_signal"]["state"] == "latest_scan"

    signals_resp = await client.get(
        f"/api/v1/projects/monitoring-total/anomalies/signals?event_id={event_id}"
    )
    assert signals_resp.status_code == 200
    signals = signals_resp.json()
    # Incident de-duplication: the event/event_type signals share the same
    # (scan_config_id, bucket, direction) as the project_total signal and are
    # suppressed in favor of the kept project_total signal.
    assert {(signal["scope_type"], signal["scope_ref"]) for signal in signals} == {
        ("project_total", scan_config_id),
    }
    assert {signal["state"] for signal in signals} == {"latest_scan"}


@pytest.mark.asyncio
async def test_get_recent_signals_when_anomaly_is_within_last_24_hours(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, "monitoring-recent")

    event_resp = await client.post(
        "/api/v1/projects/monitoring-recent/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Signup",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "signup"}],
        },
    )
    event_id = event_resp.json()["id"]

    scan_config_id = await _seed_recent_monitoring_metrics(
        project_id=setup["project_id"],
        page_type_id=setup["page_type_id"],
        event_id=event_id,
    )

    total_resp = await client.get(
        f"/api/v1/projects/monitoring-recent/metrics/total?scan_config_id={scan_config_id}"
    )
    assert total_resp.status_code == 200
    assert total_resp.json()["latest_signal"]["state"] == "recent"

    event_resp = await client.get(f"/api/v1/projects/monitoring-recent/events/{event_id}/metrics")
    assert event_resp.status_code == 200
    assert event_resp.json()["latest_signal"]["state"] == "recent"

    signals_resp = await client.get(
        f"/api/v1/projects/monitoring-recent/anomalies/signals?event_id={event_id}"
    )
    assert signals_resp.status_code == 200
    signals = signals_resp.json()
    # Incident de-duplication: the event/event_type signals share the same
    # (scan_config_id, bucket, direction) as the project_total signal and are
    # suppressed in favor of the kept project_total signal.
    assert {(signal["scope_type"], signal["state"]) for signal in signals} == {
        ("project_total", "recent"),
    }


@pytest.mark.asyncio
async def test_get_top_movers_ranks_regular_breakdowns_and_excludes_app_versions(
    client: AsyncClient,
) -> None:
    setup = await _setup_metrics_project(client, "top-movers")

    event_resp = await client.post(
        "/api/v1/projects/top-movers/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "event_name=Login",
            "status": "implemented",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "home"}],
        },
    )
    event_id = event_resp.json()["id"]
    bucket = datetime(2026, 1, 1, 10, tzinfo=UTC)

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"TopMover DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Top-mover Config",
            base_query="SELECT time, event_name, country, app_version FROM events",
            time_column="time",
            metric_breakdown_columns=["country"],
            app_version_column="app_version",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        # Two anomalies on the same bucket; one with a much bigger |z|. A third
        # row on a different bucket is added as noise that must NOT be returned.
        for value, z, count, expected in [
            ("US", -2.0, 90, 100),
            ("DE", -8.0, 10, 60),
        ]:
            session.add(
                MetricBreakdownAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    scope_type="event",
                    scope_ref=event_id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="country",
                    breakdown_value=value,
                    is_other=False,
                    actual_count=count,
                    expected_count=expected,
                    stddev=5,
                    z_score=z,
                    direction="drop",
                )
            )
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=datetime(2026, 1, 1, 11, tzinfo=UTC),
                breakdown_column="country",
                breakdown_value="US",
                is_other=False,
                actual_count=80,
                expected_count=100,
                stddev=5,
                z_score=-4.0,
                direction="drop",
            )
        )
        session.add(
            MetricBreakdownAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=bucket,
                breakdown_column="app_version",
                breakdown_value="2.10.0",
                is_other=False,
                actual_count=10,
                expected_count=100,
                stddev=2,
                z_score=-20.0,
                direction="drop",
            )
        )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/top-movers/scans/{scan_config_id}/top-movers",
        params={
            "scope_type": "event",
            "scope_ref": event_id,
            "bucket": bucket.isoformat(),
            "limit": 10,
        },
    )

    assert resp.status_code == 200
    items = resp.json()
    assert [(row["breakdown_value"], row["z_score"]) for row in items] == [
        ("DE", -8.0),
        ("US", -2.0),
    ]


@pytest.mark.asyncio
async def test_distribution_drifts_endpoint_filters_by_event_type(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, slug="distribution-api")
    bucket = datetime(2026, 1, 1, 10, tzinfo=UTC)

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name="Distribution DS",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Distribution Config",
            base_query="SELECT time, event_name, platform FROM events",
            time_column="time",
            distribution_drift_fields=["platform"],
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        session.add(
            DistributionDrift(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                event_type_id=uuid.UUID(setup["page_type_id"]),
                field_name="platform",
                bucket=bucket,
                psi=0.42,
                band="significant",
                baseline_total=1000,
                current_total=1000,
                top_movers=[
                    {
                        "value": "ios",
                        "baseline_share": 0.5,
                        "current_share": 0.9,
                        "contribution": 0.25,
                    }
                ],
            )
        )
        session.add(
            DistributionDrift(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                event_type_id=uuid.UUID(setup["track_type_id"]),
                field_name="country",
                bucket=bucket,
                psi=0.30,
                band="significant",
                baseline_total=10,
                current_total=10,
                top_movers=[],
            )
        )
        await session.commit()

    resp = await client.get(
        "/api/v1/projects/distribution-api/distribution-drifts",
        params={
            "scope_type": "event_type",
            "scope_ref": setup["page_type_id"],
            "from": (bucket - timedelta(hours=1)).isoformat(),
            "to": (bucket + timedelta(hours=1)).isoformat(),
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["fields"] == ["platform"]
    assert len(body["data"]) == 1
    assert body["data"][0]["band"] == "significant"
    assert body["data"][0]["top_movers"][0]["value"] == "ios"


@pytest.mark.asyncio
async def test_seasonality_heatmap_aggregates_by_weekday_hour(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, slug="heatmap-api")
    event_resp = await client.post(
        "/api/v1/projects/heatmap-api/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Home",
            "field_values": [
                {"field_definition_id": setup["page_field_id"], "value": "home"},
            ],
        },
    )
    event_id = event_resp.json()["id"]

    # 2026-01-05 is a Monday; 2026-01-06 a Tuesday. Hours 09 and 10 chosen
    # so the assertions can target specific (weekday, hour) slots.
    monday_9 = datetime(2026, 1, 5, 9, tzinfo=UTC)
    monday_9_again = datetime(2026, 1, 12, 9, tzinfo=UTC)
    monday_10 = datetime(2026, 1, 5, 10, tzinfo=UTC)
    tuesday_10 = datetime(2026, 1, 6, 10, tzinfo=UTC)

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Heatmap DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Heatmap Config",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()

        for bucket, count in [
            (monday_9, 30),
            (monday_9_again, 20),
            (monday_10, 5),
            (tuesday_10, 50),
        ]:
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    count=count,
                )
            )
        # One anomaly on Tuesday 10 — should show up as anomaly_count=1 in
        # that cell.
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config.id,
                scope_type="event",
                scope_ref=event_id,
                event_id=uuid.UUID(event_id),
                event_type_id=None,
                bucket=tuesday_10,
                actual_count=50,
                expected_count=5.0,
                stddev=2.0,
                z_score=15.0,
                direction="spike",
            )
        )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/heatmap-api/scans/{scan_config_id}/seasonality",
        params={
            "scope_type": "event",
            "scope_ref": event_id,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["cells"]) == 7 * 24
    assert body["total_count"] == 30 + 20 + 5 + 50
    assert body["max_count"] == 50

    by_slot = {(cell["weekday"], cell["hour"]): cell for cell in body["cells"]}
    # Monday=0, Tuesday=1 (Python datetime.weekday()).
    assert by_slot[(0, 9)]["count"] == 50  # 30 + 20 two Mondays at 09:00
    assert by_slot[(0, 9)]["anomaly_count"] == 0
    assert by_slot[(0, 10)]["count"] == 5
    assert by_slot[(1, 10)]["count"] == 50
    assert by_slot[(1, 10)]["anomaly_count"] == 1


@pytest.mark.asyncio
async def test_breakdown_timeline_returns_per_bucket_counts(client: AsyncClient) -> None:
    setup = await _setup_metrics_project(client, slug="drilldown-api")
    event_resp = await client.post(
        "/api/v1/projects/drilldown-api/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Home",
            "field_values": [
                {"field_definition_id": setup["page_field_id"], "value": "home"},
            ],
        },
    )
    event_id = event_resp.json()["id"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Drilldown DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(setup["project_id"]),
            event_type_id=uuid.UUID(setup["page_type_id"]),
            name="Drilldown Config",
            base_query="SELECT time, event_name, country FROM events",
            time_column="time",
            metric_breakdown_columns=["country"],
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()

        buckets = [
            datetime(2026, 1, 1, 10, tzinfo=UTC),
            datetime(2026, 1, 1, 11, tzinfo=UTC),
            datetime(2026, 1, 1, 12, tzinfo=UTC),
        ]
        # Two breakdown_values; expect drill-down to return only US.
        for bucket, us_count, de_count in zip(buckets, [40, 50, 60], [10, 20, 30], strict=True):
            session.add_all(
                [
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=scan_config.id,
                        event_id=uuid.UUID(event_id),
                        event_type_id=None,
                        bucket=bucket,
                        breakdown_column="country",
                        breakdown_value="US",
                        is_other=False,
                        count=us_count,
                    ),
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=scan_config.id,
                        event_id=uuid.UUID(event_id),
                        event_type_id=None,
                        bucket=bucket,
                        breakdown_column="country",
                        breakdown_value="DE",
                        is_other=False,
                        count=de_count,
                    ),
                ]
            )
        await session.commit()
        scan_config_id = str(scan_config.id)

    resp = await client.get(
        f"/api/v1/projects/drilldown-api/scans/{scan_config_id}/breakdown-timeline",
        params={
            "scope_type": "event",
            "scope_ref": event_id,
            "breakdown_column": "country",
            "breakdown_value": "US",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["breakdown_value"] == "US"
    assert [point["count"] for point in body["data"]] == [40, 50, 60]


@pytest.mark.asyncio
async def test_overview_top_events_ranks_by_volume(client: AsyncClient):
    ctx = await _setup_metrics_project(client, slug="top-ev")

    async def _make_event(name: str) -> str:
        resp = await client.post(
            "/api/v1/projects/top-ev/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": name,
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    big = await _make_event("Big")
    small = await _make_event("Small")
    recent = datetime.now(UTC) - timedelta(hours=1)
    old = datetime.now(UTC) - timedelta(days=10)

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Top DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(ctx["project_id"]),
            event_type_id=None,
            name="Top Config",
            base_query="SELECT time, event_name FROM events",
            event_type_column="event_name",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(big),
                    event_type_id=None,
                    bucket=recent,
                    count=500,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(small),
                    event_type_id=None,
                    bucket=recent,
                    count=20,
                ),
                # Outside the 48h window — must be excluded from the ranking.
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=uuid.UUID(small),
                    event_type_id=None,
                    bucket=old,
                    count=99999,
                ),
            ]
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/top-ev/overview/top-events?window_hours=48&limit=6")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["event_id"] for r in rows] == [big, small]
    assert rows[0]["total_count"] == 500
    assert rows[1]["total_count"] == 20
    assert rows[0]["name"] == "Big"


@pytest.mark.asyncio
async def test_data_source_stats_aggregates_recent_metrics(client: AsyncClient):
    ctx = await _setup_metrics_project(client, slug="ds-stats")
    recent = datetime.now(UTC) - timedelta(hours=1)
    old = datetime.now(UTC) - timedelta(days=10)
    ds_id = uuid.uuid4()

    async def _event(name: str) -> uuid.UUID:
        resp = await client.post(
            "/api/v1/projects/ds-stats/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": name,
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert resp.status_code == 201
        return uuid.UUID(resp.json()["id"])

    # Real events so event_metrics.event_id satisfies its FK (matches Postgres).
    ev1, ev2 = await _event("Ev1"), await _event("Ev2")

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=ds_id,
            name="DS stats",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=ds_id,
            project_id=uuid.UUID(ctx["project_id"]),
            event_type_id=None,
            name="DS Config",
            base_query="SELECT time, event_name FROM events",
            event_type_column="event_name",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        session.add_all(
            [
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=ev1,
                    event_type_id=None,
                    bucket=recent,
                    count=100,
                ),
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=ev2,
                    event_type_id=None,
                    bucket=recent,
                    count=50,
                ),
                # The type-level row every real chunk writes alongside the
                # event-level ones: same bucket, same total, ``event_id`` NULL.
                # The stats query used to sum BOTH levels and reported 300 for
                # 150 events; it now counts only this one (tripl-0zpq.118), so
                # without this row the window would be empty.
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=None,
                    event_type_id=uuid.UUID(ctx["page_type_id"]),
                    bucket=recent,
                    count=150,
                ),
                # Outside the window — excluded.
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config.id,
                    event_id=ev1,
                    event_type_id=None,
                    bucket=old,
                    count=9999,
                ),
            ]
        )
        await session.commit()

    resp = await client.get(f"/api/v1/data-sources/{ds_id}/stats?window_hours=48")
    assert resp.status_code == 200
    body = resp.json()
    assert body["volume_window"] == 150
    assert body["events_tracked"] == 2
    assert len(body["throughput"]) == 1


@pytest.mark.asyncio
async def test_overview_kpi_series(client: AsyncClient):
    ctx = await _setup_metrics_project(client, slug="kpi-series")
    for name in ("A", "B"):
        resp = await client.post(
            "/api/v1/projects/kpi-series/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": name,
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert resp.status_code == 201

    resp = await client.get("/api/v1/projects/kpi-series/overview/kpi-series?days=14")
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"] == 14
    # The series has always been "events created per day"; it is now named that
    # way instead of `active_events` (tripl-jfm3.22).
    assert "active_events" not in body
    assert len(body["new_events"]) == 14
    assert sum(body["new_events"]) == 2
    assert body["new_events"][-1] == 2  # both created today


@pytest.mark.asyncio
async def test_overview_kpi_series_counts_main_branch_only(client: AsyncClient):
    """The KPI sparkline sits beside "Active events", which counts the main
    branch only. Counting every branch made the series sum past that stat — a
    demo reset one minute old reported 26 across 14 days next to "ACTIVE EVENTS
    17" (tripl-jfm3.77)."""
    ctx = await _setup_metrics_project(client, slug="kpi-branch")
    resp = await client.post(
        "/api/v1/projects/kpi-branch/events",
        json={
            "event_type_id": ctx["page_type_id"],
            "name": "Alpha Viewed",
            "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
        },
    )
    assert resp.status_code == 201

    # A working branch deep-copies every event under a new branch_id.
    branch = await client.post(
        "/api/v1/projects/kpi-branch/branches",
        json={"name": "wip"},
    )
    assert branch.status_code == 201, branch.text

    resp = await client.get("/api/v1/projects/kpi-branch/overview/kpi-series?days=14")
    assert resp.status_code == 200
    # One event on main, one copy on the working branch — the series counts one.
    assert sum(resp.json()["new_events"]) == 1


async def _seed_platform_scan(
    client: AsyncClient, slug: str, *, platform_column: str | None
) -> tuple[str, dict[str, str]]:
    """Project + 2 events + a scan config (optionally platform-aware). Returns
    (scan_config_id, {event_name: event_id})."""
    proj = await client.post("/api/v1/projects", json={"name": "Platform", "slug": slug})
    project_id = proj.json()["id"]
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "track", "display_name": "Track"},
    )
    et_id = et.json()["id"]
    events: dict[str, str] = {}
    for name in ("checkout", "signup"):
        ev = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={"event_type_id": et_id, "name": name},
        )
        events[name] = ev.json()["id"]

    async with TestSessionLocal() as session:
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"Platform DS {uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=uuid.UUID(project_id),
            event_type_id=None,
            name="Platform Config",
            base_query="SELECT time, event_name FROM events",
            event_type_column="event_name",
            time_column="time",
            cardinality_threshold=100,
            interval="1h",
            platform_column=platform_column,
        )
        session.add_all([data_source, scan_config])
        # Flush parents first: SQLite enforces FK per-statement and the unit of
        # work has no ORM relationship linking scan_config to its metric/anomaly
        # rows, so without this the children can be inserted before scan_configs.
        await session.flush()
        scan_config_id = str(scan_config.id)
        await session.commit()
    return scan_config_id, events


async def test_platform_presence_matrix(client: AsyncClient) -> None:
    """Presence is derived from platform_column breakdown rows (event scope,
    count > 0); zero-count and non-platform breakdown rows are excluded."""
    scan_config_id, events = await _seed_platform_scan(
        client, "platform-presence", platform_column="platform"
    )
    bucket = datetime(2026, 1, 1, 10, tzinfo=UTC)
    rows = [
        ("checkout", "ios", 50),
        ("checkout", "android", 30),
        ("signup", "ios", 10),
        ("signup", "android", 0),  # zero count → not present
    ]
    async with TestSessionLocal() as session:
        for ev_name, platform, count in rows:
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=uuid.UUID(scan_config_id),
                    event_id=uuid.UUID(events[ev_name]),
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="platform",
                    breakdown_value=platform,
                    is_other=False,
                    count=count,
                )
            )
        # A non-platform breakdown row must be ignored by the matrix.
        session.add(
            EventMetricBreakdown(
                id=uuid.uuid4(),
                scan_config_id=uuid.UUID(scan_config_id),
                event_id=uuid.UUID(events["checkout"]),
                event_type_id=None,
                bucket=bucket,
                breakdown_column="country",
                breakdown_value="US",
                is_other=False,
                count=99,
            )
        )
        await session.commit()

    resp = await client.get(
        f"/api/v1/projects/platform-presence/scans/{scan_config_id}/platform-presence"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_column"] == "platform"
    assert body["platforms"] == ["android", "ios"]
    items = {it["event_name"]: it["present_platforms"] for it in body["items"]}
    assert items["checkout"] == ["android", "ios"]
    assert items["signup"] == ["ios"]  # android dropped (count 0)


async def test_platform_presence_empty_without_platform_column(client: AsyncClient) -> None:
    scan_config_id, _events = await _seed_platform_scan(
        client, "platform-presence-empty", platform_column=None
    )
    resp = await client.get(
        f"/api/v1/projects/platform-presence-empty/scans/{scan_config_id}/platform-presence"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["platform_column"] is None
    assert body["platforms"] == []
    assert body["items"] == []


# --- tripl-57g0: enum-shaped query params reject garbage at the edge ---------
#
# ``status`` and every ``scope_type`` below bind against a native Postgres enum
# column, so while they were declared ``str`` a typo travelled all the way to
# the driver and came back as a 500 ("not among the defined enum values"). Each
# test pins BOTH halves of the contract: garbage 422s, and the values that
# worked before still return 200 — the fix must reject junk, not narrow the
# accepted set.


class EnumParamCtx(TypedDict):
    slug: str
    event_id: str
    event_type_id: str
    scan_config_id: str


async def _setup_enum_param_project(client: AsyncClient, slug: str) -> EnumParamCtx:
    """Project + one live event + a scan config with its hourly metrics.

    That is the minimum every scan-scoped insights endpoint needs to get past
    its own 404/400 guards, so a non-422 response proves the enum value really
    reached the service rather than being short-circuited earlier.
    """
    setup = await _setup_metrics_project(client, slug=slug)
    event_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={
            "event_type_id": setup["page_type_id"],
            "name": "Checkout Started",
            "status": "live",
            "field_values": [{"field_definition_id": setup["page_field_id"], "value": "checkout"}],
        },
    )
    assert event_resp.status_code == 201, event_resp.text
    event_id = event_resp.json()["id"]
    scan_config_id = await _seed_group_metrics(
        setup["project_id"], [{"event_id": event_id, "counts": [4, 6]}]
    )
    return EnumParamCtx(
        slug=slug,
        event_id=event_id,
        event_type_id=setup["page_type_id"],
        scan_config_id=str(scan_config_id),
    )


@pytest.mark.asyncio
async def test_events_metrics_status_rejects_unknown_enum_member(client: AsyncClient) -> None:
    ctx = await _setup_enum_param_project(client, slug="enum-events-metrics")
    path = f"/api/v1/projects/{ctx['slug']}/events-metrics"

    rejected = await client.get(path, params={"status": "BOGUS"})
    assert rejected.status_code == 422, rejected.text

    # A repeated param must fail on the one bad member rather than quietly drop
    # it — silently widening the filter is the failure mode a list invites.
    partly_rejected = await client.get(path, params={"status": ["live", "BOGUS"]})
    assert partly_rejected.status_code == 422, partly_rejected.text

    accepted = await client.get(path, params={"status": ["live", "draft"]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"] == [
        _plain_point("2026-01-01T10:00:00Z", 4),
        _plain_point("2026-01-01T11:00:00Z", 6),
    ]

    # Filtering still narrows: the event is `live`, so `draft` alone matches nothing.
    narrowed = await client.get(path, params={"status": "draft"})
    assert narrowed.status_code == 200
    assert narrowed.json()["data"] == []


def _scope_type_cases(ctx: EnumParamCtx) -> list[tuple[str, str, dict[str, str]]]:
    """``(path, a scope_type that works today, the endpoint's other required params)``.

    One row per endpoint that takes a ``scope_type``. The valid value differs by
    endpoint because each supports its own subset of MetricScopeType; the point
    of the case list is that the subset is unchanged, only garbage is rejected.
    """
    base = f"/api/v1/projects/{ctx['slug']}"
    scan = f"{base}/scans/{ctx['scan_config_id']}"
    return [
        (
            f"{scan}/top-movers",
            "event",
            {"scope_ref": ctx["event_id"], "bucket": "2026-01-01T10:00:00Z"},
        ),
        (f"{scan}/seasonality", "event", {"scope_ref": ctx["event_id"]}),
        (
            f"{scan}/breakdown-timeline",
            "event",
            {
                "scope_ref": ctx["event_id"],
                "breakdown_column": "country",
                "breakdown_value": "US",
            },
        ),
        (f"{scan}/app-versions", "project_total", {}),
        (f"{scan}/release-regressions", "event", {}),
        (f"{base}/distribution-drifts", "event_type", {"scope_ref": ctx["event_type_id"]}),
    ]


@pytest.mark.asyncio
async def test_scope_type_query_params_reject_unknown_enum_member(client: AsyncClient) -> None:
    ctx = await _setup_enum_param_project(client, slug="enum-scope-type")

    for path, valid_scope, extra in _scope_type_cases(ctx):
        rejected = await client.get(path, params={**extra, "scope_type": "BOGUS"})
        assert rejected.status_code == 422, f"{path} accepted a bogus scope_type: {rejected.text}"

        accepted = await client.get(path, params={**extra, "scope_type": valid_scope})
        assert accepted.status_code == 200, (
            f"{path} rejected scope_type={valid_scope}: {accepted.text}"
        )


@pytest.mark.asyncio
async def test_app_versions_keeps_its_project_total_default(client: AsyncClient) -> None:
    """The default moved from the literal "project_total" to the enum member;
    omitting scope_type entirely must still resolve to the project-total scope."""
    ctx = await _setup_enum_param_project(client, slug="enum-app-versions-default")

    resp = await client.get(
        f"/api/v1/projects/{ctx['slug']}/scans/{ctx['scan_config_id']}/app-versions"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["scope_type"] == "project_total"


@pytest.mark.asyncio
async def test_project_total_band_follows_the_scope_false_positive_override(
    client: AsyncClient,
) -> None:
    """The chart band is ``expected ± sigma_threshold × stddev`` and is meant to
    read as "outside the band = flagged". A scope the false-positive ratchet has
    tightened is scored at ITS sigma, so serving the scan-wide value would draw a
    band narrower than the rule that produced the dots."""
    from tripl.models.anomaly_scope_override import AnomalyScopeOverride

    setup = await _setup_metrics_project(client, "metrics-scope-band")
    scan_config_id = await _seed_group_metrics(setup["project_id"], [], name="Band scan")

    before = await client.get("/api/v1/projects/metrics-scope-band/metrics/total")
    assert before.status_code == 200
    scan_sigma = before.json()["sigma_threshold"]

    async with TestSessionLocal() as session:
        session.add(
            AnomalyScopeOverride(
                id=uuid.uuid4(),
                project_id=uuid.UUID(setup["project_id"]),
                scan_config_id=scan_config_id,
                scope_type="project_total",
                scope_ref=str(scan_config_id),
                scope_name="Band scan",
                sigma_threshold=scan_sigma + 1.5,
                min_expected_count=99,
                false_positive_count=1,
            )
        )
        await session.commit()

    after = await client.get("/api/v1/projects/metrics-scope-band/metrics/total")

    assert after.status_code == 200
    assert after.json()["sigma_threshold"] == scan_sigma + 1.5
