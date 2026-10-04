"""Regression tests for the metrics read-path leftovers of batch 6.

* the grid-population predicate lives once, in
  :mod:`tripl.metric_grid`, for the async read path and the sync detector.
* the breakdown read sums the same grid population as the
  series line, not every grid the metric was ever collected on.
* the open-anchor probe measures that population too.
* the catalog-metric series serves the sigma threshold the
  detector scores it with (project setting, narrowed by the metric's override).
* ``/events-metrics`` fills the ``sigma_threshold`` it serves.
* ``/events-metrics`` evaluates the tag / status filter on the
  caller's branch.
* the fact-table batch cache never serves one project's fact
  table to another project's metric.
* a metric create may omit ``order`` (or send null).
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.dialects import sqlite

from tripl.metric_grid import MetricGrid, grid_population_filter
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.domain_enums import MetricScopeType
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.services import metric_series_service
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_metric_series_api import (
    B0,
    B2,
    _create_sql_metric,
    _metrics_url,
    _seed_breakdowns,
    _seed_event_composition_metric,
    _seed_metric_values,
    _seed_scan_config,
)
from tripl.tests.test_metric_series_api import (
    data_source as data_source,  # noqa: F401  — pytest fixture, consumed by name
)
from tripl.tests.test_metric_series_api import (
    project as project,  # noqa: F401  — pytest fixture, consumed by name
)
from tripl.tests.test_metrics_api import _seed_event_metrics_at
from tripl.tests.test_plan_branches import _create_branch, _seed_plan
from tripl.worker.tasks._errors import ScanError
from tripl.worker.tasks.metrics import detect as metrics_detect
from tripl.worker.tasks.metrics.metric_collect import _FactBatchContext


def _sql(clause: object) -> str:
    return str(
        cast("Any", clause).compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


async def _set_project_sigma(project_id: str, sigma: float) -> None:
    """Settings -> Monitoring -> Sigma threshold, whether or not the row exists yet."""
    async with TestSessionLocal() as session:
        settings = await session.scalar(
            select(ProjectAnomalySettings).where(
                ProjectAnomalySettings.project_id == uuid.UUID(project_id)
            )
        )
        if settings is None:
            session.add(
                ProjectAnomalySettings(project_id=uuid.UUID(project_id), sigma_threshold=sigma)
            )
        else:
            settings.sigma_threshold = sigma
        await session.commit()


class TestSharedGridPopulation:
    def test_read_path_and_detector_delegate_to_one_predicate(self):
        """both halves compile to the shared predicate.

        Reverting the extraction removes ``metric_grid.grid_population_filter``
        and fails the import above; re-spelling either half so it drifts
        (e.g. dropping the interval) reddens the comparisons.
        """
        config_id = uuid.uuid4()
        shared = grid_population_filter(
            MetricValue.scan_config_id, interval="1h", scan_config_id=config_id
        )
        read = metric_series_service._grid_population_filter(
            interval="1h", scan_config_id=config_id
        )
        worker = metrics_detect._metric_grid_population(
            MetricGrid(
                metric_definition_id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                interval="1h",
                scan_config_id=config_id,
            )
        )
        assert _sql(read) == _sql(shared)
        assert _sql(worker) == _sql(shared)
        assert "'1h'" in _sql(shared)
        # sql/fact metrics and a vanished metric: the exact IS NULL branch.
        null_branch = _sql(
            grid_population_filter(MetricValue.scan_config_id, interval="1h", scan_config_id=None)
        )
        assert _sql(metrics_detect._metric_grid_population(None)) == null_branch
        assert "IS NULL" in null_branch


class TestBreakdownGridPopulation:
    async def test_breakdowns_exclude_the_retired_grid(
        self,
        client: AsyncClient,
        project: dict,  # noqa: F811
    ):
        """the Breakdowns tab sums the series line's population.

        The metric's newest value sits on a live 1h config, so its grid is 1h; a
        retired 1d config still holds an older breakdown row for the same
        segment and bucket. Reverting the grid filter in
        ``_load_breakdown_value_rows`` adds the retired 100.0 back in.
        """
        slug = project["slug"]
        metric_id = await _seed_event_composition_metric(project["id"], "grid-breakdown")
        async with TestSessionLocal() as session:
            await session.execute(
                update(MetricDefinition)
                .where(MetricDefinition.id == uuid.UUID(metric_id))
                .values(breakdown_columns=["country"])
            )
            await session.commit()
        retired = await _seed_scan_config(project["id"], interval="1d")
        live = await _seed_scan_config(project["id"], interval="1h")
        await _seed_metric_values(metric_id, [(B0, 50.0)], scan_config_id=retired)
        await _seed_metric_values(metric_id, [(B2, 4.0)], scan_config_id=live)
        await _seed_breakdowns(metric_id, [("country", "US", B0, 3.0)], scan_config_id=live)
        await _seed_breakdowns(metric_id, [("country", "US", B0, 100.0)], scan_config_id=retired)

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/breakdowns")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["interval"] == "1h"
        us = next(item for item in body["series"] if item["breakdown_value"] == "US")
        assert us["total_value"] == pytest.approx(3.0)


class TestOpenAnchorProbePopulation:
    async def test_probe_ignores_a_retired_grid_newer_bucket(
        self,
        project: dict,  # noqa: F811
    ):
        """the probe measures the plotted series only.

        The retired 1d config holds a value NEWER than anything on the live 1h
        grid. Widening the probe alone (dropping its grid filter) reports that
        retired bucket as the newest value, while the series read beside it
        never plots it.
        """
        metric_id = await _seed_event_composition_metric(project["id"], "grid-probe")
        live = await _seed_scan_config(project["id"], interval="1h")
        retired = await _seed_scan_config(project["id"], interval="1d")
        await _seed_metric_values(metric_id, [(B0, 1.0)], scan_config_id=live)
        await _seed_metric_values(metric_id, [(B2, 9.0)], scan_config_id=retired)

        async with TestSessionLocal() as session:
            newest = await metric_series_service._latest_metric_value_bucket(
                session,
                uuid.UUID(metric_id),
                since=B0 - timedelta(hours=1),
                time_to=None,
                interval="1h",
                scan_config_id=live,
            )

        assert newest is not None
        assert newest.replace(tzinfo=None) == B0.replace(tzinfo=None)


class TestMetricSeriesSigma:
    async def test_series_serves_the_project_sigma(
        self,
        client: AsyncClient,
        project: dict,  # noqa: F811
        data_source: dict,  # noqa: F811
    ):
        """the catalog chart gets the project's real multiplier."""
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "sigma-proj")
        await _set_project_sigma(project["id"], 6.0)

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")

        assert resp.status_code == 200, resp.text
        assert resp.json()["sigma_threshold"] == pytest.approx(6.0)

    async def test_series_honours_the_metric_scope_override(
        self,
        client: AsyncClient,
        project: dict,  # noqa: F811
        data_source: dict,  # noqa: F811
    ):
        """The override the false-positive ratchet stored for this metric wins."""
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "sigma-override")
        await _set_project_sigma(project["id"], 6.0)
        async with TestSessionLocal() as session:
            session.add(
                AnomalyScopeOverride(
                    project_id=uuid.UUID(project["id"]),
                    scan_config_id=None,
                    scope_type=MetricScopeType.metric.value,
                    scope_ref=metric["id"],
                    sigma_threshold=7.5,
                    min_expected_count=5,
                )
            )
            await session.commit()

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")

        assert resp.status_code == 200, resp.text
        assert resp.json()["sigma_threshold"] == pytest.approx(7.5)


