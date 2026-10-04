"""Batch 6, lane A: the metric catalog service, its schemas and its router.

Each defect is pinned by an assertion that goes red the moment the production
change is reverted:

* a metric saved before a config key existed compared UNEQUAL
  to its own unchanged definition, because ``to_definition_values()`` fills in
  today's defaults while the check compared raw stored dicts. Editing only the
  description of such a metric — the form always resends the definition —
  deleted every value, breakdown and anomaly it had collected.
* that same clear could run while a collection was in flight.
  The worker holds the old definition in locals, so it kept writing old buckets
  into the cleared series and then stamped a watermark over them, and the NULL
  status it left behind released the one-active-job guard.
* every metric created through the form landed on order 0, and
  reorder only permuted the order values it found, so a permutation of equal
  values wrote nothing: drag-to-reorder never persisted, and a duplicated id in
  the request was an ``IndexError`` 500.
* ``active_total`` searched a different population than the
  rows it sits above: name/display_name only, against a stripped term, while
  the list also searched descriptions with the raw one.
* deleting a metric left its catalog-scope anomalies behind
  forever. They carry no FK, and every other purge finds anomalies through the
  ids of metrics that still exist.
* a broker outage put the whole collection group into the error
  state by assigning the two columns by hand, bypassing ``mark_collection_error``
  and leaving ``last_collection_failed_at`` unstamped, so the dispatcher's
  post-error cooldown measured from unrelated history.
* ``POST /metrics/bulk-update`` recorded nothing, so a bulk
  archive was the one mutation in its group invisible in the Audit log.
* the duplicate-id ``IndexError`` 500 in reorder, which is the
  same defect ``tripl-0zpq.175`` covers above and shares its test.
* ``metric_definition.collect`` was filed with an empty
  ``target_name``, so the row named no metric.
* the data-source scope refusal claimed the row was missing on
  anti-enumeration grounds that the workspace-wide list route already gives away.
* a denominator could be parked on a ``single`` /
  ``per_distinct_user`` composition metric, where the collector never reads it.
  Dead config, until an event merge made the two operands equal and the merge
  guard put the metric in the error state with a message about a constant-1.0
  ratio, although it is a plain count and still collects correctly.
* a sql metric whose outer projection does not name the time
  column saved with 201 and then failed EVERY collection, reported as "Scan
  failed due to an internal error." because the worker's refusal is a bare
  ``ValueError``.
* fact-metric breakdown / app_version / platform columns were
  never checked against the fact table. One typo failed the whole metric on every
  tick, top-line series included, because the assembly loop re-raises a breakdown
  scan's error before writing anything.
* catalog ``breakdown_columns`` were not deduplicated, unlike
  every sibling scalar-column field. A repeated column made the Postgres upsert
  hit the same conflict key twice in one statement.
* the data-source scoping rule lived inline in the async save
  path, so the collector could not apply it to an already-stored row.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select

# Imported for its side effect and BEFORE anything reaches a worker task module:
# that package is import-order sensitive (alerts -> celery_app -> metrics ->
# alerts), and the definition-update guard below pulls in
# ``worker.tasks.metrics.schedule`` lazily from inside the request.
import tripl.worker.celery_app  # noqa: F401
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import AnomalyDirection, MetricScopeType
from tripl.models.fact_table import FactTable
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import (
    COLLECTION_STATUS_ERROR,
    COLLECTION_STATUS_RUNNING,
    MetricDefinition,
)
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.scan_config import ScanConfig
from tripl.services import metric_definition_service
from tripl.services.data_source_scope import DATA_SOURCE_NOT_AVAILABLE
from tripl.tests.conftest import TestSessionLocal


def _metrics_url(slug: str) -> str:
    return f"/api/v1/projects/{slug}/metrics"


@pytest.fixture
async def project(client: AsyncClient) -> dict:
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Batch6 Catalog", "slug": "batch6-catalog", "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def data_source(client: AsyncClient, project: dict) -> dict:
    """A data source this project is allowed to use.

    The rule the sql-metric save path applies is OWNERSHIP, not binding (see
    ``services/data_source_scope``): a workspace-global source (``project_id``
    NULL) that no project scans is already reachable from everywhere, so this
    source would be in scope with no ``ScanConfig`` at all. The binding is added
    anyway so the claim is explicit — it is this project that scans it, which is
    what keeps it in scope against the tests below that hand a SECOND project a
    binding to a source of its own.
    """
    resp = await client.post(
        "/api/v1/data-sources",
        json={
            "name": "Batch6 CH",
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
                name="batch6-binding",
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


async def _create_fact_metric(
    client: AsyncClient,
    slug: str,
    fact_table_id: str,
    name: str,
    **extra: object,
) -> dict:
    payload: dict = {
        "kind": "fact",
        "name": name,
        "display_name": name.upper(),
        "composition": "single",
        "fact_table_id": fact_table_id,
        "aggregation": "count",
        "interval": "1h",
        **extra,
    }
    resp = await client.post(_metrics_url(slug), json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _seed_collected_data(metric_id: str) -> None:
    """One value + one breakdown + one catalog-scope anomaly for a metric."""
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
    """(values, breakdowns, catalog-scope anomalies) currently stored for a metric."""
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


async def _drop_config_key(metric_id: str, key: str) -> None:
    """Age a stored config back to the shape it had before ``key`` existed."""
    async with TestSessionLocal() as session:
        row = await session.get(MetricDefinition, uuid.UUID(metric_id))
        assert row is not None
        config = dict(row.config or {})
        assert key in config, f"{key} is not in the stored config; the test is aiming at nothing"
        config.pop(key)
        row.config = config
        await session.commit()


async def _stamp_running(metric_id: str) -> None:
    """Put the metric in the state a dispatched collection leaves it in."""
    async with TestSessionLocal() as session:
        row = await session.get(MetricDefinition, uuid.UUID(metric_id))
        assert row is not None
        row.last_collection_status = COLLECTION_STATUS_RUNNING
        await session.commit()


async def _force_catalog_position(metric_id: str, *, order: int, created_at: datetime) -> None:
    """Rewrite a metric's ordering columns to the pre-fix catalog shape.

    Every metric created through the form used to land on order 0. ``created_at``
    is pinned too, because the list's tiebreak is "newest first" and SQLite's
    ``CURRENT_TIMESTAMP`` has one-second resolution — without it these tests
    would be about clock granularity rather than about the reorder write.
    """
    async with TestSessionLocal() as session:
        row = await session.get(MetricDefinition, uuid.UUID(metric_id))
        assert row is not None
        row.order = order
        row.created_at = created_at
        await session.commit()


async def _drop_fact_table_column(fact_table_id: str, column: str) -> None:
    """Take a column out of the fact table's STORED column snapshot.

    The catalog validates dimensions against ``FactTable.columns``, which is a
    snapshot the preview writes; the collector never reads it and introspects the
    warehouse live instead. A table saved through the API without a preview, or
    one whose warehouse column was renamed since, therefore carries a stored
    dimension its own snapshot does not list — the legacy population, reproduced
    here in one line.
    """
    async with TestSessionLocal() as session:
        row = await session.get(FactTable, uuid.UUID(fact_table_id))
        assert row is not None
        stored = list(row.columns or [])
        remaining = [item for item in stored if item.get("name") != column]
        assert len(remaining) < len(stored), f"{column} is not in the snapshot; aiming at nothing"
        row.columns = remaining
        await session.commit()


async def _stored_metric(metric_id: str) -> MetricDefinition:
    async with TestSessionLocal() as session:
        row = await session.get(MetricDefinition, uuid.UUID(metric_id))
        assert row is not None
        return row


async def _audit_rows(action: str) -> list[AuditLog]:
    """Every audit row filed under one action, oldest first.

    Read straight from the table rather than through ``GET /api/v1/audit``: these
    assertions are about what the route RECORDED, and the reader's filtering and
    redaction are somebody else's test.
    """
    async with TestSessionLocal() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at.asc())
        )
        return list(result.scalars().all())


async def _bind_data_source(project_id: str, data_source_id: str, name: str) -> None:
    """Point a project's ScanConfig at a data source.

    On a workspace-global source (``project_id`` NULL) this is what CLAIMS it:
    ``services/data_source_scope`` reads "scanned by some other project and not
    by this one" as "theirs", so binding a source to the NEIGHBOUR project is how
    these tests build an out-of-scope source. A source nobody scans is shared and
    reachable from every project, so the binding is never what grants access —
    it is what withholds it from everyone else.
    """
    async with TestSessionLocal() as session:
        session.add(
            ScanConfig(
                project_id=uuid.UUID(project_id),
                data_source_id=uuid.UUID(data_source_id),
                name=name,
                base_query="SELECT 1",
            )
        )
        await session.commit()


class TestLegacyConfigShapeIsNotADefinitionChange:
    """a defaulted config key added later is not an edit."""

    async def test_description_edit_of_a_pre_conditions_fact_metric_keeps_its_history(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        slug = project["slug"]
        metric = await _create_fact_metric(client, slug, fact_table["id"], "legacy_fact")
        # A fact metric saved before ``conditions`` was added to the config.
        await _drop_config_key(metric["id"], "conditions")
        await _seed_collected_data(metric["id"])

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "description": "Now with a description",
                # The form always resends the definition, unchanged.
                "definition": {
                    "kind": "fact",
                    "composition": "single",
                    "fact_table_id": fact_table["id"],
                    "aggregation": "count",
                    "interval": "1h",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Now with a description"
        # The definition did not change, so nothing collected may be deleted.
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)

    async def test_description_edit_of_a_pre_value_column_sql_metric_keeps_its_history(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "legacy_sql")
        # A sql metric saved before ``value_column`` was added to ``SqlConfig``.
        await _drop_config_key(metric["id"], "value_column")
        await _seed_collected_data(metric["id"])

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "description": "Still the same query",
                "definition": {
                    "kind": "sql",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {
                        "metric_sql": metric["config"]["metric_sql"],
                        "time_column": metric["config"]["time_column"],
                    },
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)

    async def test_a_real_config_edit_of_a_legacy_metric_still_clears_the_series(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """The comparison must stay strict about changes that DO mean something."""
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "legacy_sql_changed")
        await _drop_config_key(metric["id"], "value_column")
        await _seed_collected_data(metric["id"])

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "definition": {
                    "kind": "sql",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {
                        "metric_sql": "SELECT 2 AS value, now() AS t",
                        "time_column": "t",
                    },
                }
            },
        )
        assert resp.status_code == 200, resp.text
        assert await _count_collected_data(metric["id"]) == (0, 0, 0)


class TestDefinitionChangeDuringCollection:
    """a running collection owns the series until it finishes."""

    async def test_material_definition_change_is_refused_while_a_collection_runs(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "running_edit")
        await _seed_collected_data(metric["id"])
        await _stamp_running(metric["id"])

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "display_name": "Renamed Mid-Run",
                "definition": {
                    "kind": "sql",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {
                        "metric_sql": "SELECT 99 AS value, now() AS t",
                        "time_column": "t",
                    },
                },
            },
        )
        assert resp.status_code == 409, resp.text
        assert "already running" in resp.json()["detail"]

        # Nothing was applied: not the definition, not the presentation field
        # that travelled with it, and above all not the clear.
        stored = await _stored_metric(metric["id"])
        assert stored.config["metric_sql"] == "SELECT 1 AS value, now() AS t"
        assert stored.display_name == metric["display_name"]
        # The guard the worker and the scheduler share is still standing.
        assert stored.last_collection_status == COLLECTION_STATUS_RUNNING
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)

    async def test_presentation_edit_is_allowed_while_a_collection_runs(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """Only a MATERIAL change races the worker; a rename never touches it."""
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "running_rename")
        await _seed_collected_data(metric["id"])
        await _stamp_running(metric["id"])

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "display_name": "Renamed Safely",
                "definition": {
                    "kind": "sql",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {
                        "metric_sql": metric["config"]["metric_sql"],
                        "time_column": metric["config"]["time_column"],
                    },
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["display_name"] == "Renamed Safely"
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)


class TestCatalogOrdering:
    """a catalog of ties cannot be reordered."""

    async def test_metrics_created_without_an_order_are_appended_not_stacked(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        first = await _create_sql_metric(client, slug, data_source["id"], "ord_first")
        second = await _create_sql_metric(client, slug, data_source["id"], "ord_second")
        third = await _create_sql_metric(client, slug, data_source["id"], "ord_third")

        orders = [first["order"], second["order"], third["order"]]
        assert orders == [0, 1, 2], f"new metrics must take distinct positions, got {orders}"

    async def test_reorder_persists_when_the_metrics_share_an_order(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        older = await _create_sql_metric(client, slug, data_source["id"], "tie_older")
        newer = await _create_sql_metric(client, slug, data_source["id"], "tie_newer")
        # The state of every catalog whose metrics predate the append rule.
        await _force_catalog_position(
            older["id"], order=0, created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        await _force_catalog_position(
            newer["id"], order=0, created_at=datetime(2026, 1, 2, tzinfo=UTC)
        )

        listed = await client.get(_metrics_url(slug))
        assert [item["id"] for item in listed.json()["items"]] == [newer["id"], older["id"]]

        resp = await client.patch(
            f"{_metrics_url(slug)}/reorder",
            json={"metric_ids": [older["id"], newer["id"]]},
        )
        assert resp.status_code == 200, resp.text

        # The echoed response always looked right; what used to spring back is
        # the refetch, so that is what is asserted.
        refetched = await client.get(_metrics_url(slug))
        items = refetched.json()["items"]
        assert [item["order"] for item in items] == [0, 1]
        assert [item["id"] for item in items] == [older["id"], newer["id"]]

    async def test_reorder_rejects_a_duplicated_id(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "dup_id")
        resp = await client.patch(
            f"{_metrics_url(slug)}/reorder",
            json={"metric_ids": [metric["id"], metric["id"]]},
        )
        # Used to walk off the end of the slot list with an IndexError 500.
        assert resp.status_code == 400, resp.text

    async def test_a_partial_reorder_does_not_tie_a_metric_it_did_not_send(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """A filtered view sends two of four rows; the other two must stay put.

        Forcing only the SENT rows' own order values strictly increasing bumped
        one of a tied pair onto a value an unsent metric already held — trading
        the tie it removed for a new one a slot down, where the same
        ``created_at`` tie-break decides the catalog all over again.
        """
        slug = project["slug"]
        first = await _create_sql_metric(client, slug, data_source["id"], "part_a")
        second = await _create_sql_metric(client, slug, data_source["id"], "part_b")
        third = await _create_sql_metric(client, slug, data_source["id"], "part_c")
        fourth = await _create_sql_metric(client, slug, data_source["id"], "part_d")
        # A legacy tie at the head of the catalog, and two metrics below it that
        # the filtered request never mentions. ``third`` sits on 1 — the value the
        # bump reached for.
        await _force_catalog_position(
            first["id"], order=0, created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        await _force_catalog_position(
            second["id"], order=0, created_at=datetime(2026, 1, 2, tzinfo=UTC)
        )
        await _force_catalog_position(
            third["id"], order=1, created_at=datetime(2026, 1, 3, tzinfo=UTC)
        )
        await _force_catalog_position(
            fourth["id"], order=2, created_at=datetime(2026, 1, 4, tzinfo=UTC)
        )

        resp = await client.patch(
            f"{_metrics_url(slug)}/reorder",
            json={"metric_ids": [first["id"], second["id"]]},
        )
        assert resp.status_code == 200, resp.text

        listed = await client.get(_metrics_url(slug))
        items = listed.json()["items"]
        orders = [item["order"] for item in items]
        # Used to be [0, 1, 1, 2]: ``second`` was bumped onto ``third``'s order.
        assert len(set(orders)) == len(orders), f"the reorder left a tie: {orders}"
        # And the tie put ``third`` — which nobody dragged — above ``second``.
        assert [item["id"] for item in items] == [
            first["id"],
            second["id"],
            third["id"],
            fourth["id"],
        ]

    async def test_move_up_separates_metrics_that_share_an_order(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        older = await _create_sql_metric(client, slug, data_source["id"], "mv_tie_older")
        newer = await _create_sql_metric(client, slug, data_source["id"], "mv_tie_newer")
        await _force_catalog_position(
            older["id"], order=0, created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        await _force_catalog_position(
            newer["id"], order=0, created_at=datetime(2026, 1, 2, tzinfo=UTC)
        )

        # Listed newest-first, so the older metric is second and can move up.
        resp = await client.patch(
            f"{_metrics_url(slug)}/{older['id']}/move", json={"direction": "up"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["order"] == 0

        moved_past = await client.get(f"{_metrics_url(slug)}/{newer['id']}")
        # Swapping two equal order values used to leave both rows exactly as they
        # were, so the move reported success and moved nothing.
        assert moved_past.json()["order"] == 1


class TestActiveTotalMatchesTheListedRows:
    """the KPI strip must count the population on screen."""

    @staticmethod
    async def _seed_description_matches(
        client: AsyncClient, slug: str, data_source_id: str
    ) -> None:
        """Three active metrics whose ONLY match is in the description.

        The term sits at the END of the description on purpose: it is what makes
        an unstripped ``%revenue %`` pattern miss rows a stripped one finds.
        """
        for index in range(3):
            await _create_sql_metric(
                client,
                slug,
                data_source_id,
                f"kpi_{index}",
                description="growth of net revenue",
                status="active",
            )

    async def test_a_description_only_match_is_counted_as_active(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        await self._seed_description_matches(client, slug, data_source["id"])

        resp = await client.get(_metrics_url(slug), params={"search": "revenue"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        # Used to be 0: the count ignored descriptions, so the strip contradicted
        # the three rows listed right below it.
        assert body["active_total"] == 3

    async def test_a_trailing_space_does_not_split_the_two_counts(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        await self._seed_description_matches(client, slug, data_source["id"])

        # The catalog sends the debounced input untrimmed.
        resp = await client.get(_metrics_url(slug), params={"search": "revenue "})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        assert body["active_total"] == 3


class TestDeleteRemovesMetricScopeAnomalies:
    """a deleted metric's anomalies are unreachable, not gone."""

    async def test_deleting_a_metric_deletes_its_catalog_scope_anomalies(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "deleted_with_anomaly")
        await _seed_collected_data(metric["id"])
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)

        resp = await client.delete(f"{_metrics_url(slug)}/{metric['id']}")
        assert resp.status_code == 204, resp.text

        # Values and breakdowns cascade through their FK; the anomaly has none,
        # and every later purge looks for it through a metric id that is gone.
        assert await _count_collected_data(metric["id"]) == (0, 0, 0)

    async def test_deleting_the_project_deletes_them_too(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """The other half of the same sweep, and the half nothing covered.

        ``project_service.purge_project_rows`` is the second door onto these
        rows: dropping a project cascades its ``metric_definitions`` away, and a
        ``metric``-scope ``MetricAnomaly`` has no ``project_id`` and no FK to the
        metric it names, so the cascade cannot reach it. It is also the door demo
        reset goes through, which is how a reset workspace accumulated anomalies
        addressed to ids nothing resolves.

        RED on a revert: drop the ``delete(MetricAnomaly)`` statement from
        ``purge_project_rows`` and the anomaly count below stays 1 while the
        values and breakdowns still go to 0 — i.e. exactly the ``(0, 0, 1)``
        asymmetry the sweep exists to close.
        """
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "purged_with_project")
        await _seed_collected_data(metric["id"])
        assert await _count_collected_data(metric["id"]) == (1, 1, 1)

        resp = await client.delete(f"/api/v1/projects/{slug}")
        assert resp.status_code == 204, resp.text

        assert await _count_collected_data(metric["id"]) == (0, 0, 0)


