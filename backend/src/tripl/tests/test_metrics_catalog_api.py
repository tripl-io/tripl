import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

import tripl.core.adapters.registry as adapter_registry
import tripl.worker.tasks.metrics.metric_collect as mc
from tripl.api.deps import get_current_user
from tripl.core.adapters.measure_validator import SqlDialect
from tripl.main import app
from tripl.models.domain_enums import AnomalyDirection, MetricScopeType
from tripl.models.fact_table import FactTable
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.scan_config import ScanConfig
from tripl.models.user import User
from tripl.services import metric_definition_service
from tripl.tests._members import persisted_member_user
from tripl.tests.conftest import TestSessionLocal


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Metrics Test", "slug": "metrics-test", "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient, project: dict) -> dict:
    """A data source THIS PROJECT is allowed to use.

    A data source is global; a project owns it only through a ``ScanConfig``
    binding, and the sql-metric save and preview paths enforce that now for the
    same multi-tenant reason the fact-table paths always did — an editor in one
    project must not be able to run, or schedule, SQL against another project's
    warehouse credential. Seeded here so every sql-metric fixture below keeps
    working; ``bound_fact_table`` adds its own binding on the same source under a
    different name, because the unique constraint is (data_source_id, name).
    """
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Test CH",
            "db_type": "clickhouse",
            "host": "localhost",
            "port": 8123,
            "database_name": "test_db",
        },
    )
    assert resp.status_code == 201
    created = resp.json()
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(created["id"]),
                name="metrics-ds-binding",
                base_query="SELECT 1",
            )
        )
        await session.commit()
    return created