class TestEventsMetricsSigma:
    async def test_events_total_serves_the_project_sigma(self, client: AsyncClient):
        """both returns fill the field instead of the 4.0 default."""
        slug = "leftovers-events-sigma"
        await _seed_plan(client, slug)
        main_event = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]
        await _set_project_sigma(main_event["project_id"], 6.0)

        empty = await client.get(f"/api/v1/projects/{slug}/events-metrics")
        assert empty.status_code == 200, empty.text
        assert empty.json()["data"] == []
        assert empty.json()["sigma_threshold"] == pytest.approx(6.0)

        await _seed_event_metrics_at(
            main_event["project_id"],
            main_event["id"],
            name="sigma scan",
            points=[(datetime(2026, 9, 1, 10, tzinfo=UTC), 4)],
        )
        resp = await client.get(f"/api/v1/projects/{slug}/events-metrics")
        assert resp.status_code == 200, resp.text
        assert [point["count"] for point in resp.json()["data"]] == [4]
        assert resp.json()["sigma_threshold"] == pytest.approx(6.0)


class TestEventsMetricsBranchFilter:
    async def test_tag_filter_reads_the_branch_rows(self, client: AsyncClient):
        """a tag added on the branch selects that event's volume.

        Main's copy carries no tag, so evaluating the filter against main's rows
        (what the endpoint did while it ignored ``?branch=``) charts nothing.
        """
        slug = "leftovers-branch-tag"
        await _seed_plan(client, slug)
        main_event = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]
        branch_id = await _create_branch(client, slug)
        await _seed_event_metrics_at(
            main_event["project_id"],
            main_event["id"],
            name="branch scan",
            points=[
                (datetime(2026, 9, 1, 10, tzinfo=UTC), 4),
                (datetime(2026, 9, 1, 11, tzinfo=UTC), 6),
            ],
        )
        branch_event = (
            await client.get(f"/api/v1/projects/{slug}/events", params={"branch": branch_id})
        ).json()["items"][0]
        assert branch_event["id"] != main_event["id"]
        tagged = await client.patch(
            f"/api/v1/projects/{slug}/events/{branch_event['id']}",
            params={"branch": branch_id},
            json={"tags": ["vip"]},
        )
        assert tagged.status_code == 200, tagged.text

        on_branch = await client.get(
            f"/api/v1/projects/{slug}/events-metrics",
            params={"tag": "vip", "branch": branch_id},
        )
        assert on_branch.status_code == 200, on_branch.text
        assert [point["count"] for point in on_branch.json()["data"]] == [4, 6]

        # Control: on main nothing carries the tag, so the chart stays empty.
        on_main = await client.get(f"/api/v1/projects/{slug}/events-metrics", params={"tag": "vip"})
        assert on_main.status_code == 200, on_main.text
        assert on_main.json()["data"] == []