class TestDispatchFailureEntersTheErrorStateThroughTheModel:
    """a broker outage must stamp WHEN it failed, not just that it did."""

    async def test_a_broker_failure_stamps_the_time_the_backoff_measures_from(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "broker_is_down")
        # Never collected, so nothing has stamped a failure time yet: whatever is
        # in that column after the 503 was put there by this request.
        assert (await _stored_metric(metric["id"])).last_collection_failed_at is None

        async def broker_unavailable(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            metric_definition_service, "_dispatch_metric_collection", broker_unavailable
        )

        resp = await client.post(f"{_metrics_url(slug)}/{metric['id']}/collect")
        assert resp.status_code == 503, resp.text

        stored = await _stored_metric(metric["id"])
        assert stored.last_collection_status == COLLECTION_STATUS_ERROR
        assert stored.last_collection_error == "Failed to dispatch collection task to worker"
        # The assertion the fix exists for. The branch used to assign the status
        # and the message by hand, bypassing ``mark_collection_error`` — which
        # the model's docstring calls the only way into the error state — and
        # leaving this NULL. The dispatcher's post-error backoff then read
        # ``last_collection_failed_at or updated_at`` and cooled the metric down
        # from its own edit timestamp, which is to say not at all.
        assert stored.last_collection_failed_at is not None