@pytest.fixture
async def fact_table(client: AsyncClient, project: dict) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/fact-tables",
        json={
            "name": "orders_ft",
            "display_name": "Orders",
            "sql": "SELECT created_at, amount, user_id FROM orders",
            "timestamp_column": "created_at",
            "columns": [
                {"name": "created_at", "type": "timestamp"},
                {"name": "amount", "type": "number"},
                {"name": "user_id", "type": "string"},
            ],
            "identifier_columns": ["user_id"],
            "row_filters": [{"name": "exclude_test", "sql": "is_test = 0"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def bound_fact_table(client: AsyncClient, project: dict, data_source: dict) -> dict:
    """A fact table BOUND to a data source — required to preview its filters.

    A data source is scoped to a project through a ``ScanConfig`` (multi-tenant
    isolation), so the membership link is seeded first; without it the fact-table
    create rejects the ``data_source_id`` with a 404.
    """
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project["id"]),
                data_source_id=uuid.UUID(data_source["id"]),
                name="scan-binding",
                base_query="SELECT 1",
            )
        )
        await session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/fact-tables",
        json={
            "name": "bound_orders_ft",
            "display_name": "Bound Orders",
            "sql": "SELECT created_at, amount, user_id, is_test FROM orders",
            "timestamp_column": "created_at",
            "data_source_id": data_source["id"],
            "columns": [
                {"name": "created_at", "type": "timestamp"},
                {"name": "amount", "type": "number"},
                {"name": "user_id", "type": "string"},
                {"name": "is_test", "type": "number"},
            ],
            "identifier_columns": ["user_id"],
            "row_filters": [{"name": "exclude_test", "sql": "is_test = 0"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def event_type(client: AsyncClient, project: dict) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture
async def event(client: AsyncClient, project: dict, event_type: dict) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/events",
        json={"event_type_id": event_type["id"], "name": "signup"},
    )
    assert resp.status_code == 201
    return resp.json()


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
        "interval": "1d",
        "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        **extra,
    }
    resp = await client.post(_metrics_url(slug), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_multi_filter_fact_table(client: AsyncClient, slug: str) -> dict:
    """A fact table with two named row filters (for multi-filter metric tests)."""
    resp = await client.post(
        f"/api/v1/projects/{slug}/fact-tables",
        json={
            "name": "orders_multi_ft",
            "display_name": "Orders Multi",
            "sql": "SELECT created_at, amount, user_id FROM orders",
            "timestamp_column": "created_at",
            "columns": [
                {"name": "created_at", "type": "timestamp"},
                {"name": "amount", "type": "number"},
                {"name": "user_id", "type": "string"},
            ],
            "identifier_columns": ["user_id"],
            "row_filters": [
                {"name": "positive", "sql": "amount > 0"},
                {"name": "big", "sql": "amount > 100"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreateHappyPaths:
    async def test_create_fact_single_sum(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "revenue",
                "display_name": "Revenue",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
                "measure_column": "amount",
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["kind"] == "fact"
        assert data["aggregation"] == "sum"
        assert data["composition"] == "single"
        assert data["fact_table_id"] == fact_table["id"]
        # The data source is taken from the fact table, not stored on the metric.
        assert data["data_source_id"] is None
        assert data["interval"] == "1h"
        assert data["config"]["measure_column"] == "amount"
        assert data["status"] == "draft"
        assert data["project_id"] == project["id"]

    async def test_create_fact_single_count_without_measure(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "orders_count",
                "display_name": "Orders",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1d",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["config"].get("measure_column") is None

    async def test_create_fact_single_with_row_filter(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "clean_revenue",
                "display_name": "Clean Revenue",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
                "measure_column": "amount",
                "row_filter": "exclude_test",
            },
        )
        assert resp.status_code == 201, resp.text
        # Legacy single ``row_filter`` is folded into the effective ``row_filters``.
        config = resp.json()["config"]
        assert config["row_filters"] == ["exclude_test"]
        assert config["filter_sql"] is None

    async def test_create_fact_single_with_multiple_row_filters(
        self, client: AsyncClient, project: dict
    ):
        fact_table = await _create_multi_filter_fact_table(client, project["slug"])
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "big_positive_orders",
                "display_name": "Big positive orders",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "row_filters": ["positive", "big"],
            },
        )
        assert resp.status_code == 201, resp.text
        config = resp.json()["config"]
        assert config["row_filters"] == ["positive", "big"]
        assert config["filter_sql"] is None

    async def test_create_fact_single_with_filter_sql(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "free_text_revenue",
                "display_name": "Free-text Revenue",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
                "measure_column": "amount",
                "filter_sql": "amount > 0",
            },
        )
        assert resp.status_code == 201, resp.text
        config = resp.json()["config"]
        assert config["row_filters"] == []
        assert config["filter_sql"] == "amount > 0"

    async def test_create_fact_single_with_row_filters_and_filter_sql(
        self, client: AsyncClient, project: dict
    ):
        fact_table = await _create_multi_filter_fact_table(client, project["slug"])
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "combo_filtered",
                "display_name": "Combo filtered",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "row_filters": ["positive"],
                "filter_sql": "amount < 1000",
            },
        )
        assert resp.status_code == 201, resp.text
        config = resp.json()["config"]
        assert config["row_filters"] == ["positive"]
        assert config["filter_sql"] == "amount < 1000"

    async def test_create_fact_single_with_structured_conditions(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "trial_revenue",
                "display_name": "Trial Revenue",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
                "measure_column": "amount",
                "conditions": [
                    {"column": "amount", "operator": "gt", "value": "3"},
                    {"column": "user_id", "operator": "is_not_null"},
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        config = resp.json()["config"]
        assert config["row_filters"] == []
        assert config["filter_sql"] is None
        assert config["conditions"] == [
            {"column": "amount", "operator": "gt", "value": "3"},
            {"column": "user_id", "operator": "is_not_null"},
        ]

    async def test_create_fact_ratio_cross_fact_table(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        # A second fact table for the denominator: the ratio may span two tables.
        other = await client.post(
            f"/api/v1/projects/{project['slug']}/fact-tables",
            json={
                "name": "sessions_ft",
                "display_name": "Sessions",
                "sql": "SELECT started_at, session_id FROM sessions",
                "timestamp_column": "started_at",
                "columns": [
                    {"name": "started_at", "type": "timestamp"},
                    {"name": "session_id", "type": "string"},
                ],
                "identifier_columns": ["session_id"],
                "row_filters": [],
            },
        )
        assert other.status_code == 201, other.text
        denominator_ft = other.json()

        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "orders_per_session",
                "display_name": "Orders / session",
                "composition": "ratio",
                "interval": "1h",
                "numerator": {"fact_table_id": fact_table["id"], "aggregation": "count"},
                "denominator": {
                    "fact_table_id": denominator_ft["id"],
                    "aggregation": "count_distinct",
                    "distinct_column": "session_id",
                },
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["composition"] == "ratio"
        # The numerator operand mirrors onto the catalog fact_table_id / aggregation.
        assert data["fact_table_id"] == fact_table["id"]
        assert data["aggregation"] == "count"
        assert data["config"]["numerator"]["fact_table_id"] == fact_table["id"]
        assert data["config"]["denominator"]["fact_table_id"] == denominator_ft["id"]
        assert data["config"]["denominator"]["distinct_column"] == "session_id"

    async def test_create_sql_metric(self, client: AsyncClient, project: dict, data_source: dict):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "dau",
                "display_name": "Daily Active Users",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {
                    "metric_sql": (
                        "SELECT toDate(t) AS bucket, count() AS value FROM e GROUP BY bucket"
                    ),
                    "time_column": "t",
                },
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["kind"] == "sql"
        assert data["config"]["metric_sql"].startswith("SELECT")
        assert data["data_source_id"] == data_source["id"]

    async def test_create_event_composition_single(
        self, client: AsyncClient, project: dict, event: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "signup_count",
                "display_name": "Signups",
                "composition": "single",
                "numerator_event_id": event["id"],
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["kind"] == "event_composition"
        assert data["composition"] == "single"
        assert data["numerator_event_id"] == event["id"]
        # Composition metrics read existing series: no data source / interval.
        assert data["data_source_id"] is None
        assert data["interval"] is None

    async def test_create_event_composition_ratio(
        self, client: AsyncClient, project: dict, event: dict, event_type: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "conversion",
                "display_name": "Conversion",
                "composition": "ratio",
                "numerator_event_id": event["id"],
                "denominator_event_type_id": event_type["id"],
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["composition"] == "ratio"
        assert data["numerator_event_id"] == event["id"]
        assert data["denominator_event_type_id"] == event_type["id"]

    async def test_create_sql_metric_custom_value_column(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "sessions",
                "display_name": "Sessions",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {
                    "metric_sql": (
                        "SELECT toDate(t) AS bucket, count() AS sessions FROM e GROUP BY bucket"
                    ),
                    "time_column": "t",
                    "value_column": "sessions",
                },
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["config"]["value_column"] == "sessions"

        bad = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "bad-value-col",
                "display_name": "Bad",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {
                    "metric_sql": "SELECT 1 AS value, now() AS t",
                    "time_column": "t",
                    "value_column": "not a column!!",
                },
            },
        )
        assert bad.status_code == 422

    async def test_create_per_distinct_user_with_user_id_column(
        self, client: AsyncClient, project: dict, event: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "events_per_device",
                "display_name": "Events per device",
                "composition": "per_distinct_user",
                "numerator_event_id": event["id"],
                "user_id_column": "device_id",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["config"] == {"user_id_column": "device_id"}

        # The override is meaningless outside per_distinct_user — reject it so
        # a stray value doesn't silently sit in config.
        bad = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "single_with_user_col",
                "display_name": "Single",
                "composition": "single",
                "numerator_event_id": event["id"],
                "user_id_column": "device_id",
            },
        )
        assert bad.status_code == 422


class TestListFilterPagination:
    async def _seed(self, client: AsyncClient, slug: str, data_source_id: str) -> None:
        for idx in range(3):
            resp = await client.post(
                _metrics_url(slug),
                json={
                    "kind": "sql",
                    "name": f"m{idx}",
                    "display_name": f"Metric {idx}",
                    "data_source_id": data_source_id,
                    "interval": "1d",
                    "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
                },
            )
            assert resp.status_code == 201, resp.text

    async def test_list_returns_items_and_total(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        await self._seed(client, project["slug"], data_source["id"])
        resp = await client.get(_metrics_url(project["slug"]))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 3

    async def test_list_filter_by_kind(
        self, client: AsyncClient, project: dict, data_source: dict, event: dict
    ):
        await self._seed(client, project["slug"], data_source["id"])
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "comp",
                "display_name": "Comp",
                "composition": "single",
                "numerator_event_id": event["id"],
            },
        )
        assert resp.status_code == 201
        resp = await client.get(_metrics_url(project["slug"]), params={"kind": "sql"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert all(item["kind"] == "sql" for item in body["items"])

    async def test_list_search_and_pagination(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        await self._seed(client, project["slug"], data_source["id"])
        resp = await client.get(_metrics_url(project["slug"]), params={"search": "m1"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        resp = await client.get(_metrics_url(project["slug"]), params={"offset": 0, "limit": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2


class TestNameUniqueness:
    async def test_duplicate_name_conflict(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        payload = {
            "kind": "sql",
            "name": "dup",
            "display_name": "Dup",
            "data_source_id": data_source["id"],
            "interval": "1d",
            "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
        }
        first = await client.post(_metrics_url(project["slug"]), json=payload)
        assert first.status_code == 201
        second = await client.post(_metrics_url(project["slug"]), json=payload)
        assert second.status_code == 409


class TestConfigValidation:
    async def test_fact_single_sum_without_measure_column(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "bad_sum",
                "display_name": "Bad Sum",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
            },
        )
        assert resp.status_code == 422

    async def test_fact_single_count_distinct_requires_distinct_column(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "bad_cd",
                "display_name": "Bad CD",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count_distinct",
                "interval": "1h",
            },
        )
        assert resp.status_code == 422

    async def test_fact_single_without_fact_table_id(self, client: AsyncClient, project: dict):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "no_ft",
                "display_name": "No Fact Table",
                "composition": "single",
                "aggregation": "count",
                "interval": "1h",
            },
        )
        assert resp.status_code == 422

    async def test_fact_measure_column_not_in_fact_table_rejected(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        # A syntactically valid identifier that is NOT one of the fact table's
        # introspected columns is rejected by the service-level allowlist check.
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "ghost_col",
                "display_name": "Ghost Col",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
                "measure_column": "not_a_column",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_fact_unknown_row_filter_rejected(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "ghost_filter",
                "display_name": "Ghost Filter",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "row_filter": "does_not_exist",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_fact_unknown_row_filters_entry_rejected(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        # One known name + one unknown name in ``row_filters`` -> 422 (same as the
        # legacy single ``row_filter`` unknown-name rejection).
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "ghost_in_list",
                "display_name": "Ghost in list",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "row_filters": ["exclude_test", "does_not_exist"],
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_fact_unknown_condition_column_rejected(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "ghost_condition_column",
                "display_name": "Ghost condition column",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "conditions": [{"column": "missing_col", "operator": "eq", "value": "x"}],
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_fact_condition_value_type_rejected_before_collection(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "invalid_numeric_condition",
                "display_name": "Invalid numeric condition",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "conditions": [{"column": "amount", "operator": "gt", "value": "many"}],
            },
        )
        assert resp.status_code == 422, resp.text
        assert "numeric value" in resp.json()["detail"]

    async def test_fact_condition_operator_must_match_column_type(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "invalid_numeric_operator",
                "display_name": "Invalid numeric operator",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "conditions": [{"column": "amount", "operator": "contains", "value": "1"}],
            },
        )
        assert resp.status_code == 422, resp.text
        assert "operator 'contains'" in resp.json()["detail"]

    async def test_fact_boolean_condition_rejects_non_boolean_value(
        self, client: AsyncClient, project: dict
    ):
        fact_response = await client.post(
            f"/api/v1/projects/{project['slug']}/fact-tables",
            json={
                "name": "boolean_condition_ft",
                "display_name": "Boolean conditions",
                "sql": "SELECT created_at, is_active FROM subscriptions",
                "timestamp_column": "created_at",
                "columns": [
                    {"name": "created_at", "type": "timestamp"},
                    {"name": "is_active", "type": "bool"},
                ],
            },
        )
        assert fact_response.status_code == 201, fact_response.text

        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "invalid_boolean_condition",
                "display_name": "Invalid boolean condition",
                "composition": "single",
                "fact_table_id": fact_response.json()["id"],
                "aggregation": "count",
                "interval": "1h",
                "conditions": [{"column": "is_active", "operator": "eq", "value": "yes"}],
            },
        )
        assert resp.status_code == 422, resp.text
        assert "true or false" in resp.json()["detail"]

    async def test_fact_nonexistent_fact_table_rejected(self, client: AsyncClient, project: dict):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "ghost_ft",
                "display_name": "Ghost FT",
                "composition": "single",
                "fact_table_id": str(uuid.uuid4()),
                "aggregation": "count",
                "interval": "1h",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_fact_ratio_cross_table_breakdowns_rejected(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        other = await client.post(
            f"/api/v1/projects/{project['slug']}/fact-tables",
            json={
                "name": "sessions_ratio_guard_ft",
                "display_name": "Sessions",
                "sql": "SELECT started_at, session_id, country FROM sessions",
                "timestamp_column": "started_at",
                "columns": [
                    {"name": "started_at", "type": "timestamp"},
                    {"name": "session_id", "type": "string"},
                    {"name": "country", "type": "string"},
                ],
                "identifier_columns": ["session_id"],
                "row_filters": [],
            },
        )
        assert other.status_code == 201, other.text
        denominator_ft = other.json()

        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "orders_per_session_by_country",
                "display_name": "Orders / session by country",
                "composition": "ratio",
                "interval": "1h",
                "breakdown_columns": ["country"],
                "numerator": {"fact_table_id": fact_table["id"], "aggregation": "count"},
                "denominator": {
                    "fact_table_id": denominator_ft["id"],
                    "aggregation": "count_distinct",
                    "distinct_column": "session_id",
                },
            },
        )

        assert resp.status_code == 422, resp.text
        assert "same fact table" in resp.json()["detail"]

    async def test_sql_without_metric_sql(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "no_sql",
                "display_name": "No SQL",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {"time_column": "t"},
            },
        )
        assert resp.status_code == 422

    async def test_sql_without_data_source(self, client: AsyncClient, project: dict):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "no_ds",
                "display_name": "No DS",
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        )
        assert resp.status_code == 422

    async def test_event_composition_ratio_without_denominator(
        self, client: AsyncClient, project: dict, event: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "ratio_no_denom",
                "display_name": "Ratio No Denominator",
                "composition": "ratio",
                "numerator_event_id": event["id"],
            },
        )
        assert resp.status_code == 422

    async def test_event_composition_with_nonexistent_event(
        self, client: AsyncClient, project: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "ghost_ref",
                "display_name": "Ghost Ref",
                "composition": "single",
                "numerator_event_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 422


class TestUpdateDeleteReorder:
    async def _create_sql(
        self, client: AsyncClient, slug: str, data_source_id: str, name: str
    ) -> dict:
        resp = await client.post(
            _metrics_url(slug),
            json={
                "kind": "sql",
                "name": name,
                "display_name": name.upper(),
                "data_source_id": data_source_id,
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def test_update_metric(self, client: AsyncClient, project: dict, data_source: dict):
        metric = await self._create_sql(client, project["slug"], data_source["id"], "upd")
        resp = await client.patch(
            f"{_metrics_url(project['slug'])}/{metric['id']}",
            json={"display_name": "Updated", "status": "active", "reviewed": True},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["display_name"] == "Updated"
        assert data["status"] == "active"
        assert data["reviewed"] is True

    async def test_delete_metric(self, client: AsyncClient, project: dict, data_source: dict):
        metric = await self._create_sql(client, project["slug"], data_source["id"], "del")
        resp = await client.delete(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert resp.status_code == 204
        resp = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert resp.status_code == 404

    async def test_bulk_update_status(self, client: AsyncClient, project: dict, data_source: dict):
        m1 = await self._create_sql(client, project["slug"], data_source["id"], "b1")
        m2 = await self._create_sql(client, project["slug"], data_source["id"], "b2")
        resp = await client.post(
            f"{_metrics_url(project['slug'])}/bulk-update",
            json={"metric_ids": [m1["id"], m2["id"]], "status": "archived"},
        )
        assert resp.status_code == 204
        listing = await client.get(_metrics_url(project["slug"]), params={"status": "archived"})
        assert listing.json()["total"] == 2

    async def test_reorder_metrics(self, client: AsyncClient, project: dict, data_source: dict):
        m1 = await self._create_sql(client, project["slug"], data_source["id"], "r1")
        m2 = await self._create_sql(client, project["slug"], data_source["id"], "r2")
        resp = await client.patch(
            f"{_metrics_url(project['slug'])}/reorder",
            json={"metric_ids": [m2["id"], m1["id"]]},
        )
        assert resp.status_code == 200, resp.text
        ordered = resp.json()
        assert [item["id"] for item in ordered] == [m2["id"], m1["id"]]
        assert ordered[0]["order"] <= ordered[1]["order"]


class TestSchemaSecurityValidation:
    """The schema boundary is the only gate before warehouse SQL with no bound
    params, so every identifier / SQL-text field must reject injection probes."""

    async def test_reject_row_filter_raw_sql_fragment(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        # ``row_filter`` is the NAME of a stored fact-table filter, never a raw
        # SQL fragment: a fragment is not a known name and is rejected.
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "inj_filter_name",
                "display_name": "Inj Filter Name",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "row_filter": "1=1 UNION SELECT secret FROM users --",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_filter_sql_injection(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        # ``filter_sql`` is free-text but SQL-safety-validated at the schema
        # boundary (same guard as a fact table's named row filter): a stacked
        # statement / UNION / comment probe is rejected.
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "inj_filter_sql",
                "display_name": "Inj Filter SQL",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "filter_sql": "1=1; DROP TABLE users --",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_condition_column_injection(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "inj_condition_column",
                "display_name": "Inj Condition Column",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "conditions": [
                    {"column": "amount; DROP TABLE users", "operator": "eq", "value": "1"}
                ],
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_empty_filter_sql(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "empty_filter_sql",
                "display_name": "Empty Filter SQL",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "filter_sql": "   ",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_measure_column_with_quote(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "inj_measure",
                "display_name": "Inj Measure",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
                "measure_column": "am'ount",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_breakdown_column_bad_identifier(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "inj_breakdown",
                "display_name": "Inj Breakdown",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
                "breakdown_columns": ["good_col", "bad-col"],
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_app_version_column_bad(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "inj_appver",
                "display_name": "Inj AppVer",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
                "app_version_column": "app version",
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_reject_metric_sql_with_drop_and_comment(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "inj_sql",
                "display_name": "Inj SQL",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {
                    "metric_sql": "SELECT 1 AS v FROM x; DROP TABLE y -- gone",
                    "time_column": "t",
                },
            },
        )
        assert resp.status_code == 422, resp.text


class TestBulkUpdateOwner:
    async def test_owner_id_null_unassigns_owner(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        owner_id = (await client.get("/api/v1/users")).json()[0]["id"]
        metric = await _create_sql_metric(client, project["slug"], data_source["id"], "owned")

        assign = await client.post(
            f"{_metrics_url(project['slug'])}/bulk-update",
            json={"metric_ids": [metric["id"]], "owner_id": owner_id},
        )
        assert assign.status_code == 204
        got = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert got.json()["owner_id"] == owner_id

        unassign = await client.post(
            f"{_metrics_url(project['slug'])}/bulk-update",
            json={"metric_ids": [metric["id"]], "owner_id": None},
        )
        assert unassign.status_code == 204
        got = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert got.json()["owner_id"] is None

    async def test_bulk_update_without_any_field_is_422(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        metric = await _create_sql_metric(client, project["slug"], data_source["id"], "nofield")
        resp = await client.post(
            f"{_metrics_url(project['slug'])}/bulk-update",
            json={"metric_ids": [metric["id"]]},
        )
        assert resp.status_code == 422, resp.text


class TestListEnumValidation:
    async def test_invalid_kind_returns_422(self, client: AsyncClient, project: dict):
        resp = await client.get(_metrics_url(project["slug"]), params={"kind": "bogus"})
        assert resp.status_code == 422, resp.text

    async def test_invalid_status_returns_422(self, client: AsyncClient, project: dict):
        resp = await client.get(_metrics_url(project["slug"]), params={"status": "bogus"})
        assert resp.status_code == 422, resp.text


class TestMoveMetric:
    async def test_move_up_swaps_with_previous(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        m0 = await _create_sql_metric(client, slug, data_source["id"], "mv_up0", order=0)
        m1 = await _create_sql_metric(client, slug, data_source["id"], "mv_up1", order=1)

        resp = await client.patch(f"{_metrics_url(slug)}/{m1['id']}/move", json={"direction": "up"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["order"] == 0

        other = await client.get(f"{_metrics_url(slug)}/{m0['id']}")
        assert other.json()["order"] == 1

    async def test_move_down_swaps_with_next(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        m0 = await _create_sql_metric(client, slug, data_source["id"], "mv_dn0", order=0)
        m1 = await _create_sql_metric(client, slug, data_source["id"], "mv_dn1", order=1)

        resp = await client.patch(
            f"{_metrics_url(slug)}/{m0['id']}/move", json={"direction": "down"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["order"] == 1

        other = await client.get(f"{_metrics_url(slug)}/{m1['id']}")
        assert other.json()["order"] == 0

    async def test_move_up_when_first_is_noop(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        m0 = await _create_sql_metric(client, slug, data_source["id"], "mv_first0", order=0)
        await _create_sql_metric(client, slug, data_source["id"], "mv_first1", order=1)

        resp = await client.patch(f"{_metrics_url(slug)}/{m0['id']}/move", json={"direction": "up"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["order"] == 0


class TestCrossProjectIsolation:
    async def test_metric_from_other_project_returns_404(
        self, client: AsyncClient, data_source: dict
    ):
        proj_a = await client.post(
            "/api/v1/projects",
            json={"name": "Proj A", "slug": "proj-a", "description": ""},
        )
        proj_b = await client.post(
            "/api/v1/projects",
            json={"name": "Proj B", "slug": "proj-b", "description": ""},
        )
        assert proj_a.status_code == 201
        assert proj_b.status_code == 201

        # Project B needs its own binding to the shared source: the `data_source`
        # fixture binds it to the `project` fixture's project only, and the save
        # path below refuses a source the saving project does not reach through a
        # ScanConfig. This is setup for the save, not the subject of the test —
        # the 404 asserted below is the metric read's own project scope, which
        # does not consult the data source at all.
        async with TestSessionLocal() as session:
            session.add(
                ScanConfig(
                    project_id=uuid.UUID(proj_b.json()["id"]),
                    data_source_id=uuid.UUID(data_source["id"]),
                    name="proj-b-ds-binding",
                    base_query="SELECT 1",
                )
            )
            await session.commit()

        metric_b = await _create_sql_metric(client, "proj-b", data_source["id"], "b_metric")

        # A valid metric_id from project B, fetched under project A's slug, must 404.
        resp = await client.get(f"/api/v1/projects/proj-a/metrics/{metric_b['id']}")
        assert resp.status_code == 404, resp.text


class _FakeAsyncResult:
    """Stand-in for a Celery AsyncResult so the endpoint can read ``.id``."""

    def __init__(self, task_id: str) -> None:
        self.id = task_id


class _DispatchRecorder:
    """Records the positional args of a patched task ``.delay`` call."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.calls: list[tuple] = []

    def __call__(self, *args: object) -> _FakeAsyncResult:
        self.calls.append(args)
        return _FakeAsyncResult(self.task_id)


@pytest.fixture
def dispatch_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, _DispatchRecorder]:
    """Patch both collection tasks' ``.delay`` so no broker is needed in tests.

    Returns the recorders so a test can assert which task was dispatched and with
    which metric id / window.
    """
    sql_rec = _DispatchRecorder("task-sql")
    fact_rec = _DispatchRecorder("task-fact")
    monkeypatch.setattr(mc.collect_metric_definitions, "delay", sql_rec)
    monkeypatch.setattr(mc.collect_fact_metrics_batch, "delay", fact_rec)
    return {"sql": sql_rec, "fact": fact_rec}


class TestCollectNow:
    async def test_collect_returns_conflict_when_scheduler_holds_dispatch_lock(
        self,
        client: AsyncClient,
        project: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def lock_busy(_session: object) -> bool:
            return False

        monkeypatch.setattr(
            metric_definition_service,
            "_try_acquire_metric_dispatch_transaction_lock",
            lock_busy,
        )

        response = await client.post(f"{_metrics_url(project['slug'])}/{uuid.uuid4()}/collect")

        # The non-blocking lock check precedes even the metric lookup, so a
        # scheduler tick can never make this API request hang behind its lock.
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Metric collection dispatcher is busy"
        assert dispatch_recorder["sql"].calls == []
        assert dispatch_recorder["fact"].calls == []

    async def test_sql_metric_dispatches_with_backfill_window(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
    ):
        metric = await _create_sql_metric(client, project["slug"], data_source["id"], "cn_sql")
        # A freshly-created metric is draft; collect-now must still dispatch
        # (and force collection so the worker does not skip on status).
        assert metric["status"] == "draft"
        resp = await client.post(f"{_metrics_url(project['slug'])}/{metric['id']}/collect")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["metric_id"] == metric["id"]
        assert body["status"] == "queued"
        assert body["task_id"] == "task-sql"
        assert body["window_from"] is not None
        assert body["window_to"] is not None

        # 1d interval -> 30 buckets, capped at the 30-day ceiling.
        window_from = datetime.fromisoformat(body["window_from"])
        window_to = datetime.fromisoformat(body["window_to"])
        assert window_to - window_from == timedelta(days=30)

        # The single-metric collector is dispatched with (metric_id, from, to);
        # the fact batch task is NOT used for a sql metric. Compare parsed
        # datetimes, not strings: the ISO spelling may differ ("+00:00" vs "Z").
        #
        # The DISPATCHED window and the REPORTED one coincide here, and that is a
        # property of this fixture rather than a rule. The response reports
        # `effective_manual_window`, which widens the bounded manual window to the
        # resume fallback when the fallback reaches further back; on a FRESH 1d
        # metric the two rules land on the same instants (the helper's own
        # docstring says so for 1d and 1h, "which is why nothing caught this").
        # They are not equal in general: a 1w metric reports 210 days and
        # dispatches 28, pinned in test_batch5_facttables as
        # test_collect_now_reports_the_widened_window_but_dispatches_the_bounded_one.
        sql_calls = dispatch_recorder["sql"].calls
        assert len(sql_calls) == 1
        assert sql_calls[0][0] == metric["id"]
        assert datetime.fromisoformat(sql_calls[0][1]) == window_from
        assert datetime.fromisoformat(sql_calls[0][2]) == window_to
        # force=True is always passed by the manual trigger so a draft metric is
        # not skipped on status by the worker.
        assert sql_calls[0][3] is True
        # manual_backfill=True: the worker widens its resume window to reach this
        # one rather than replacing it, so a lagging metric keeps its backlog.
        assert sql_calls[0][4] is True
        assert dispatch_recorder["fact"].calls == []

    async def test_fact_metric_dispatches_batch_with_window(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
    ):
        created = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "cn_fact",
                "display_name": "CN Fact",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "interval": "1h",
                "measure_column": "amount",
            },
        )
        assert created.status_code == 201, created.text
        metric = created.json()
        assert metric["status"] == "draft"

        resp = await client.post(f"{_metrics_url(project['slug'])}/{metric['id']}/collect")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["task_id"] == "task-fact"

        # 1h interval -> 48 buckets = 48h (well under the 30-day cap).
        window_from = datetime.fromisoformat(body["window_from"])
        window_to = datetime.fromisoformat(body["window_to"])
        assert window_to - window_from == timedelta(hours=48)

        # Fact metrics reuse the shared-scan batch task with a one-element list.
        # Same caveat as the sql case above: the dispatched and reported windows
        # coincide because this is a fresh 1h metric, not because they must.
        fact_calls = dispatch_recorder["fact"].calls
        assert len(fact_calls) == 1
        assert fact_calls[0][0] == [metric["id"]]
        assert datetime.fromisoformat(fact_calls[0][1]) == window_from
        assert datetime.fromisoformat(fact_calls[0][2]) == window_to
        # force=True is always passed so a draft fact metric stays in the batch.
        assert fact_calls[0][3] is True
        assert dispatch_recorder["sql"].calls == []

    async def test_fact_metric_collect_dispatches_all_active_dependents(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
    ):
        other_resp = await client.post(
            f"/api/v1/projects/{project['slug']}/fact-tables",
            json={
                "name": "sessions_collect_ft",
                "display_name": "Sessions",
                "sql": "SELECT created_at, amount, user_id FROM sessions",
                "timestamp_column": "created_at",
                "columns": [
                    {"name": "created_at", "type": "timestamp"},
                    {"name": "amount", "type": "number"},
                    {"name": "user_id", "type": "string"},
                ],
                "identifier_columns": ["user_id"],
            },
        )
        assert other_resp.status_code == 201, other_resp.text
        other_fact_table = other_resp.json()

        async def create_metric(payload: dict[str, object]) -> dict:
            response = await client.post(_metrics_url(project["slug"]), json=payload)
            assert response.status_code == 201, response.text
            return response.json()

        clicked = await create_metric(
            {
                "kind": "fact",
                "name": "collect_clicked_draft",
                "display_name": "Clicked draft",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
            }
        )
        active_single = await create_metric(
            {
                "kind": "fact",
                "name": "collect_active_single",
                "display_name": "Active single",
                "status": "active",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "sum",
                "measure_column": "amount",
                "interval": "1h",
            }
        )
        active_ratio = await create_metric(
            {
                "kind": "fact",
                "name": "collect_active_ratio_denominator",
                "display_name": "Active ratio denominator",
                "status": "active",
                "composition": "ratio",
                "interval": "1h",
                "numerator": {
                    "fact_table_id": other_fact_table["id"],
                    "aggregation": "count",
                },
                "denominator": {
                    "fact_table_id": fact_table["id"],
                    "aggregation": "count",
                },
            }
        )
        transitive_active = await create_metric(
            {
                "kind": "fact",
                "name": "collect_transitive_active",
                "display_name": "Transitive active",
                "status": "active",
                "composition": "single",
                "fact_table_id": other_fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
            }
        )
        inactive_sibling = await create_metric(
            {
                "kind": "fact",
                "name": "collect_inactive_sibling",
                "display_name": "Inactive sibling",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
            }
        )

        response = await client.post(f"{_metrics_url(project['slug'])}/{clicked['id']}/collect")
        assert response.status_code == 202, response.text
        fact_calls = dispatch_recorder["fact"].calls
        assert len(fact_calls) == 1
        dispatched_ids = set(fact_calls[0][0])
        assert dispatched_ids == {
            clicked["id"],
            active_single["id"],
            active_ratio["id"],
            transitive_active["id"],
        }
        assert inactive_sibling["id"] not in dispatched_ids
        # The batch computes a bounded manual window per interval group, so a
        # cross-interval dependent never inherits the clicked metric's grid.
        assert fact_calls[0][4] is True
        # The clicked metric is the shared batch completion marker watched by UI.
        assert fact_calls[0][5] == clicked["id"]

        for metric_id in dispatched_ids:
            got = await client.get(f"{_metrics_url(project['slug'])}/{metric_id}")
            assert got.json()["last_collection_status"] == "running"

    async def test_fact_metric_collect_is_capped_and_reports_its_batch_size(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
        monkeypatch: pytest.MonkeyPatch,
    ):
        """One click must not hand a whole project to a single Celery task.

        The fact-table dependency closure is transitive, so a chain of ratios can
        walk from the clicked metric across most of a project. Metrics past the
        cap keep their own watermark and stay on the scheduler.
        """
        monkeypatch.setattr(metric_definition_service, "MAX_MANUAL_COLLECT_GROUP", 2)

        async def create_metric(name: str) -> dict:
            response = await client.post(
                _metrics_url(project["slug"]),
                json={
                    "kind": "fact",
                    "name": name,
                    "display_name": name,
                    "status": "active",
                    "composition": "single",
                    "fact_table_id": fact_table["id"],
                    "aggregation": "count",
                    "interval": "1h",
                },
            )
            assert response.status_code == 201, response.text
            return response.json()

        clicked = await create_metric("collect_capped_clicked")
        for index in range(3):
            await create_metric(f"collect_capped_sibling_{index}")

        response = await client.post(f"{_metrics_url(project['slug'])}/{clicked['id']}/collect")

        assert response.status_code == 202, response.text
        dispatched_ids = dispatch_recorder["fact"].calls[0][0]
        assert len(dispatched_ids) == 2
        assert clicked["id"] in dispatched_ids
        # ... and the click reports what it actually set in motion.
        assert response.json()["metric_count"] == 2

    async def test_collect_marks_metric_running(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
    ):
        metric = await _create_sql_metric(client, project["slug"], data_source["id"], "cn_run")
        resp = await client.post(f"{_metrics_url(project['slug'])}/{metric['id']}/collect")
        assert resp.status_code == 202, resp.text

        # Stamped running before dispatch so the scheduler won't double-dispatch.
        got = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert got.json()["last_collection_status"] == "running"

    async def test_fact_collect_rejects_when_any_dependent_is_already_running(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
    ):
        async def create_metric(name: str, *, status: str = "draft") -> dict:
            response = await client.post(
                _metrics_url(project["slug"]),
                json={
                    "kind": "fact",
                    "name": name,
                    "display_name": name,
                    "status": status,
                    "composition": "single",
                    "fact_table_id": fact_table["id"],
                    "aggregation": "count",
                    "interval": "1h",
                },
            )
            assert response.status_code == 201, response.text
            return response.json()

        clicked = await create_metric("collect_conflict_clicked")
        sibling = await create_metric("collect_conflict_running", status="active")
        async with TestSessionLocal() as session:
            running = await session.get(MetricDefinition, uuid.UUID(sibling["id"]))
            assert running is not None
            running.last_collection_status = "running"
            await session.commit()

        response = await client.post(f"{_metrics_url(project['slug'])}/{clicked['id']}/collect")

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "Metric collection is already running"
        assert dispatch_recorder["fact"].calls == []
        async with TestSessionLocal() as session:
            untouched = await session.get(MetricDefinition, uuid.UUID(clicked["id"]))
            assert untouched is not None
            assert untouched.last_collection_status is None

    async def test_dispatch_failure_returns_503_and_stamps_error(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        metric = await _create_sql_metric(client, project["slug"], data_source["id"], "cn_fail")

        def _broker_down(*args: object) -> None:
            raise RuntimeError("broker unavailable")

        monkeypatch.setattr(mc.collect_metric_definitions, "delay", _broker_down)
        resp = await client.post(f"{_metrics_url(project['slug'])}/{metric['id']}/collect")
        assert resp.status_code == 503, resp.text

        # The failed dispatch is persisted as a terminal error (not a stuck
        # "running") so the UI's status poll and the scheduler's one-active-job
        # guard both see the truth (tripl-4mju).
        got = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        body = got.json()
        assert body["last_collection_status"] == "error"
        assert body["last_collection_error"] == "Failed to dispatch collection task to worker"

    async def test_unknown_metric_returns_404(
        self,
        client: AsyncClient,
        project: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
    ):
        resp = await client.post(f"{_metrics_url(project['slug'])}/{uuid.uuid4()}/collect")
        assert resp.status_code == 404, resp.text
        assert dispatch_recorder["sql"].calls == []
        assert dispatch_recorder["fact"].calls == []

    async def test_requires_editor_role(
        self,
        client: AsyncClient,
        project: dict,
        dispatch_recorder: dict[str, _DispatchRecorder],
    ):
        # A persisted viewer MEMBER: a non-member would get 404, not 403.
        viewer = await persisted_member_user(uuid.UUID(project["id"]), role="viewer")

        async def _viewer() -> User:
            return viewer

        app.dependency_overrides[get_current_user] = _viewer
        try:
            resp = await client.post(f"{_metrics_url(project['slug'])}/{uuid.uuid4()}/collect")
        finally:
            app.dependency_overrides.pop(get_current_user, None)

        assert resp.status_code == 403, resp.text
        assert dispatch_recorder["sql"].calls == []
        assert dispatch_recorder["fact"].calls == []


class TestMetricCollectionSchedule:
    async def test_detail_exposes_due_and_next_scheduled_collection(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
    ):
        metric = await _create_sql_metric(
            client,
            project["slug"],
            data_source["id"],
            "schedule_detail",
            status="active",
        )

        due = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert due.status_code == 200, due.text
        assert due.json()["collection_due"] is True
        assert due.json()["next_collection_at"] is not None

        now = datetime.now(UTC)
        current_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        latest_complete = current_day - timedelta(days=1)
        async with TestSessionLocal() as session:
            session.add(
                MetricValue(
                    metric_definition_id=uuid.UUID(metric["id"]),
                    bucket=latest_complete,
                    value=1.0,
                )
            )
            await session.commit()

        scheduled = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert scheduled.status_code == 200, scheduled.text
        body = scheduled.json()
        assert body["collection_due"] is False
        assert datetime.fromisoformat(body["next_collection_at"]) == current_day + timedelta(days=1)

    async def test_a_cooling_metric_is_not_reported_as_due_now(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
    ):
        """The drilldown and the dispatcher must answer one question one way.

        A metric in the error state sits out a cooldown before it is dispatched
        again. This endpoint used to compute due-ness from the watermark alone,
        so it told the operator a collection was due NOW while the scheduler
        would skip it for up to a full interval — and a "due now" that does not
        happen is the report that sends somebody digging through worker logs
        (tripl-os3v).
        """
        from tripl.models.metric_definition import MetricDefinition
        from tripl.worker.tasks.metrics.metric_collect import COLLECTION_STATUS_ERROR
        from tripl.worker.tasks.metrics.schedule import metric_definition_cooldown_until

        metric = await _create_sql_metric(
            client,
            project["slug"],
            data_source["id"],
            "schedule_cooldown",
            status="active",
        )

        # Fresh and never collected, so the watermark alone says "due now".
        before = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert before.status_code == 200, before.text
        assert before.json()["collection_due"] is True

        failed_at = datetime.now(UTC) - timedelta(minutes=1)
        async with TestSessionLocal() as session:
            definition = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
            assert definition is not None
            definition.last_collection_status = COLLECTION_STATUS_ERROR
            definition.last_collection_error = "boom"
            definition.last_collection_failed_at = failed_at
            await session.commit()

        after = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")
        assert after.status_code == 200, after.text
        body = after.json()
        assert body["collection_due"] is False, "a cooling metric is not due"

        # And the moment it reports is the scheduler's own, not a second guess.
        async with TestSessionLocal() as session:
            definition = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
            assert definition is not None
            expected = metric_definition_cooldown_until(definition, now=datetime.now(UTC))
        assert expected is not None
        reported = datetime.fromisoformat(body["next_collection_at"])
        assert abs(reported - expected) < timedelta(seconds=5)

    async def test_zero_row_collection_watermark_advances_schedule(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
    ):
        metric = await _create_sql_metric(
            client,
            project["slug"],
            data_source["id"],
            "schedule_empty_success",
            status="active",
        )
        now = datetime.now(UTC)
        current_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with TestSessionLocal() as session:
            row = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
            assert row is not None
            row.last_collection_window_to = current_day
            row.last_collection_status = "success"
            await session.commit()

        scheduled = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}")

        assert scheduled.status_code == 200, scheduled.text
        body = scheduled.json()
        assert body["collection_due"] is False
        assert datetime.fromisoformat(body["next_collection_at"]) == current_day + timedelta(days=1)


class TestGeneratedFactSql:
    async def test_returns_shared_primary_batch_sql_without_executing_warehouse(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
    ):
        created = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "generated_source_sql",
                "display_name": "Generated source SQL",
                "composition": "single",
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "sum",
                "measure_column": "amount",
                "interval": "1d",
                "row_filters": ["exclude_test"],
                "conditions": [
                    {"column": "amount", "operator": "gt", "value": "1"},
                    {"column": "amount", "operator": "lt", "value": 10},
                ],
            },
        )
        assert created.status_code == 201, created.text
        metric = created.json()

        sibling_response = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "generated_source_count",
                "display_name": "Generated source count",
                "composition": "single",
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "count",
                "interval": "1d",
                "status": "active",
                "conditions": [{"column": "is_test", "operator": "eq", "value": 0}],
            },
        )
        assert sibling_response.status_code == 201, sibling_response.text
        sibling = sibling_response.json()

        response = await client.get(f"{_metrics_url(project['slug'])}/{metric['id']}/generated-sql")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["breakdown_queries_omitted"] is True
        assert len(body["queries"]) == 1
        query = body["queries"][0]
        assert query["role"] == "primary"
        assert query["label"] == "Bound Orders · 1d"
        assert query["fact_table_id"] == bound_fact_table["id"]
        assert query["fact_table_name"] == "Bound Orders"
        assert query["interval"] == "1d"
        assert set(query["metric_ids"]) == {metric["id"], sibling["id"]}
        assert datetime.fromisoformat(query["window_from"]) < datetime.fromisoformat(
            query["window_to"]
        )
        sql = query["sql"]
        assert sql.count("FROM (SELECT created_at, amount, user_id, is_test FROM orders)") == 1
        assert "sumIf(`amount`, (is_test = 0) AND (`amount` > 1) AND (`amount` < 10))" in sql
        assert "countIf((`is_test` = 0))" in sql
        assert "GROUP BY _bucket ORDER BY _bucket" in sql

    async def test_rejects_oversized_legacy_operand_row_filters_before_compilation(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
    ):
        created = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "generated_legacy_filters",
                "display_name": "Generated legacy filters",
                "composition": "single",
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "count",
                "interval": "1d",
                "row_filters": ["exclude_test"],
            },
        )
        assert created.status_code == 201, created.text
        metric_id = created.json()["id"]

        # Simulate a row saved before request-schema collection limits existed.
        # Repeating one valid name must be rejected before it is expanded into
        # an attacker-amplified ``AND`` expression.
        async with TestSessionLocal() as session:
            row = await session.get(MetricDefinition, uuid.UUID(metric_id))
            assert row is not None
            row.config = {
                **dict(row.config or {}),
                "row_filters": ["exclude_test"] * 101,
            }
            await session.commit()

        response = await client.get(f"{_metrics_url(project['slug'])}/{metric_id}/generated-sql")

        assert response.status_code == 422, response.text
        assert "filters exceed the 100-item preview limit" in response.json()["detail"]

    async def test_caps_repeated_metric_id_metadata(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        first = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "generated_metadata_first",
                "display_name": "Generated metadata first",
                "composition": "single",
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "count",
                "interval": "1d",
            },
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "generated_metadata_second",
                "display_name": "Generated metadata second",
                "composition": "single",
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "count",
                "interval": "1d",
                "status": "active",
            },
        )
        assert second.status_code == 201, second.text
        monkeypatch.setattr(
            "tripl.services.metric_preview_service.MAX_GENERATED_METRIC_ID_REFERENCES",
            1,
        )

        response = await client.get(
            f"{_metrics_url(project['slug'])}/{first.json()['id']}/generated-sql"
        )

        assert response.status_code == 422, response.text
        assert "metric-reference preview limit" in response.json()["detail"]

    async def test_ratio_operands_share_one_primary_query(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
    ):
        created = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "generated_ratio_sql",
                "display_name": "Generated ratio SQL",
                "composition": "ratio",
                "interval": "1h",
                "numerator": {
                    "fact_table_id": bound_fact_table["id"],
                    "aggregation": "sum",
                    "measure_column": "amount",
                    "conditions": [{"column": "amount", "operator": "gt", "value": 0}],
                },
                "denominator": {
                    "fact_table_id": bound_fact_table["id"],
                    "aggregation": "count",
                    "conditions": [{"column": "is_test", "operator": "eq", "value": 0}],
                },
            },
        )
        assert created.status_code == 201, created.text

        response = await client.get(
            f"{_metrics_url(project['slug'])}/{created.json()['id']}/generated-sql"
        )
        assert response.status_code == 200, response.text
        queries = response.json()["queries"]
        assert len(queries) == 1
        query = queries[0]
        assert query["role"] == "primary"
        assert query["fact_table_id"] == bound_fact_table["id"]
        assert query["metric_ids"] == [created.json()["id"]]
        assert "sumIf(`amount`, (`amount` > 0))" in query["sql"]
        assert "countIf((`is_test` = 0))" in query["sql"]


async def _seed_collected_data(metric_id: str) -> None:
    """Insert a value + breakdown + metric-scope anomaly + collection stamp.

    Lets the kind-change tests assert that everything the metric owns is cleared
    (and presentation-only edits assert that identical definitions keep history).
    """
    mid = uuid.UUID(metric_id)
    bucket = datetime(2026, 1, 1, tzinfo=UTC)
    async with TestSessionLocal() as session:
        session.add(MetricValue(metric_definition_id=mid, bucket=bucket, value=1.0))
        session.add(
            MetricValueBreakdown(
                metric_definition_id=mid,
                bucket=bucket,
                breakdown_column="country",
                breakdown_value="US",
                value=1.0,
            )
        )
        session.add(
            MetricAnomaly(
                scope_type=MetricScopeType.metric.value,
                scope_ref=metric_id,
                bucket=bucket,
                actual_count=10,
                expected_count=2.0,
                stddev=1.0,
                z_score=8.0,
                direction=AnomalyDirection.spike.value,
            )
        )
        await session.commit()


async def _count_collected_data(metric_id: str) -> tuple[int, int, int]:
    """(values, breakdowns, metric-scope anomalies) currently stored for a metric."""
    mid = uuid.UUID(metric_id)
    async with TestSessionLocal() as session:
        values = await session.scalar(
            select(func.count(MetricValue.id)).where(MetricValue.metric_definition_id == mid)
        )
        breakdowns = await session.scalar(
            select(func.count(MetricValueBreakdown.id)).where(
                MetricValueBreakdown.metric_definition_id == mid
            )
        )
        anomalies = await session.scalar(
            select(func.count(MetricAnomaly.id)).where(
                MetricAnomaly.scope_type == MetricScopeType.metric.value,
                MetricAnomaly.scope_ref == metric_id,
            )
        )
    return int(values or 0), int(breakdowns or 0), int(anomalies or 0)


class TestUpdateDefinition:
    """PATCH /metrics/{id} can re-define a metric's kind + collection config."""

    async def test_edit_sql_config_data_source_interval_and_sql(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "edit_sql")
        # A second data source the metric can be repointed at. It needs a scan
        # config in THIS project, because a metric may only bind a data source
        # its own project already uses — otherwise an editor on one project
        # could point warehouse SQL at another project's credential, and the
        # beat would then run it unattended. Binding it here keeps this test
        # about re-pointing a metric; the refusal has its own tests.
        other_ds = (
            await client.post(
                "/api/v1/data-sources",
                json={
                    "name": "Other CH",
                    "db_type": "clickhouse",
                    "host": "localhost",
                    "port": 8123,
                    "database_name": "other_db",
                },
            )
        ).json()
        async with TestSessionLocal() as session:
            session.add(
                ScanConfig(
                    project_id=uuid.UUID(project["id"]),
                    data_source_id=uuid.UUID(other_ds["id"]),
                    name="metrics-other-ds-binding",
                    base_query="SELECT 1",
                )
            )
            await session.commit()

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "definition": {
                    "kind": "sql",
                    "data_source_id": other_ds["id"],
                    "interval": "6h",
                    "config": {
                        "metric_sql": "SELECT toStartOfHour(t) AS bucket, count() AS value FROM e",
                        "time_column": "bucket",
                    },
                }
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["kind"] == "sql"
        assert data["data_source_id"] == other_ds["id"]
        assert data["interval"] == "6h"
        assert data["config"]["metric_sql"].endswith("FROM e")
        assert data["config"]["time_column"] == "bucket"

    async def test_edit_fact_operand_aggregation_measure_and_filter(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        slug = project["slug"]
        created = await client.post(
            _metrics_url(slug),
            json={
                "kind": "fact",
                "name": "edit_fact",
                "display_name": "Edit Fact",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
            },
        )
        assert created.status_code == 201, created.text
        metric = created.json()
        assert metric["aggregation"] == "count"

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "definition": {
                    "kind": "fact",
                    "composition": "single",
                    "fact_table_id": fact_table["id"],
                    "aggregation": "sum",
                    "interval": "1h",
                    "measure_column": "amount",
                    "row_filter": "exclude_test",
                }
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["kind"] == "fact"
        assert data["aggregation"] == "sum"
        assert data["config"]["measure_column"] == "amount"
        # Legacy single ``row_filter`` is folded into the effective ``row_filters``.
        assert data["config"]["row_filters"] == ["exclude_test"]

    async def test_edit_fact_operand_unknown_column_rejected(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        # The same service-level allowlist that guards create also guards update.
        slug = project["slug"]
        created = await client.post(
            _metrics_url(slug),
            json={
                "kind": "fact",
                "name": "edit_fact_bad",
                "display_name": "Edit Fact Bad",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
            },
        )
        assert created.status_code == 201, created.text
        resp = await client.patch(
            f"{_metrics_url(slug)}/{created.json()['id']}",
            json={
                "definition": {
                    "kind": "fact",
                    "composition": "single",
                    "fact_table_id": fact_table["id"],
                    "aggregation": "sum",
                    "interval": "1h",
                    "measure_column": "not_a_column",
                }
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_edit_fact_condition_value_type_rejected_before_collection(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        slug = project["slug"]
        created = await client.post(
            _metrics_url(slug),
            json={
                "kind": "fact",
                "name": "edit_fact_condition_type",
                "display_name": "Edit fact condition type",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "conditions": [{"column": "amount", "operator": "gt", "value": "3"}],
            },
        )
        assert created.status_code == 201, created.text

        resp = await client.patch(
            f"{_metrics_url(slug)}/{created.json()['id']}",
            json={
                "definition": {
                    "kind": "fact",
                    "composition": "single",
                    "fact_table_id": fact_table["id"],
                    "aggregation": "count",
                    "interval": "1h",
                    "conditions": [{"column": "amount", "operator": "lte", "value": "a lot"}],
                }
            },
        )
        assert resp.status_code == 422, resp.text
        assert "numeric value" in resp.json()["detail"]

    async def test_change_kind_sql_to_fact_clears_values_and_stores_config(
        self, client: AsyncClient, project: dict, data_source: dict, fact_table: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "kind_change")
        await _seed_collected_data(metric["id"])
        # Stamp a collection state so we can assert it is reset on the kind change.
        async with TestSessionLocal() as session:
            from tripl.models.metric_definition import MetricDefinition

            row = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
            assert row is not None
            row.last_collection_status = "ok"
            row.last_collected_at = datetime(2026, 1, 1, tzinfo=UTC)
            row.last_collection_window_to = datetime(2026, 1, 2, tzinfo=UTC)
            await session.commit()
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "definition": {
                    "kind": "fact",
                    "composition": "single",
                    "fact_table_id": fact_table["id"],
                    "aggregation": "sum",
                    "interval": "1h",
                    "measure_column": "amount",
                }
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # New definition stored.
        assert data["kind"] == "fact"
        assert data["aggregation"] == "sum"
        assert data["fact_table_id"] == fact_table["id"]
        assert data["config"]["measure_column"] == "amount"
        # ``sql`` left its data source behind; ``fact`` takes it from the table.
        assert data["data_source_id"] is None
        # Collection stamp reset.
        assert data["last_collection_status"] is None
        assert data["last_collected_at"] is None
        async with TestSessionLocal() as session:
            from tripl.models.metric_definition import MetricDefinition

            cleared = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
            assert cleared is not None
            assert cleared.last_collection_window_to is None
        # Every incompatible collected row was cleared.
        assert await _count_collected_data(metric["id"]) == (0, 0, 0)

    async def test_same_kind_config_tweak_clears_collected_values(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "same_kind")
        await _seed_collected_data(metric["id"])
        async with TestSessionLocal() as session:
            from tripl.models.metric_definition import MetricDefinition

            row = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
            assert row is not None
            row.last_collection_status = "ok"
            row.last_collected_at = datetime(2026, 1, 1, tzinfo=UTC)
            row.last_collection_window_to = datetime(2026, 1, 2, tzinfo=UTC)
            await session.commit()

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "definition": {
                    "kind": "sql",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {
                        "metric_sql": "SELECT t AS bucket, count() AS value FROM e2",
                        "time_column": "bucket",
                    },
                }
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["config"]["metric_sql"].endswith("FROM e2")
        assert data["last_collection_status"] is None
        assert data["last_collected_at"] is None
        async with TestSessionLocal() as session:
            from tripl.models.metric_definition import MetricDefinition

            cleared = await session.get(MetricDefinition, uuid.UUID(metric["id"]))
            assert cleared is not None
            assert cleared.last_collection_window_to is None
        assert await _count_collected_data(metric["id"]) == (0, 0, 0)

    async def test_same_definition_presentation_edit_keeps_collected_values(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "same_definition")
        await _seed_collected_data(metric["id"])

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "display_name": "Same Definition Renamed",
                "definition": {
                    "kind": "sql",
                    "data_source_id": data_source["id"],
                    "interval": metric["interval"],
                    "config": {
                        "metric_sql": metric["config"]["metric_sql"],
                        "time_column": metric["config"]["time_column"],
                    },
                },
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["display_name"] == "Same Definition Renamed"
        assert data["config"] == metric["config"]
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)

    async def test_internal_name_is_immutable_on_update(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "stable_name")
        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "name": "renamed",
                "display_name": "Renamed Display",
                "definition": {
                    "kind": "sql",
                    "name": "renamed_in_definition",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
                },
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # The internal query identifier is unchanged; only display_name moved.
        assert data["name"] == "stable_name"
        assert data["display_name"] == "Renamed Display"


class _PreviewStubAdapter:
    """Warehouse stub for the preview endpoint; records the wrapped call."""

    def __init__(
        self,
        columns: list[str],
        rows: list[tuple[object, ...]],
        error: Exception | None = None,
    ) -> None:
        self.columns = columns
        self.rows = rows
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        self.calls.append(
            {
                "sql": base_query,
                "limit": limit,
                "time_column": time_column,
                "time_from": time_from,
                "time_to": time_to,
            }
        )
        if self.error is not None:
            raise self.error
        return self.columns, self.rows

    def close(self) -> None:
        self.closed = True


def _patch_preview_adapter(
    monkeypatch: pytest.MonkeyPatch, adapter: _PreviewStubAdapter
) -> list[uuid.UUID]:
    """Patch ``registry.build_adapter`` (the service imports it lazily) to
    return the stub; returns the list of data-source ids it was built for."""
    built: list[uuid.UUID] = []

    def _build(ds: object) -> _PreviewStubAdapter:
        built.append(ds.id)  # type: ignore[attr-defined]
        return adapter

    monkeypatch.setattr(adapter_registry, "build_adapter", _build)
    return built


def _preview_url(slug: str) -> str:
    return f"{_metrics_url(slug)}/preview"


class TestPreview:
    """POST /metrics/preview: stateless dry-run of a sql-kind SELECT."""

    async def test_happy_path_returns_columns_and_bucketed_points(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(
            ["t", "value"],
            [
                (datetime(2026, 7, 1, 10, 0, tzinfo=UTC), 5),
                # Coerced like a real collection: string value, mid-bucket time.
                (datetime(2026, 7, 1, 11, 30, tzinfo=UTC), "7.5"),
            ],
        )
        built = _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _preview_url(project["slug"]),
            json={
                "data_source_id": data_source["id"],
                "sql": "SELECT toStartOfHour(ts) AS t, count() AS value FROM e GROUP BY t",
                "time_column": "t",
                "interval": "1h",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        assert body["columns"] == ["t", "value"]
        assert body["truncated"] is False
        assert body["point_count"] == 2
        points = body["points"]
        assert [point["value"] for point in points] == [5.0, 7.5]
        assert datetime.fromisoformat(points[0]["bucket"]) == datetime(2026, 7, 1, 10, tzinfo=UTC)
        # 11:30 floors to the 11:00 bucket on a 1h interval.
        assert datetime.fromisoformat(points[1]["bucket"]) == datetime(2026, 7, 1, 11, tzinfo=UTC)

        # The adapter was built for the requested data source, queried once with
        # the hard row cap and a 50-bucket window ending now, and closed.
        assert built == [uuid.UUID(data_source["id"])]
        assert len(adapter.calls) == 1
        call = adapter.calls[0]
        assert call["limit"] == 200
        assert call["time_column"] == "t"
        assert call["time_to"] - call["time_from"] == timedelta(hours=50)
        assert adapter.closed is True

    async def test_custom_value_column_and_truncation_flag(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Exactly the row cap comes back -> flagged truncated; same-bucket rows
        # overwrite (worker semantics), so one point remains per bucket.
        rows = [(datetime(2026, 7, 1, 10, 0, tzinfo=UTC), float(index)) for index in range(200)]
        adapter = _PreviewStubAdapter(["t", "sessions"], rows)
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _preview_url(project["slug"]),
            json={
                "data_source_id": data_source["id"],
                "sql": "SELECT t, sessions FROM e",
                "time_column": "t",
                "value_column": "sessions",
                "interval": "1h",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        assert body["truncated"] is True
        assert body["point_count"] == 1
        assert body["points"][0]["value"] == 199.0

    async def test_non_select_sql_returns_error_payload(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(["t", "value"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _preview_url(project["slug"]),
            json={
                "data_source_id": data_source["id"],
                "sql": "DROP TABLE users",
                "time_column": "t",
                "interval": "1h",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is not None
        assert "read-only SELECT" in body["error"]
        assert body["points"] == []
        assert body["point_count"] == 0
        assert body["truncated"] is False
        # Invalid SQL never reaches the warehouse.
        assert adapter.calls == []

    async def test_missing_time_column_in_result_returns_error_payload(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # The SELECT projects both columns textually, but the warehouse result
        # comes back without the time column -> reported as a user-level error.
        adapter = _PreviewStubAdapter(["value", "other"], [(1.0, "x")])
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _preview_url(project["slug"]),
            json={
                "data_source_id": data_source["id"],
                "sql": "SELECT t, value FROM e",
                "time_column": "t",
                "interval": "1d",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is not None
        assert "'t'" in body["error"]
        assert body["columns"] == ["value", "other"]
        assert body["points"] == []

    async def test_warehouse_error_is_trimmed_into_error_payload(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter([], [], error=RuntimeError("x" * 800))
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _preview_url(project["slug"]),
            json={
                "data_source_id": data_source["id"],
                "sql": "SELECT t, value FROM e",
                "time_column": "t",
                "interval": "1h",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is not None
        assert body["error"].startswith("x")
        # Trimmed to ~500 chars so a driver dump never reaches the client.
        assert len(body["error"]) <= 510
        assert body["points"] == []

    async def test_unknown_data_source_returns_404(
        self,
        client: AsyncClient,
        project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(["t", "value"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _preview_url(project["slug"]),
            json={
                "data_source_id": str(uuid.uuid4()),
                "sql": "SELECT t, value FROM e",
                "time_column": "t",
                "interval": "1h",
            },
        )
        assert resp.status_code == 404, resp.text
        assert adapter.calls == []

    async def test_requires_editor_role(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Same auth gate as metric create: a viewer is rejected with 403.
        adapter = _PreviewStubAdapter(["t", "value"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        # A persisted viewer MEMBER: a non-member would get 404, not 403.
        viewer = await persisted_member_user(uuid.UUID(project["id"]), role="viewer")

        async def _viewer() -> User:
            return viewer

        app.dependency_overrides[get_current_user] = _viewer
        try:
            resp = await client.post(
                _preview_url(project["slug"]),
                json={
                    "data_source_id": data_source["id"],
                    "sql": "SELECT t, value FROM e",
                    "time_column": "t",
                    "interval": "1h",
                },
            )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

        assert resp.status_code == 403, resp.text
        assert adapter.calls == []

    async def test_connection_failure_is_masked_not_echoed(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # A driver connection error names the host, the port and the user. It is a
        # failure to REACH the warehouse, not the engine's verdict on the SQL, so
        # it is replaced wholesale (the raw text stays in the log).
        adapter = _PreviewStubAdapter(
            [],
            [],
            error=RuntimeError("connection refused: 10.1.2.3:9440 user=analytics_admin"),
        )
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _preview_url(project["slug"]),
            json={
                "data_source_id": data_source["id"],
                "sql": "SELECT t, value FROM e",
                "time_column": "t",
                "interval": "1h",
            },
        )
        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert "10.1.2.3" not in error
        assert "9440" not in error
        assert "analytics_admin" not in error
        assert "Could not reach the data source" in error


def _fact_preview_url(slug: str) -> str:
    return f"{_metrics_url(slug)}/fact-preview"


class TestFactPreview:
    """POST /metrics/fact-preview: dry-run of ONE fact operand's row filter.

    The gap this closes: a fact metric's filters were compiled per dialect but
    never EXECUTED before save, so the first run happened inside a Celery worker
    where the user could not see it fail.
    """

    async def test_happy_path_executes_the_collector_s_own_compiled_sql(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(
            ["created_at", "amount", "user_id", "is_test"],
            [(datetime(2026, 7, 1, 10, 0, tzinfo=UTC), 12.5, "u1", 0)],
        )
        built = _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _fact_preview_url(project["slug"]),
            json={
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "sum",
                "measure_column": "amount",
                "row_filters": ["exclude_test"],
                "conditions": [{"column": "amount", "operator": "gt", "value": 3}],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is None
        assert body["row_count"] == 1
        assert body["columns"] == ["created_at", "amount", "user_id", "is_test"]

        # The SQL that ran is the collector's, not a re-implementation: the named
        # filter resolved by name, the structured condition compiled for the
        # source's dialect (ClickHouse -> back-quoted identifier), both ANDed and
        # wrapped around the fact table's own SELECT.
        assert built == [uuid.UUID(data_source["id"])]
        assert len(adapter.calls) == 1
        call = adapter.calls[0]
        assert call["sql"] == (
            "SELECT * FROM (SELECT created_at, amount, user_id, is_test FROM orders) "
            "AS _filtered WHERE (is_test = 0) AND (`amount` > 3)"
        )
        # A probe, not a data dump: one row, over a bounded recent window of the
        # fact table's timestamp column.
        assert call["limit"] == 1
        assert call["time_column"] == "created_at"
        assert call["time_to"] - call["time_from"] == timedelta(days=7)
        assert adapter.closed is True

    async def test_compiled_sql_matches_the_worker_resolver_exactly(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Preview and collection must not be able to drift: assert the previewed
        # string is byte-identical to what the worker's own resolver produces for
        # the same operand config.
        adapter = _PreviewStubAdapter(["created_at", "amount"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        operand = {
            "fact_table_id": bound_fact_table["id"],
            "aggregation": "count",
            "filter_sql": "user_id != ''",
            "conditions": [{"column": "amount", "operator": "in", "value": [1, 2]}],
        }
        resp = await client.post(_fact_preview_url(project["slug"]), json=operand)
        assert resp.status_code == 200, resp.text
        assert resp.json()["error"] is None

        async with TestSessionLocal() as session:
            fact_table = await session.get(FactTable, uuid.UUID(bound_fact_table["id"]))
            assert fact_table is not None
            expected = mc._resolve_fact_operand_query(
                fact_table,
                mc._operand_from_config(operand),
                dialect=SqlDialect.clickhouse,
            )
        assert adapter.calls[0]["sql"] == expected

    async def test_unknown_named_filter_returns_error_payload(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(["created_at", "amount"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _fact_preview_url(project["slug"]),
            json={
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "count",
                "row_filters": ["no_such_filter"],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is not None
        assert "no_such_filter" in body["error"]
        assert body["row_count"] == 0
        # A filter that cannot be compiled never reaches the warehouse.
        assert adapter.calls == []

    async def test_warehouse_rejection_returns_the_engine_s_own_message(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # The engine's verdict on the user's filter IS the product here: it names
        # the mistake ("amont") and is surfaced verbatim on a 200.
        adapter = _PreviewStubAdapter(
            [],
            [],
            error=RuntimeError("Code: 47. DB::Exception: Missing columns: 'amont'"),
        )
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _fact_preview_url(project["slug"]),
            json={
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "count",
                "filter_sql": "amont > 0",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is not None
        assert "Missing columns: 'amont'" in body["error"]
        assert body["row_count"] == 0

    async def test_connection_failure_does_not_leak_host_or_credentials(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(
            [],
            [],
            error=RuntimeError(
                "could not connect to server at warehouse.internal:5432, "
                "password authentication failed for user 'tripl_ro'"
            ),
        )
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _fact_preview_url(project["slug"]),
            json={"fact_table_id": bound_fact_table["id"], "aggregation": "count"},
        )
        assert resp.status_code == 200, resp.text
        error = resp.json()["error"]
        assert "warehouse.internal" not in error
        assert "5432" not in error
        assert "tripl_ro" not in error
        assert "Could not reach the data source" in error

    async def test_mutating_filter_sql_is_rejected_at_the_schema_boundary(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # The read-only gate is NOT relaxed to make previewing easier: the preview
        # body is the same FactOperand a save sends, so validate_sql_fragment
        # rejects a mutating / stacked / subquery fragment with a 422 before any
        # adapter exists.
        adapter = _PreviewStubAdapter(["created_at"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        for filter_sql in (
            "1=1; DROP TABLE orders",
            "user_id IN (SELECT id FROM admins)",
            "1=1 UNION SELECT 1",
            "amount > 0 -- comment",
        ):
            resp = await client.post(
                _fact_preview_url(project["slug"]),
                json={
                    "fact_table_id": bound_fact_table["id"],
                    "aggregation": "count",
                    "filter_sql": filter_sql,
                },
            )
            assert resp.status_code == 422, f"{filter_sql!r} -> {resp.text}"
        assert adapter.calls == []

    async def test_measure_column_absent_from_filtered_query_returns_error(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # The measure is checked against the columns the FILTERED query actually
        # returned — the same allowlist check `_aggregate_fact_window` runs before
        # it aggregates.
        adapter = _PreviewStubAdapter(["created_at", "user_id"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _fact_preview_url(project["slug"]),
            json={
                "fact_table_id": bound_fact_table["id"],
                "aggregation": "sum",
                "measure_column": "amount",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["error"] is not None
        assert "amount" in body["error"]
        assert body["columns"] == ["created_at", "user_id"]

    async def test_fact_table_without_data_source_returns_error_payload(
        self,
        client: AsyncClient,
        project: dict,
        fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(["created_at"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _fact_preview_url(project["slug"]),
            json={"fact_table_id": fact_table["id"], "aggregation": "count"},
        )
        assert resp.status_code == 200, resp.text
        assert "no data source" in resp.json()["error"]
        assert adapter.calls == []

    async def test_unknown_fact_table_returns_404(
        self,
        client: AsyncClient,
        project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(["created_at"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        resp = await client.post(
            _fact_preview_url(project["slug"]),
            json={"fact_table_id": str(uuid.uuid4()), "aggregation": "count"},
        )
        assert resp.status_code == 404, resp.text
        assert adapter.calls == []

    async def test_requires_editor_role(
        self,
        client: AsyncClient,
        project: dict,
        bound_fact_table: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        adapter = _PreviewStubAdapter(["created_at"], [])
        _patch_preview_adapter(monkeypatch, adapter)

        # A persisted viewer MEMBER: a non-member would get 404, not 403.
        viewer = await persisted_member_user(uuid.UUID(project["id"]), role="viewer")

        async def _viewer() -> User:
            return viewer

        app.dependency_overrides[get_current_user] = _viewer
        try:
            resp = await client.post(
                _fact_preview_url(project["slug"]),
                json={"fact_table_id": bound_fact_table["id"], "aggregation": "count"},
            )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

        assert resp.status_code == 403, resp.text
        assert adapter.calls == []
