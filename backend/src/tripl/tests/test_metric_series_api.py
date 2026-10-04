"""API tests for the catalog-metric series reads + list enrichment.

Seeds ``MetricValue`` / ``MetricValueBreakdown`` rows directly (the collection
worker is ticket .5, not exercised here) plus a ``MetricAnomaly`` row keyed by
``scope_ref = str(metric_definition_id)`` to exercise the defensive
anomaly-join seam documented in ``metric_series_service``.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricComposition, MetricKind, MetricStatus
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.scan_config import ScanConfig
from tripl.services.metric_definition_service import _SPARK_POINTS
from tripl.tests.conftest import TestSessionLocal

# Three 1h-aligned buckets; b1 is intentionally left without a stored value so
# the densify-to-grid step has to fill it with 0.0. Anchored to recent wall-clock
# hours so the latest anomaly (on B2) stays inside the signal freshness horizon and
# surfaces as an open "latest_scan" signal; the 1h spacing is preserved.
_SERIES_BASE = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=4)
B0 = _SERIES_BASE
B1 = _SERIES_BASE + timedelta(hours=1)
B2 = _SERIES_BASE + timedelta(hours=2)


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Series Test", "slug": "series-test", "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient, project: dict) -> dict:
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Series CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201
    created = resp.json()
    # A data source belongs to a project through a ScanConfig; the sql-metric save
    # path refuses an unbound id, because that id selects the warehouse credential
    # the metric's free-text SELECT will run under on the catalog beat.
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(created["id"]),
                name="series-ds-binding",
                base_query="SELECT 1",
            )
        )
        await session.commit()
    return created


def _metrics_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/metrics"


async def _create_sql_metric(
    client: AsyncClient,
    slug: str,
    data_source_id: str,
    name: str,
    **extra: object,
) -> dict:
    payload: dict = {
        "kind": "sql",
        "name": name,
        "display_name": name.upper(),
        "data_source_id": data_source_id,
        "interval": "1h",
        "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        # ACTIVE, because these tests seed values and anomalies and then assert a
        # SIGNAL. ``MetricDefinitionCreate.status`` defaults to ``draft``, and a
        # draft metric is neither collected (``check_metric_definitions_due``
        # dispatches only ``active``) nor scored, so a draft metric holding a
        # fresh anomaly is a state the pipeline cannot produce — and one that is
        # deliberately signal-less on every surface.
        "status": "active",
        **extra,
    }
    resp = await client.post(_metrics_url(slug), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seed_scan_config(
    project_id: str,
    *,
    interval: str = "1h",
    data_source_id: str | None = None,
    app_version_column: str | None = None,
) -> uuid.UUID:
    """A bare scan config to satisfy MetricAnomaly.scan_config_id (non-nullable)."""
    scan_config_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        data_source = None
        resolved_data_source_id = uuid.UUID(data_source_id) if data_source_id is not None else None
        if resolved_data_source_id is None:
            data_source = DataSource(
                id=uuid.uuid4(),
                name=f"Series DS {uuid.uuid4().hex[:8]}",
                db_type="clickhouse",
                host="localhost",
                port=8123,
                database_name="default",
                username="default",
                password_encrypted="",
            )
            resolved_data_source_id = data_source.id
        scan_config = ScanConfig(
            id=scan_config_id,
            data_source_id=resolved_data_source_id,
            project_id=uuid.UUID(project_id),
            name=f"Series Config {uuid.uuid4().hex[:8]}",
            base_query="SELECT time, v FROM t",
            interval=interval,
            app_version_column=app_version_column,
        )
        session.add(scan_config)
        if data_source is not None:
            session.add(data_source)
        await session.commit()
    return scan_config_id


async def _seed_event_composition_metric(
    project_id: str,
    name: str,
    *,
    app_version_column: str | None = None,
) -> str:
    """Seed an ``event_composition`` metric (interval NULL) straight to the DB.

    Composition metrics are normally created via the API with real event refs;
    here the interval-resolution seam only needs a row whose ``interval`` is NULL
    so the grid is derived from the values' ``scan_config_id``.
    """
    metric_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add(
            MetricDefinition(
                id=metric_id,
                project_id=uuid.UUID(project_id),
                name=name,
                display_name=name.upper(),
                kind=MetricKind.event_composition,
                composition=MetricComposition.single,
                config={},
                interval=None,
                app_version_column=app_version_column,
                # Same reason as _create_sql_metric: the column default is
                # ``draft``, which is not a monitored metric.
                status=MetricStatus.active.value,
            )
        )
        await session.commit()
    return str(metric_id)


async def _seed_metric_values(
    metric_id: str,
    rows: list[tuple[datetime, float]],
    *,
    scan_config_id: uuid.UUID | None = None,
) -> None:
    async with TestSessionLocal() as session:
        for bucket, value in rows:
            session.add(
                MetricValue(
                    id=uuid.uuid4(),
                    metric_definition_id=uuid.UUID(metric_id),
                    scan_config_id=scan_config_id,
                    bucket=bucket,
                    value=value,
                )
            )
        await session.commit()


async def _seed_metric_anomaly(
    *,
    scan_config_id: uuid.UUID,
    metric_id: str,
    bucket: datetime,
    direction: str,
    actual_count: int,
    expected_count: float,
    scope_type: str = "metric",
) -> None:
    # Catalog-metric anomalies are stored under scope_type='metric' /
    # scope_ref=str(metric_definition_id); the series read matches on both.
    async with TestSessionLocal() as session:
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=scan_config_id,
                scope_type=scope_type,
                scope_ref=str(metric_id),
                event_id=None,
                event_type_id=None,
                bucket=bucket,
                actual_count=actual_count,
                expected_count=expected_count,
                stddev=1.0,
                z_score=5.0 if direction == "spike" else -5.0,
                direction=direction,
            )
        )
        await session.commit()


async def _seed_breakdowns(
    metric_id: str,
    rows: list[tuple[str, str, datetime, float]],
    *,
    scan_config_id: uuid.UUID | None = None,
) -> None:
    async with TestSessionLocal() as session:
        for column, value, bucket, amount in rows:
            session.add(
                MetricValueBreakdown(
                    id=uuid.uuid4(),
                    metric_definition_id=uuid.UUID(metric_id),
                    scan_config_id=scan_config_id,
                    bucket=bucket,
                    breakdown_column=column,
                    breakdown_value=value,
                    is_other=False,
                    value=amount,
                )
            )
        await session.commit()


async def _seed_project_version_totals(
    scan_config_id: uuid.UUID,
    rows: list[tuple[str, datetime, int]],
) -> None:
    async with TestSessionLocal() as session:
        # The project-total version query keys off (event_id IS NULL,
        # event_type_id IS NOT NULL) rows (see metric_series_service), so the
        # breakdown needs a real event type — a bare uuid4 dangles the FK. It's
        # grouped by breakdown_value/bucket, not event_type_id, so one shared type
        # in the scan's project suffices. Flush it before its child breakdown rows.
        scan_config = await session.get(ScanConfig, scan_config_id)
        assert scan_config is not None
        event_type = EventType(
            id=uuid.uuid4(),
            project_id=scan_config.project_id,
            name=f"version-totals-{uuid.uuid4().hex[:8]}",
            display_name="Version Totals",
        )
        session.add(event_type)
        await session.flush()
        for version, bucket, count in rows:
            session.add(
                EventMetricBreakdown(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    event_id=None,
                    event_type_id=event_type.id,
                    bucket=bucket,
                    breakdown_column="app_version",
                    breakdown_value=version,
                    is_other=False,
                    count=count,
                )
            )
        await session.commit()


class TestMetricSeries:
    async def test_series_densified_with_anomaly_flags(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "lat")
        scan_config_id = await _seed_scan_config(project["id"])
        await _seed_metric_values(metric["id"], [(B0, 10.0), (B2, 4.0)])
        await _seed_metric_anomaly(
            scan_config_id=scan_config_id,
            metric_id=metric["id"],
            bucket=B0,
            direction="spike",
            actual_count=10,
            expected_count=3.0,
        )
        await _seed_metric_anomaly(
            scan_config_id=scan_config_id,
            metric_id=metric["id"],
            bucket=B2,
            direction="drop",
            actual_count=4,
            expected_count=12.0,
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["metric_id"] == metric["id"]
        assert body["scope"] == "metric"
        assert body["interval"] == "1h"

        # b1 was never stored. A ``sql`` metric is fractional (value-kind, ticket
        # .6): gaps are NOT zero-filled — they stay absent (a null break in the
        # chart) so a missing bucket never reads as a drop to 0.
        assert [point["value"] for point in body["data"]] == [10.0, 4.0]

        spike, drop = body["data"]
        assert spike["is_anomaly"] is True
        assert spike["anomaly_direction"] == "spike"
        assert drop["is_anomaly"] is True
        assert drop["anomaly_direction"] == "drop"

        # Latest anomaly sits on the latest value bucket → an open "latest_scan" signal.
        assert body["latest_signal"] is not None
        assert body["latest_signal"]["direction"] == "drop"
        assert body["latest_signal"]["scope_ref"] == metric["id"]

    async def test_series_ignores_foreign_scope_with_colliding_scope_ref(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """A non-metric anomaly reusing the metric UUID as scope_ref must NOT flag.

        Review finding: the series read filters on scope_type='metric'
        too, so a row from another scope that happens to carry the metric UUID is
        excluded rather than surfaced as a false anomaly.
        """
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "iso2")
        scan_config_id = await _seed_scan_config(project["id"])
        await _seed_metric_values(metric["id"], [(B0, 10.0), (B2, 4.0)])
        await _seed_metric_anomaly(
            scan_config_id=scan_config_id,
            metric_id=metric["id"],
            bucket=B0,
            direction="spike",
            actual_count=10,
            expected_count=3.0,
            scope_type="event",  # foreign scope reusing the metric UUID
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert all(point["is_anomaly"] is False for point in body["data"])
        assert body["latest_signal"] is None

    async def test_series_empty_when_no_values(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "empty")
        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"] == []
        assert body["latest_signal"] is None

    async def test_series_unknown_metric_404(self, client: AsyncClient, project: dict):
        resp = await client.get(f"{_metrics_url(project['slug'])}/{uuid.uuid4()}/series")
        assert resp.status_code == 404, resp.text

    async def test_series_cross_project_404(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        other = await client.post(
            "/api/v1/projects",
            json={"name": "Other", "slug": "series-other", "description": ""},
        )
        assert other.status_code == 201
        metric = await _create_sql_metric(client, project["slug"], data_source["id"], "iso")
        resp = await client.get(f"{_metrics_url('series-other')}/{metric['id']}/series")
        assert resp.status_code == 404, resp.text


class TestMetricIntervalResolution:
    async def test_event_composition_grid_follows_latest_scan_config(
        self, client: AsyncClient, project: dict
    ):
        """A composition metric collected under two scan_configs must resolve its
        grid to the MOST-RECENT one, not an arbitrary ``LIMIT 1`` row."""
        slug = project["slug"]
        metric_id = await _seed_event_composition_metric(project["id"], "comp")
        # "old" carries the earlier bucket; "new" the later one. Seed old FIRST so
        # an unordered ``LIMIT 1`` would wrongly return the old config's grid.
        old_config = await _seed_scan_config(project["id"], interval="1h")
        new_config = await _seed_scan_config(project["id"], interval="1d")
        await _seed_metric_values(metric_id, [(B0, 10.0)], scan_config_id=old_config)
        await _seed_metric_values(metric_id, [(B2, 4.0)], scan_config_id=new_config)

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/series")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The latest bucket (B2) was collected under new_config → its grid wins.
        assert body["scan_config_id"] == str(new_config)
        assert body["interval"] == "1d"


class TestMetricBreakdowns:
    async def test_breakdown_series(self, client: AsyncClient, project: dict, data_source: dict):
        slug = project["slug"]
        metric = await _create_sql_metric(
            client, slug, data_source["id"], "bd", breakdown_columns=["country"]
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("country", "US", B0, 3.0),
                ("country", "US", B2, 5.0),
                ("country", "CA", B0, 1.0),
            ],
        )
        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/breakdowns")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == ["country"]
        assert body["selected_column"] == "country"
        series_by_value = {item["breakdown_value"]: item for item in body["series"]}
        assert set(series_by_value) == {"US", "CA"}
        assert series_by_value["US"]["total_value"] == 8.0
        # Highest-volume series sorts first.
        assert body["series"][0]["breakdown_value"] == "US"

    async def test_breakdown_unknown_column_400(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(
            client, slug, data_source["id"], "bd2", breakdown_columns=["country"]
        )
        resp = await client.get(
            f"{_metrics_url(slug)}/{metric['id']}/breakdowns", params={"column": "ghost"}
        )
        assert resp.status_code == 400, resp.text


class TestMetricVersionSeries:
    async def test_version_series(self, client: AsyncClient, project: dict, data_source: dict):
        slug = project["slug"]
        metric = await _create_sql_metric(
            client, slug, data_source["id"], "ver", app_version_column="app_version"
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("app_version", "1.0.0", B0, 2.0),
                ("app_version", "1.1.0", B0, 3.0),
            ],
        )
        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/versions")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["app_version_column"] == "app_version"
        assert body["latest_version"] == "1.1.0"
        versions = {item["version"]: item for item in body["versions"]}
        assert versions["1.1.0"]["is_latest"] is True
        # Until a matching project-total scan has collected rows, a fractional
        # metric must not interpret ratio values as rollout traffic.
        assert versions["1.1.0"]["is_active"] is True
        assert {item["version"] for item in body["series"]} == {"1.0.0", "1.1.0"}

    async def test_ratio_versions_use_project_total_release_maturity(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(
            client,
            slug,
            data_source["id"],
            "ratio-ver",
            app_version_column="app_version",
        )
        scan_config_id = await _seed_scan_config(
            project["id"],
            data_source_id=data_source["id"],
            app_version_column="app_version",
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("app_version", "1.0.0", B0, 0.9),
                ("app_version", "1.0.0", B1, 0.9),
                ("app_version", "2.0.0", B0, 0.1),
                ("app_version", "2.0.0", B1, 0.1),
            ],
        )
        await _seed_project_version_totals(
            scan_config_id,
            [
                ("1.0.0", B0, 10),
                ("1.0.0", B1, 10),
                ("2.0.0", B0, 990),
                ("2.0.0", B1, 990),
            ],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/versions")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["latest_version"] == "2.0.0"
        versions = {item["version"]: item for item in body["versions"]}
        assert versions["2.0.0"]["is_latest"] is True
        assert versions["2.0.0"]["is_active"] is True
        assert versions["1.0.0"]["is_active"] is False

    async def test_ratio_versions_choose_one_canonical_project_total_scan(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(
            client,
            slug,
            data_source["id"],
            "canonical-ver",
            app_version_column="app_version",
        )
        broad_scan_id = await _seed_scan_config(
            project["id"],
            data_source_id=data_source["id"],
            app_version_column="app_version",
        )
        narrow_scan_id = await _seed_scan_config(
            project["id"],
            data_source_id=data_source["id"],
            app_version_column="app_version",
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("app_version", "1.0.0", B0, 0.9),
                ("app_version", "1.0.0", B1, 0.9),
                ("app_version", "2.0.0", B0, 0.1),
                ("app_version", "2.0.0", B1, 0.1),
            ],
        )
        await _seed_project_version_totals(
            broad_scan_id,
            [
                ("1.0.0", B0, 9000),
                ("1.0.0", B1, 9000),
                ("2.0.0", B0, 1),
                ("2.0.0", B1, 1),
            ],
        )
        await _seed_project_version_totals(
            narrow_scan_id,
            [
                ("1.0.0", B0, 10),
                ("1.0.0", B1, 10),
                ("2.0.0", B0, 900),
                ("2.0.0", B1, 900),
            ],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/versions")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["latest_version"] == "1.0.0"
        versions = {item["version"]: item for item in body["versions"]}
        assert versions["1.0.0"]["is_active"] is True
        assert versions["2.0.0"]["is_active"] is False

    async def test_scan_bound_metric_uses_its_exact_project_total_scan(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric_id = await _seed_event_composition_metric(
            project["id"],
            "bound-ver",
            app_version_column="app_version",
        )
        bound_scan_id = await _seed_scan_config(
            project["id"],
            data_source_id=data_source["id"],
            app_version_column="app_version",
        )
        unrelated_scan_id = await _seed_scan_config(
            project["id"],
            data_source_id=data_source["id"],
            app_version_column="app_version",
        )
        await _seed_metric_values(metric_id, [(B0, 1.0)], scan_config_id=bound_scan_id)
        await _seed_breakdowns(
            metric_id,
            [
                ("app_version", "1.0.0", B0, 0.9),
                ("app_version", "2.0.0", B0, 0.1),
            ],
            scan_config_id=bound_scan_id,
        )
        await _seed_project_version_totals(
            bound_scan_id,
            [
                ("1.0.0", B0, 900),
                ("1.0.0", B1, 900),
                ("2.0.0", B0, 1),
                ("2.0.0", B1, 1),
            ],
        )
        await _seed_project_version_totals(
            unrelated_scan_id,
            [
                ("1.0.0", B0, 10),
                ("1.0.0", B1, 10),
                ("2.0.0", B0, 9000),
                ("2.0.0", B1, 9000),
            ],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/versions")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["latest_version"] == "1.0.0"
        versions = {item["version"]: item for item in body["versions"]}
        assert versions["1.0.0"]["is_active"] is True
        assert versions["2.0.0"]["is_active"] is False

    async def test_standalone_metric_uses_project_version_retention(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        update = await client.patch(
            f"/api/v1/projects/{slug}",
            json={"app_version_keep_releases": 1},
        )
        assert update.status_code == 200, update.text
        metric = await _create_sql_metric(
            client,
            slug,
            data_source["id"],
            "project-ver",
            app_version_column="app_version",
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("app_version", "1.0.0", B0, 2.0),
                ("app_version", "1.1.0", B0, 3.0),
                ("app_version", "2.0.0", B0, 4.0),
            ],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/versions")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [item["version"] for item in body["versions"]] == ["2.0.0", "Other"]
        series_by_version = {item["version"]: item for item in body["series"]}
        assert set(series_by_version) == {"2.0.0", "Other"}
        # The MEAN of the folded versions (2.0 and 3.0), not their sum. This is a
        # sql metric, so its values are fractional and adding them together makes
        # a number nothing in the product means — the "Other" line has to stay on
        # the same scale as the named ones. Reverting the fold
        # to a sum puts 5.0 back here.
        assert series_by_version["Other"]["total_value"] == pytest.approx(2.5)

    async def test_version_series_empty_when_no_column(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "nover")
        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/versions")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["app_version_column"] is None
        assert body["versions"] == []
        assert body["series"] == []


class TestListEnrichment:
    async def test_list_includes_latest_value_and_signal(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        seeded = await _create_sql_metric(client, slug, data_source["id"], "seeded")
        bare = await _create_sql_metric(client, slug, data_source["id"], "bare")
        scan_config_id = await _seed_scan_config(project["id"])
        await _seed_metric_values(seeded["id"], [(B0, 10.0), (B2, 4.0)])
        await _seed_metric_anomaly(
            scan_config_id=scan_config_id,
            metric_id=seeded["id"],
            bucket=B2,
            direction="drop",
            actual_count=4,
            expected_count=12.0,
        )

        resp = await client.get(_metrics_url(slug))
        assert resp.status_code == 200, resp.text
        items = {item["id"]: item for item in resp.json()["items"]}

        seeded_item = items[seeded["id"]]
        assert seeded_item["latest_value"] == 4.0
        assert seeded_item["spark"] == [10.0, 4.0]
        assert seeded_item["latest_signal"] is not None
        assert seeded_item["latest_signal"]["direction"] == "drop"

        bare_item = items[bare["id"]]
        assert bare_item["latest_value"] is None
        assert bare_item["spark"] == []
        assert bare_item["latest_signal"] is None

    async def test_spark_bounded_to_last_n_points(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """With more history than ``_SPARK_POINTS``, the window-bounded query keeps
        only the trailing N values per metric — newest tail, ascending, latest last."""
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "deep")
        total_points = _SPARK_POINTS + 5
        await _seed_metric_values(
            metric["id"],
            [(B0 + timedelta(hours=i), float(i)) for i in range(total_points)],
        )

        resp = await client.get(_metrics_url(slug))
        assert resp.status_code == 200, resp.text
        item = {row["id"]: row for row in resp.json()["items"]}[metric["id"]]

        # Bounded to the trailing N, in ascending bucket order, with the most
        # recent bucket's value as both the spark tail and ``latest_value``.
        expected_tail = [float(i) for i in range(total_points - _SPARK_POINTS, total_points)]
        assert len(item["spark"]) == _SPARK_POINTS
        assert item["spark"] == expected_tail
        assert item["latest_value"] == float(total_points - 1)


class TestRoutePrecedence:
    async def test_series_route_does_not_shadow_crud(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "prec")

        # The single-segment CRUD route still returns the definition itself.
        crud = await client.get(f"{_metrics_url(slug)}/{metric['id']}")
        assert crud.status_code == 200, crud.text
        assert crud.json()["id"] == metric["id"]
        assert "kind" in crud.json()

        # The two-segment series route resolves to the series handler.
        series = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")
        assert series.status_code == 200, series.text
        assert series.json()["metric_id"] == metric["id"]