class TestBulkUpdateLeavesATrail:
    """the one bulk mutation in the group the Audit log could not see."""

    async def test_a_bulk_archive_files_one_row_naming_the_change_and_its_size(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        first = await _create_sql_metric(client, slug, data_source["id"], "bulk_first")
        second = await _create_sql_metric(client, slug, data_source["id"], "bulk_second")

        resp = await client.post(
            f"{_metrics_url(slug)}/bulk-update",
            # One id repeated: the service updates ``set(metric_ids)``, so the row
            # must count metrics rather than list entries.
            json={
                "metric_ids": [first["id"], second["id"], first["id"]],
                "status": "archived",
            },
        )
        assert resp.status_code == 204, resp.text

        rows = await _audit_rows("metric_definition.bulk_update")
        # Used to be zero rows: the route took no ``current_user`` and recorded
        # nothing, so archiving forty metrics at once was invisible while
        # archiving them one at a time filed forty ``metric_definition.update``
        # rows.
        assert len(rows) == 1
        payload = rows[0].payload or {}
        assert payload["status"] == "archived"
        assert payload["count"] == 2
        assert set(payload["metric_ids"]) == {first["id"], second["id"]}
        assert payload["truncated"] is False
        # Fields the request never sent stay out of the row, exactly as they stay
        # out of the UPDATE statement.
        assert "owner_id" not in payload
        assert "anomaly_detection_enabled" not in payload

    async def test_the_archive_still_lands(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """The control: recording must not have replaced the write it records."""
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "bulk_control")

        resp = await client.post(
            f"{_metrics_url(slug)}/bulk-update",
            json={"metric_ids": [metric["id"]], "status": "archived"},
        )
        assert resp.status_code == 204, resp.text

        detail = await client.get(f"{_metrics_url(slug)}/{metric['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "archived"


class TestCollectAuditNamesItsMetric:
    """a collect row that could not say what it collected."""

    async def test_a_manual_collect_files_the_metric_name(
        self,
        client: AsyncClient,
        project: dict,
        data_source: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "collect_named")

        async def dispatched(*_args: object, **_kwargs: object) -> str:
            return "task-batch6"

        monkeypatch.setattr(metric_definition_service, "_dispatch_metric_collection", dispatched)

        resp = await client.post(f"{_metrics_url(slug)}/{metric['id']}/collect")
        assert resp.status_code == 202, resp.text

        rows = await _audit_rows("metric_definition.collect")
        assert len(rows) == 1
        # Used to be "": the row carried a bare UUID, so the Audit log could not
        # tell an owner which metric someone had put through the warehouse —
        # every other row this router files names its metric.
        assert rows[0].target_name == metric["name"]


class TestForeignDataSourceRefusalNamesTheRealCause:
    """ "not found" for a row the caller can already list by name."""

    async def test_a_source_scanned_only_by_another_project_is_refused_as_out_of_scope(
        self, client: AsyncClient, project: dict
    ):
        neighbour = await client.post(
            "/api/v1/projects",
            json={"name": "Batch6 Neighbour", "slug": "batch6-neighbour", "description": ""},
        )
        assert neighbour.status_code == 201, neighbour.text
        created = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Neighbour CH",
                "db_type": "clickhouse",
                "host": "localhost",
                "port": 8123,
                "database_name": "neighbour_db",
            },
        )
        assert created.status_code == 201, created.text
        borrowed = created.json()
        await _bind_data_source(neighbour.json()["id"], borrowed["id"], "neighbour-binding")

        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "borrowed_credential",
                "display_name": "Borrowed",
                "data_source_id": borrowed["id"],
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        )

        assert resp.status_code == 404, resp.text
        # Used to say "Data source not found", justified as anti-enumeration. It
        # sent a legitimate editor hunting for a row that exists and is sitting in
        # the picker they chose it from.
        assert resp.json()["detail"] == "Data source is not available in this project."

    async def test_the_workspace_inventory_the_honest_message_rests_on_is_open(
        self, client: AsyncClient
    ):
        """The premise, pinned: naming the scope leaks nothing the list does not.

        ``GET /api/v1/data-sources`` is workspace-wide for any authenticated
        caller — it redacts host/port/credentials, never the id or the name. That
        is why the refusal above can name the real cause. If this ever narrows to
        the caller's reachable projects, the vague message becomes load-bearing
        again and the refusal's wording should be reconsidered with it.
        """
        created = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Listed Anyway",
                "db_type": "clickhouse",
                "host": "localhost",
                "port": 8123,
                "database_name": "listed_db",
            },
        )
        assert created.status_code == 201, created.text
        unbound = created.json()

        listed = await client.get("/api/v1/data-sources")
        assert listed.status_code == 200, listed.text
        by_id = {item["id"]: item for item in listed.json()}
        assert unbound["id"] in by_id
        assert by_id[unbound["id"]]["name"] == "Listed Anyway"


