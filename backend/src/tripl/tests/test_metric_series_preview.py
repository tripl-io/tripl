"""Metrics follow-ups: the series dry run and the Used-by catalog filter.

* ``POST /metrics/series-preview`` previews a draft ``fact`` or
  ``event_composition`` metric's series with the collector's own code and
  persists nothing.
* ``GET /metrics?fact_table_id=`` (F7) narrows the catalog to the metrics that
  read one fact table, either ratio operand included — the population the fact
  tables list's "Used by" count describes.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

import tripl.core.adapters.registry as adapter_registry
from tripl.core.adapters.base import ColumnInfo
from tripl.models.domain_enums import ScanInterval
from tripl.models.event_metric import EventMetric
from tripl.models.metric_value import MetricValue
from tripl.models.scan_config import ScanConfig
from tripl.tests.conftest import TestSessionLocal


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Followups Metrics", "slug": "followups-metrics", "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient, project: dict) -> dict:
    """A warehouse this project may use: bound to it through a ``ScanConfig``."""
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Followups CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(created["id"]),
                name="followups-binding",
                base_query="SELECT 1",
            )
        )
        await session.commit()
    return created


async def _create_fact_table(
    client: AsyncClient, slug: str, data_source_id: str, name: str
) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{slug}/fact-tables",
        json={
            "name": name,
            "display_name": name.title(),
            "sql": "SELECT created_at, amount, user_id FROM orders",
            "timestamp_column": "created_at",
            "data_source_id": data_source_id,
            "columns": [
                {"name": "created_at", "type": "timestamp"},
                {"name": "amount", "type": "number"},
                {"name": "user_id", "type": "string"},
            ],
            "identifier_columns": ["user_id"],
            "row_filters": [{"name": "big", "sql": "amount > 100"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def fact_table(client: AsyncClient, project: dict, data_source: dict) -> dict:
    return await _create_fact_table(client, project["slug"], data_source["id"], "orders")


@pytest.fixture
async def event(client: AsyncClient, project: dict) -> dict:
    type_resp = await client.post(
        f"/api/v1/projects/{project['slug']}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert type_resp.status_code == 201, type_resp.text
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/events",
        json={"event_type_id": type_resp.json()["id"], "name": "signup"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class _SeriesStubAdapter:
    """Warehouse stub for the fact series preview: one bucketed aggregate call."""

    def __init__(
        self,
        rows: list[tuple[object, ...]],
        *,
        columns: tuple[str, ...] = ("created_at", "amount", "user_id"),
        error: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.columns = columns
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        return [ColumnInfo(name=name, type_name="String") for name in self.columns]

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: object,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: object,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self.calls.append(
            {
                "sql": base_query,
                "time_column": time_column,
                "interval": interval,
                "measure": measure_column,
                "time_from": time_from,
                "time_to": time_to,
            }
        )
        if self.error is not None:
            raise self.error
        return ["bucket", "value"], [], self.rows

    def close(self) -> None:
        self.closed = True


def _patch_adapters(
    monkeypatch: pytest.MonkeyPatch, *adapters: _SeriesStubAdapter
) -> list[_SeriesStubAdapter]:
    """Hand out ``adapters`` in order, one per ``build_adapter`` call."""
    queue = list(adapters)
    built: list[_SeriesStubAdapter] = []

    def _build(_ds: object) -> _SeriesStubAdapter:
        adapter = queue.pop(0)
        built.append(adapter)
        return adapter

    monkeypatch.setattr(adapter_registry, "build_adapter", _build)
    return built


def _series_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/metrics/series-preview"


def _bucket(hour: int) -> datetime:
    return datetime(2026, 9, 1, hour, tzinfo=UTC)


class TestFactSeriesPreview:
    async def test_single_runs_the_collector_aggregate_and_stores_nothing(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _SeriesStubAdapter([(_bucket(10), 5), (_bucket(11), None), (_bucket(12), "7.5")])
        _patch_adapters(monkeypatch, adapter)

        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "fact",
                "composition": "single",
                "interval": "1h",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "measure_column": "amount",
                "row_filters": ["big"],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        # A NULL aggregate cell is an absent bucket, as in a collection.
        assert [point["value"] for point in body["points"]] == [5.0, 7.5]
        assert body["point_count"] == 2

        call = adapter.calls[0]
        assert call["measure"] == "amount"
        assert call["time_column"] == "created_at"
        # The named filter compiled into the collector's filtered wrapper.
        assert "amount > 100" in str(call["sql"])
        assert call["time_to"] - call["time_from"] == timedelta(hours=50)  # type: ignore[operator]
        assert adapter.closed is True

        async with TestSessionLocal() as session:
            stored = await session.scalar(select(func.count(MetricValue.id)))
        assert stored == 0

    async def test_ratio_divides_numerator_by_denominator(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        numerator = _SeriesStubAdapter([(_bucket(10), 2), (_bucket(11), 3)])
        denominator = _SeriesStubAdapter([(_bucket(10), 4), (_bucket(11), 0)])
        _patch_adapters(monkeypatch, numerator, denominator)

        operand = {"fact_table_id": fact_table["id"]}
        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "fact",
                "composition": "ratio",
                "interval": "1h",
                "numerator": {**operand, "aggregation": "sum", "measure_column": "amount"},
                "denominator": {**operand, "aggregation": "count"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        # 2/4 at 10:00; 3/0 is divide-by-zero, a gap rather than a point.
        assert [point["value"] for point in body["points"]] == [0.5]

    async def test_measure_the_query_no_longer_returns_is_an_inline_error(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _SeriesStubAdapter([], columns=("created_at", "user_id"))
        _patch_adapters(monkeypatch, adapter)

        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "fact",
                "interval": "1h",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "measure_column": "amount",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["points"] == []
        assert "amount" in body["error"]
        assert adapter.calls == []

    async def test_warehouse_failure_is_a_200_with_a_masked_error(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _SeriesStubAdapter(
            [], error=RuntimeError("Connection refused to 10.1.2.3:9440 as analytics_admin")
        )
        _patch_adapters(monkeypatch, adapter)

        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "fact",
                "interval": "1h",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
            },
        )
        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert "10.1.2.3" not in error
        assert "analytics_admin" not in error
        assert adapter.closed is True

    async def test_unknown_named_filter_fails_before_any_connection(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        built = _patch_adapters(monkeypatch)

        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "fact",
                "interval": "1h",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "row_filters": ["no_such_filter"],
            },
        )
        assert resp.status_code == 200, resp.text
        assert "no_such_filter" in resp.json()["error"]
        assert built == []

    async def test_fact_table_of_another_project_is_404(
        self,
        client: AsyncClient,
        project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _patch_adapters(monkeypatch)
        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "fact",
                "interval": "1h",
                "fact_table_id": str(uuid.uuid4()),
                "aggregation": "count",
            },
        )
        assert resp.status_code == 404, resp.text

    async def test_sql_kind_is_not_accepted_here(self, client: AsyncClient, project: dict):
        resp = await client.post(
            _series_url(project["slug"]),
            json={"kind": "sql", "interval": "1h"},
        )
        assert resp.status_code == 422


async def _seed_counts(
    project_id: str,
    data_source_id: str,
    *,
    event_id: str,
    counts: dict[int, int],
    name: str = "followups-grid",
) -> uuid.UUID:
    async with TestSessionLocal() as session:
        grid = ScanConfig(
            project_id=uuid.UUID(project_id),
            data_source_id=uuid.UUID(data_source_id),
            name=name,
            base_query="SELECT 1",
            time_column="ts",
            interval=ScanInterval.h1,
        )
        session.add(grid)
        await session.flush()
        for hour, count in counts.items():
            session.add(
                EventMetric(
                    scan_config_id=grid.id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=_bucket(hour),
                    count=count,
                )
            )
        await session.commit()
        return grid.id


class TestEventCompositionSeriesPreview:
    async def test_single_reads_the_collected_counts(
        self, client: AsyncClient, project: dict, data_source: dict, event: dict
    ):
        await _seed_counts(
            project["id"], data_source["id"], event_id=event["id"], counts={9: 3, 10: 4}
        )

        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "event_composition",
                "composition": "single",
                "numerator_event_id": event["id"],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        assert [point["value"] for point in body["points"]] == [3.0, 4.0]
        async with TestSessionLocal() as session:
            stored = await session.scalar(select(func.count(MetricValue.id)))
        assert stored == 0

    async def test_window_is_bounded_to_the_newest_fifty_buckets(
        self, client: AsyncClient, project: dict, data_source: dict, event: dict
    ):
        # 60 hourly buckets ending at 12:00 on day 3; only the newest 50 show.
        async with TestSessionLocal() as session:
            grid = ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(data_source["id"]),
                name="long-grid",
                base_query="SELECT 1",
                time_column="ts",
                interval=ScanInterval.h1,
            )
            session.add(grid)
            await session.flush()
            head = datetime(2026, 9, 3, 12, tzinfo=UTC)
            for offset in range(60):
                session.add(
                    EventMetric(
                        scan_config_id=grid.id,
                        event_id=uuid.UUID(event["id"]),
                        event_type_id=None,
                        bucket=head - timedelta(hours=offset),
                        count=offset + 1,
                    )
                )
            await session.commit()

        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "event_composition",
                "composition": "single",
                "numerator_event_id": event["id"],
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["point_count"] == 50

    async def test_no_counts_yet_says_so(self, client: AsyncClient, project: dict, event: dict):
        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "event_composition",
                "composition": "single",
                "numerator_event_id": event["id"],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["points"] == []
        assert "No counts" in body["error"]

    async def test_event_outside_the_project_is_refused_like_a_save(
        self, client: AsyncClient, project: dict
    ):
        resp = await client.post(
            _series_url(project["slug"]),
            json={
                "kind": "event_composition",
                "composition": "single",
                "numerator_event_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 422, resp.text


class TestFactTableUsedByFilter:
    async def test_lists_the_metrics_that_read_the_table_either_operand(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        fact_table: dict,
    ):
        other = await _create_fact_table(client, project["slug"], data_source["id"], "refunds")
        url = f"/api/v1/projects/{project['slug']}/metrics"
        single = await client.post(
            url,
            json={
                "kind": "fact",
                "name": "order_count",
                "display_name": "Order count",
                "interval": "1h",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
            },
        )
        assert single.status_code == 201, single.text
        # The other table appears only as the denominator, i.e. only in config.
        ratio = await client.post(
            url,
            json={
                "kind": "fact",
                "name": "refund_rate",
                "display_name": "Refund rate",
                "interval": "1h",
                "composition": "ratio",
                "numerator": {"fact_table_id": fact_table["id"], "aggregation": "count"},
                "denominator": {"fact_table_id": other["id"], "aggregation": "count"},
            },
        )
        assert ratio.status_code == 201, ratio.text

        by_orders = await client.get(url, params={"fact_table_id": fact_table["id"]})
        assert by_orders.status_code == 200, by_orders.text
        assert {item["name"] for item in by_orders.json()["items"]} == {
            "order_count",
            "refund_rate",
        }
        assert by_orders.json()["total"] == 2

        by_refunds = await client.get(url, params={"fact_table_id": other["id"]})
        assert [item["name"] for item in by_refunds.json()["items"]] == ["refund_rate"]
        assert by_refunds.json()["total"] == 1

        # The count agrees with the fact tables list's own "Used by".
        tables = await client.get(f"/api/v1/projects/{project['slug']}/fact-tables")
        counts = {item["id"]: item["metric_count"] for item in tables.json()["items"]}
        assert counts[other["id"]] == 1
        assert counts[fact_table["id"]] == 2

    async def test_unused_table_lists_nothing(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.get(
            f"/api/v1/projects/{project['slug']}/metrics",
            params={"fact_table_id": fact_table["id"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["items"] == []
        assert resp.json()["total"] == 0