class _FakeSession:
    def __init__(self, fact_table: object) -> None:
        self._fact_table = fact_table

    def get(self, _model: object, _ident: object) -> object:
        return self._fact_table


class TestFactBatchCacheScope:
    def test_cached_fact_table_is_not_served_to_another_project(self):
        """a cache hit re-applies the project scope.

        Project A's metric resolved the fact table first; project B's metric in
        the same batch asks for the same id. The adapter and the data-source
        verdict are pre-seeded for B so only the fact-table scope can refuse it
        — reverting the cache-hit check hands B project A's table.
        """
        project_a = uuid.uuid4()
        project_b = uuid.uuid4()
        data_source_id = uuid.uuid4()
        fact_table = SimpleNamespace(
            id=uuid.uuid4(), project_id=project_a, data_source_id=data_source_id
        )
        context = _FactBatchContext(session=cast("Any", _FakeSession(fact_table)))
        context.fact_tables[fact_table.id] = cast("Any", fact_table)
        context.adapters[data_source_id] = cast("Any", object())
        context.scoped.add((data_source_id, project_b))
        context.scoped.add((data_source_id, project_a))

        with pytest.raises(ScanError, match="does not belong"):
            context.resolve(fact_table.id, project_id=project_b)

        # The owning project still hits the cache.
        resolved, _adapter = context.resolve(fact_table.id, project_id=project_a)
        assert resolved is fact_table


class TestMetricCreateOrder:
    async def test_create_accepts_a_null_order_and_appends(
        self,
        client: AsyncClient,
        project: dict,  # noqa: F811
        data_source: dict,  # noqa: F811
    ):
        """``order`` is optional on create, and null means append.

        While the field was ``int = 0`` the contract forced callers to send it
        and a null was a 422.
        """
        slug = project["slug"]
        first = await _create_sql_metric(client, slug, data_source["id"], "order-first")
        second = await _create_sql_metric(
            client, slug, data_source["id"], "order-second", order=None
        )

        assert second["order"] > first["order"]
        async with TestSessionLocal() as session:
            stored = await session.get(MetricDefinition, uuid.UUID(second["id"]))
            assert stored is not None
            assert stored.order == second["order"]


def test_breakdown_model_carries_the_grid_column():
    """The shared predicate is applied to the breakdown table's own column."""
    clause = _sql(
        grid_population_filter(
            MetricValueBreakdown.scan_config_id, interval="1d", scan_config_id=uuid.uuid4()
        )
    )
    assert "metric_value_breakdowns.scan_config_id IN" in clause