@pytest.fixture
async def event_type(client: AsyncClient, project: dict) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project['slug']}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def events(client: AsyncClient, project: dict, event_type: dict) -> list[dict]:
    """Two events in one project — a numerator and a would-be denominator."""
    created: list[dict] = []
    for name in ("signup", "purchase"):
        resp = await client.post(
            f"/api/v1/projects/{project['slug']}/events",
            json={"event_type_id": event_type["id"], "name": name},
        )
        assert resp.status_code == 201, resp.text
        created.append(resp.json())
    return created


class TestADenominatorOnlyBelongsToARatio:
    """config the collector never reads must not be storable."""

    async def test_a_single_composition_metric_refuses_a_denominator_event(
        self, client: AsyncClient, project: dict, events: list[dict]
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "signups_with_leftover_denominator",
                "display_name": "Signups",
                "composition": "single",
                "numerator_event_id": events[0]["id"],
                "denominator_event_id": events[1]["id"],
            },
        )
        # Used to be 201. ``_collect_metric_definitions`` reads a denominator only
        # for a ratio, so this stored a field nothing consumed — and then the
        # event-merge guard, which compares the operands for EVERY composition,
        # marked the metric red the day those two events were merged.
        assert resp.status_code == 422, resp.text

    async def test_a_per_distinct_user_metric_refuses_a_denominator_event_type(
        self, client: AsyncClient, project: dict, events: list[dict], event_type: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "dau_with_leftover_denominator",
                "display_name": "DAU",
                "composition": "per_distinct_user",
                "numerator_event_id": events[0]["id"],
                "denominator_event_type_id": event_type["id"],
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_a_ratio_still_takes_both_operands(
        self, client: AsyncClient, project: dict, events: list[dict]
    ):
        """The half that must NOT move: a ratio is the composition that has one."""
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "event_composition",
                "name": "conversion_ratio",
                "display_name": "Conversion",
                "composition": "ratio",
                "numerator_event_id": events[0]["id"],
                "denominator_event_id": events[1]["id"],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["denominator_event_id"] == events[1]["id"]


class TestSqlMetricMustProjectTheTimeColumnItNames:
    """save and collect must agree on the projection rule."""

    # The filed shape: the columns are real, the OUTER projection just does not
    # name them, which is all the collector's textual check can see.
    _WILDCARD_CTE = (
        "WITH b AS (SELECT toStartOfHour(ts) AS t, count() AS value FROM events GROUP BY t) "
        "SELECT * FROM b"
    )

    async def test_a_cte_finished_with_select_star_is_refused_at_save(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "wildcard_cte",
                "display_name": "Wildcard CTE",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {"metric_sql": self._WILDCARD_CTE, "time_column": "t"},
            },
        )
        # Used to be 201, after which every tick raised a bare ValueError inside
        # the worker and surfaced as "Scan failed due to an internal error." — a
        # metric that could never collect and could not say why.
        assert resp.status_code == 422, resp.text
        assert "time column" in resp.text

    async def test_the_same_select_is_refused_on_an_edit(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "edit_into_wildcard")
        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={
                "definition": {
                    "kind": "sql",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {"metric_sql": self._WILDCARD_CTE, "time_column": "t"},
                }
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_a_named_value_column_must_be_projected_too(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "sql",
                "name": "misnamed_value_column",
                "display_name": "Misnamed",
                "data_source_id": data_source["id"],
                "interval": "1d",
                "config": {
                    "metric_sql": (
                        "SELECT toDate(ts) AS bucket, count() AS v FROM e GROUP BY bucket"
                    ),
                    "time_column": "bucket",
                    "value_column": "sessions",
                },
            },
        )
        assert resp.status_code == 422, resp.text
        assert "value column" in resp.text

    async def test_an_unnamed_value_column_must_project_the_value_convention(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """``value_column`` unset means the documented ``value`` column.

        The save used to let this shape through and leave it to the worker,
        which refused it on every tick. Now the save holds it to the same
        fallback the collector applies.
        """

        async def save(name: str, metric_sql: str) -> int:
            resp = await client.post(
                _metrics_url(project["slug"]),
                json={
                    "kind": "sql",
                    "name": name,
                    "display_name": "Conventional",
                    "data_source_id": data_source["id"],
                    "interval": "1d",
                    "config": {"metric_sql": metric_sql, "time_column": "t"},
                },
            )
            return resp.status_code

        assert await save("unprojected_value", "SELECT 1 AS v, now() AS t") == 422
        assert await save("conventional_value", "SELECT 1 AS value, now() AS t") == 201


class TestFactMetricDimensionsAreCheckedAgainstTheFactTable:
    """an unknown dimension used to fail the whole metric."""

    async def test_create_refuses_a_platform_column_the_fact_table_does_not_have(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "orders_by_platform",
                "display_name": "Orders",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                # The form renders this as free text, not a picker.
                "platform_column": "platform_name",
            },
        )
        # Used to be 201. Then every tick raised inside the breakdown scan, and
        # the assembly loop re-raised it before writing anything — so the TOP
        # LINE went uncollected too, over one mistyped dimension.
        assert resp.status_code == 422, resp.text
        assert "platform_column" in resp.text

    async def test_create_refuses_a_breakdown_column_the_fact_table_does_not_have(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        resp = await client.post(
            _metrics_url(project["slug"]),
            json={
                "kind": "fact",
                "name": "orders_by_country",
                "display_name": "Orders",
                "composition": "single",
                "fact_table_id": fact_table["id"],
                "aggregation": "count",
                "interval": "1h",
                "breakdown_columns": ["country"],
            },
        )
        assert resp.status_code == 422, resp.text

    async def test_a_dimension_only_edit_is_checked_too(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        """The request shape most likely to carry the mistake, and the one that
        never reaches ``_apply_definition_update``."""
        slug = project["slug"]
        metric = await _create_fact_metric(client, slug, fact_table["id"], "orders_dimensions")
        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={"breakdown_columns": ["country"]},
        )
        assert resp.status_code == 422, resp.text

        stored = await _stored_metric(metric["id"])
        assert list(stored.breakdown_columns or []) == []

    async def test_a_real_fact_table_column_is_still_accepted(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        slug = project["slug"]
        metric = await _create_fact_metric(
            client,
            slug,
            fact_table["id"],
            "orders_by_user",
            breakdown_columns=["user_id"],
        )
        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={"breakdown_columns": ["user_id", "amount"]},
        )
        assert resp.status_code == 200, resp.text


class TestAPresentationPatchDoesNotAnswerForAStoredDimension:
    """tripl-0zpq.174, the other half — the re-check must not fence unrelated edits."""

    async def test_a_status_only_patch_lands_on_a_metric_whose_dimension_is_stale(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        slug = project["slug"]
        metric = await _create_fact_metric(
            client,
            slug,
            fact_table["id"],
            "stale_dimension",
            breakdown_columns=["user_id"],
        )
        await _drop_fact_table_column(fact_table["id"], "user_id")

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}", json={"status": "archived"}
        )
        # Used to be 422: the persisted-dimension re-check ran on EVERY PATCH that
        # carried no ``definition``, so a metric whose fact table lost the column
        # from its stored snapshot — a table saved without a preview, or one
        # re-introspected after a rename — could no longer be archived, recoloured
        # or renamed until the caller also repaired a dimension it had not sent.
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "archived"

    async def test_a_dimension_patch_on_the_same_row_is_still_checked(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        """The control: the gate narrows WHEN the check runs, never whether."""
        slug = project["slug"]
        metric = await _create_fact_metric(
            client,
            slug,
            fact_table["id"],
            "stale_dimension_edited",
            breakdown_columns=["user_id"],
        )
        await _drop_fact_table_column(fact_table["id"], "user_id")

        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={"breakdown_columns": ["user_id"]},
        )
        assert resp.status_code == 422, resp.text
        assert "breakdown column" in resp.text


class TestBreakdownColumnsAreDeduplicated:
    """one statement must not hit the same conflict key twice."""

    async def test_a_repeated_breakdown_column_is_stored_once_on_create(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        metric = await _create_fact_metric(
            client,
            project["slug"],
            fact_table["id"],
            "orders_repeated_breakdown",
            breakdown_columns=["user_id", "user_id"],
        )
        stored = await _stored_metric(metric["id"])
        # Used to store ["user_id", "user_id"]: the planner built one breakdown
        # plan per entry, assembly emitted every (bucket, value) row twice, and
        # the single INSERT ... ON CONFLICT DO UPDATE failed on Postgres with
        # "command cannot affect row a second time". SQLite applies the same
        # statement row by row, which is why the suite never saw it.
        assert list(stored.breakdown_columns or []) == ["user_id"]

    async def test_a_repeated_breakdown_column_is_stored_once_on_update(
        self, client: AsyncClient, project: dict, fact_table: dict
    ):
        slug = project["slug"]
        metric = await _create_fact_metric(client, slug, fact_table["id"], "orders_dedupe_update")
        resp = await client.patch(
            f"{_metrics_url(slug)}/{metric['id']}",
            json={"breakdown_columns": ["amount", "user_id", "amount"]},
        )
        assert resp.status_code == 200, resp.text

        stored = await _stored_metric(metric["id"])
        # First occurrence wins, so the catalog's column order is preserved.
        assert list(stored.breakdown_columns or []) == ["amount", "user_id"]


class TestDataSourceScopeIsOneSharedRule:
    """the save-time rule, extracted so the collector can reuse it.

    ``load_project_data_source`` is async and request-shaped; the sql collector is
    sync and holds a stored ``data_source_id`` that predates this rule entirely.
    ``data_source_out_of_project_scope`` is the ownership decision as a pure
    function of the three facts both paths have, so the two cannot drift — a
    predicate that means one thing on save and another on collect is exactly the
    shape of the defect this came from.

    The collector half landed with the extraction: ``metric_collect._collect_sql``
    calls ``metric_collect._reject_foreign_data_source`` on the stored
    ``data_source_id`` right after it resolves the row and before it builds the
    adapter, so a cross-project id written before the save-time check existed now
    fails its collection loudly instead of running under the foreign credential.

    WHAT IS STILL UNTESTED is that collector half: these cases cover the pure
    predicate and the save door that consults it, in an async API test. Pinning
    the worker's refusal needs the sync collector harness, which lives with the
    other ``metric_collect`` tests.
    """

    _ORG = uuid.uuid4()

    @classmethod
    def _source(cls, project_id: uuid.UUID | None) -> DataSource:
        return DataSource(
            name="scoped",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="db",
            project_id=project_id,
            organization_id=cls._ORG,
        )

    def test_a_source_owned_by_another_project_is_out_of_scope(self):
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        assert metric_definition_service.data_source_out_of_project_scope(
            self._source(theirs),
            project_id=mine,
            project_organization_id=self._ORG,
            scanning_project_ids=set(),
        )

    def test_a_source_owned_by_this_project_is_in_scope(self):
        mine = uuid.uuid4()
        assert not metric_definition_service.data_source_out_of_project_scope(
            self._source(mine),
            project_id=mine,
            project_organization_id=self._ORG,
            scanning_project_ids=set(),
        )

    def test_a_workspace_global_source_nobody_scans_is_shared(self):
        """NULL ``project_id`` means shared — that is the normal case, not a gap."""
        assert not metric_definition_service.data_source_out_of_project_scope(
            self._source(None),
            project_id=uuid.uuid4(),
            project_organization_id=self._ORG,
            scanning_project_ids=set(),
        )

    def test_a_workspace_global_source_only_another_project_scans_is_theirs(self):
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        assert metric_definition_service.data_source_out_of_project_scope(
            self._source(None),
            project_id=mine,
            project_organization_id=self._ORG,
            scanning_project_ids={theirs},
        )

    def test_another_organizations_source_is_out_of_scope(self):
        """Neither ownership nor sharing crosses an organization boundary."""
        mine = uuid.uuid4()
        assert metric_definition_service.data_source_out_of_project_scope(
            self._source(None),
            project_id=mine,
            project_organization_id=uuid.uuid4(),
            scanning_project_ids={mine},
        )

    def test_a_workspace_global_source_this_project_also_scans_is_ours(self):
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        assert not metric_definition_service.data_source_out_of_project_scope(
            self._source(None),
            project_id=mine,
            project_organization_id=self._ORG,
            scanning_project_ids={theirs, mine},
        )

    @staticmethod
    async def _save_against(
        client: AsyncClient, slug: str, data_source_id: str, name: str
    ) -> Response:
        return await client.post(
            _metrics_url(slug),
            json={
                "kind": "sql",
                "name": name,
                "display_name": "Borrowed",
                "data_source_id": data_source_id,
                "interval": "1d",
                "config": {"metric_sql": "SELECT 1 AS value, now() AS t", "time_column": "t"},
            },
        )

    async def test_the_save_path_returns_the_same_verdict_it_delegates(
        self, client: AsyncClient, project: dict, monkeypatch: pytest.MonkeyPatch
    ):
        """The predicate is not a parallel implementation: the router hits THIS one.

        A bare ``assert resp.status_code == 404`` would certify nothing here — the
        inline check this extraction replaced refused the identical case with the
        identical status. So the refusal is tied to the shared module's own
        sentence, and then the shared module's own predicate is REPLACED: a router
        that keeps refusing while the function it supposedly calls says "in scope"
        is a router carrying its own copy of the rule, which is the drift
        tripl-0zpq.347 is about.
        """
        neighbour = await client.post(
            "/api/v1/projects",
            json={"name": "Batch6 Scope", "slug": "batch6-scope", "description": ""},
        )
        assert neighbour.status_code == 201, neighbour.text
        created = await client.post(
            "/api/v1/data-sources",
            json={
                "name": "Scope CH",
                "db_type": "clickhouse",
                "host": "localhost",
                "port": 8123,
                "database_name": "scope_db",
            },
        )
        assert created.status_code == 201, created.text
        borrowed = created.json()
        await _bind_data_source(neighbour.json()["id"], borrowed["id"], "scope-binding")

        refused = await self._save_against(
            client, project["slug"], borrowed["id"], "scope_borrowed"
        )
        assert refused.status_code == 404, refused.text
        # The shared module's constant, not a string this test spells itself.
        assert refused.json()["detail"] == DATA_SOURCE_NOT_AVAILABLE

        monkeypatch.setattr(
            metric_definition_service,
            "data_source_out_of_project_scope",
            lambda *_args, **_kwargs: False,
        )
        allowed = await self._save_against(
            client, project["slug"], borrowed["id"], "scope_borrowed_stubbed"
        )
        # Same request, same borrowed source, one stubbed predicate: if this is
        # still 404 the save path is not asking it.
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["data_source_id"] == borrowed["id"]
