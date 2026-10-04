"""Batch 6, lane B: catalog-metric series and the metrics read path.

Regression tests for four findings in ``services/metric_series_service``:

* breakdowns zero-filled FRACTIONAL series because
  ``get_metric_breakdowns`` was the only caller that never passed
  ``count_shaped``.
* the version fold ADDED ratios together, so a ~30% metric
  plotted its "Other" line at 85%.
* the value read mixed every scan grid's ``MetricValue``
  rows, so a two-grid ``event_composition`` metric plotted the retired grid and
  invented a zero where the live one had data. The repair is a GRID POPULATION
  shared by the read and the detector (``_grid_population_filter`` /
  ``detect._metric_grid_population``): SUM every config on the resolved grid's
  interval, exclude every other. The first attempt narrowed to the single
  resolved config instead, which left the plotted line one ADDEND of the band
  drawn around it, so ``TestMetricGridIsolation`` pins both halves — too narrow
  and too wide each redden a different test in it.
* a stored non-finite value 500'd the whole series read on
  ``round()``.

and for four more in ``services/metrics_service``, the event-metrics read path:

* ``get_events_metrics`` filtered by the BRANCH copy's
  event-type id, which no metric row carries, so every tab's Dynamics card was
  empty on a working branch.
* the scan's ``platform_column`` never reached the
  Breakdowns tab's column list, making the stored per-platform series and its
  parity badges unreachable.
* ``get_platform_presence`` hydrated one row per (event,
  platform, bucket) of the scan's whole history to build a set.
* ``get_data_source_stats`` summed event-level AND
  type-level ``event_metrics`` rows, double-counting matched volume.

and for five that span both plus ``services/metrics_insights_service``:

* every read path fitted an STL/MSTL on the event loop,
  including a per-event batch that threw the result away.
* the served band was drawn from a
  sigma no API writes and, on catalog series, from the un-floored stddev.
* the version-activation rule documented for fractional
  metrics was not the rule the code applies.
* a non-UUID ``scope_ref`` 500'd the seasonality heatmap.

Fixtures and seeding helpers are reused from ``test_metric_series_api`` and
``test_metrics_api`` (the sibling suites for these endpoints) rather than
re-declared, the way the ``test_batch4_*`` modules reuse their neighbours'.
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl.core.analyzers.anomaly_detector import ForecastPoint, SeriesPoint
from tripl.models.domain_enums import MetricComposition, MetricKind
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.scan_config import ScanConfig
from tripl.services import metric_series_service, metrics_service
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_batch3_a1 import (
    _BASE,
    _HOUR,
    _add_metric,
    _add_sibling_config,
    _seed_project,
    _seed_values,
)
from tripl.tests.test_batch3_a1 import (
    sync_session_factory as sync_session_factory,  # noqa: F401  — pytest fixture, consumed by name
)
from tripl.tests.test_metric_series_api import (
    B0,
    B1,
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
from tripl.tests.test_metrics_api import (
    _seed_event_metrics_at,
    _seed_platform_scan,
    _setup_metrics_project,
)
from tripl.tests.test_plan_branches import _create_branch, _seed_plan
from tripl.tests.test_project_lookup_perf import captured_sql
from tripl.worker.tasks.metrics import detect as metrics_detect


def _naive(moment: datetime) -> datetime:
    """The UTC wall clock of ``moment``, without the offset.

    SQLite has no time zones and drops the offset on a
    ``DateTime(timezone=True)`` column, so the API echoes these buckets back
    naive — the same normalisation ``test_alerting`` and ``test_batch4_dst`` do
    before comparing a response timestamp with the one they seeded.
    """
    return moment.replace(tzinfo=None)


def _buckets(points: list[dict]) -> list[datetime]:
    return [_naive(datetime.fromisoformat(point["bucket"])) for point in points]


async def _seed_metric_scope_anomaly(
    *,
    metric_id: str,
    bucket: datetime,
    actual_count: float,
    expected_count: float,
) -> None:
    """A ``metric``-scope anomaly spelled exactly the way the detector writes one.

    ``scan_config_id=None`` is the whole point, not a shortcut:
    ``detect._replace_scope_anomalies`` stores catalog-metric rows with a NULL
    config because they describe the series SUMMED over the metric's grid
    population, never one source of it. So ``actual_count`` here is the total,
    and the value line the API draws underneath the band has to be read on the
    same population or the two disagree by an addend.

    The sibling helper in ``test_metric_series_api`` passes a real config id,
    which the read ignores (it matches on scope_type + scope_ref) but which
    would quietly make these tests seed a row the pipeline cannot produce.
    """
    async with TestSessionLocal() as session:
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=None,
                scope_type="metric",
                scope_ref=metric_id,
                event_id=None,
                event_type_id=None,
                bucket=bucket,
                actual_count=actual_count,
                expected_count=expected_count,
                stddev=1.0,
                effective_stddev=1.0,
                z_score=6.0,
                direction="spike",
            )
        )
        await session.commit()


async def _seed_count_shaped_metric(
    project_id: str,
    name: str,
    *,
    app_version_column: str | None = None,
    breakdown_columns: list[str] | None = None,
) -> str:
    """An ``event_composition`` ``single`` metric — count-shaped per value-kind.

    Its grid is inherited from the scan config its stored values carry, so one
    anchoring ``MetricValue`` row is seeded alongside it.
    """
    metric_id = await _seed_event_composition_metric(
        project_id, name, app_version_column=app_version_column
    )
    scan_config_id = await _seed_scan_config(
        project_id, interval="1h", app_version_column=app_version_column
    )
    await _seed_metric_values(metric_id, [(B0, 7.0)], scan_config_id=scan_config_id)
    if breakdown_columns is not None:
        async with TestSessionLocal() as session:
            await session.execute(
                update(MetricDefinition)
                .where(MetricDefinition.id == uuid.UUID(metric_id))
                .values(breakdown_columns=breakdown_columns)
            )
            await session.commit()
    return metric_id


async def _metric_value_scan_config_id(metric_id: str) -> uuid.UUID | None:
    """The scan config the count-shaped metric's anchoring value was stored under.

    Breakdown rows are read on the metric's grid population, so a
    breakdown seeded for a scan-bound metric must carry that metric's config,
    exactly as its value rows do.
    """
    async with TestSessionLocal() as session:
        return (
            await session.execute(
                select(MetricValue.scan_config_id)
                .where(MetricValue.metric_definition_id == uuid.UUID(metric_id))
                .limit(1)
            )
        ).scalar_one_or_none()


class TestBreakdownValueKind:
    async def test_fractional_breakdown_keeps_its_gaps_instead_of_dropping_to_zero(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """a sql metric is fractional, so a missing breakdown
        bucket is "no data", never 0.

        The ratio collector skips zero-denominator buckets outright, so filling
        them with 0.0 drew a conversion rate falling to 0% on segments that
        merely had no traffic. Reverting the ``count_shaped`` argument in
        ``get_metric_breakdowns`` puts a third point, B1 = 0.0, back between the
        two stored ones and reddens both assertions below.
        """
        slug = project["slug"]
        metric = await _create_sql_metric(
            client, slug, data_source["id"], "conv-rate", breakdown_columns=["country"]
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("country", "US", B0, 0.31),
                ("country", "US", B2, 0.29),
            ],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/breakdowns")

        assert resp.status_code == 200, resp.text
        us = next(item for item in resp.json()["series"] if item["breakdown_value"] == "US")
        assert [point["value"] for point in us["data"]] == [0.31, 0.29]
        assert _buckets(us["data"]) == [_naive(B0), _naive(B2)]
        assert _naive(B1) not in _buckets(us["data"])

    async def test_count_shaped_breakdown_still_zero_fills_its_gaps(
        self, client: AsyncClient, project: dict
    ):
        """The companion to the test above: the fix is value-kind aware, not a
        blanket "never densify".

        An ``event_composition`` ``single`` metric IS a count, where a missing
        bucket genuinely means zero happened. Hard-coding ``count_shaped=False``
        in ``get_metric_breakdowns`` would also close .113 and would redden this.
        """
        slug = project["slug"]
        metric_id = await _seed_count_shaped_metric(
            project["id"], "signups-by-country", breakdown_columns=["country"]
        )
        await _seed_breakdowns(
            metric_id,
            [
                ("country", "US", B0, 3.0),
                ("country", "US", B2, 5.0),
            ],
            scan_config_id=await _metric_value_scan_config_id(metric_id),
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/breakdowns")

        assert resp.status_code == 200, resp.text
        us = next(item for item in resp.json()["series"] if item["breakdown_value"] == "US")
        assert [point["value"] for point in us["data"]] == [3.0, 0.0, 5.0]
        assert _buckets(us["data"]) == [_naive(B0), _naive(B1), _naive(B2)]


class TestVersionFold:
    async def test_folding_versions_into_other_never_adds_fractional_levels(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """ "Other" must stay on the scale of the lines it folds.

        With ``app_version_keep_releases = 1`` only 2.0.0 keeps its own line and
        the two older releases fold into "Other". Summing them made a metric
        whose values never leave [0, 1] plot "Other" at 0.58 here (0.30 + 0.28),
        and far higher on a real 30-day window with a stored warehouse tail row.
        The fold now takes the mean of the folded levels, which is always inside
        ``[min, max]`` of them. Reverting ``_fold_bucket`` to the old ``+=``
        makes every assertion below fail.
        """
        slug = project["slug"]
        update_resp = await client.patch(
            f"/api/v1/projects/{slug}",
            json={"app_version_keep_releases": 1},
        )
        assert update_resp.status_code == 200, update_resp.text
        metric = await _create_sql_metric(
            client,
            slug,
            data_source["id"],
            "ratio-fold",
            app_version_column="app_version",
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("app_version", "1.0.0", B0, 0.30),
                ("app_version", "1.1.0", B0, 0.28),
                ("app_version", "2.0.0", B0, 0.31),
            ],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/versions")

        assert resp.status_code == 200, resp.text
        series_by_version = {item["version"]: item for item in resp.json()["series"]}
        assert set(series_by_version) == {"2.0.0", "Other"}
        # The kept release is alone under its display key: untouched by the fold.
        assert series_by_version["2.0.0"]["data"][0]["value"] == pytest.approx(0.31)
        other = series_by_version["Other"]
        assert other["data"][0]["value"] == pytest.approx(0.29)
        assert other["total_value"] == pytest.approx(0.29)
        # The property that matters on the chart, stated directly: a folded
        # fractional line can never climb above the versions it summarises.
        assert other["data"][0]["value"] <= 0.30

    async def test_folding_versions_into_other_still_sums_counts(
        self, client: AsyncClient, project: dict
    ):
        """The companion to the test above: counts DO add.

        An ``event_composition`` ``single`` metric is count-shaped, so folding
        two retired releases' volumes into "Other" must remain a sum. A fix that
        averaged unconditionally would close .114 and redden this.
        """
        slug = project["slug"]
        update_resp = await client.patch(
            f"/api/v1/projects/{slug}",
            json={"app_version_keep_releases": 1},
        )
        assert update_resp.status_code == 200, update_resp.text
        metric_id = await _seed_count_shaped_metric(
            project["id"], "count-fold", app_version_column="app_version"
        )
        await _seed_breakdowns(
            metric_id,
            [
                ("app_version", "1.0.0", B0, 2.0),
                ("app_version", "1.1.0", B0, 3.0),
                ("app_version", "2.0.0", B0, 4.0),
            ],
            scan_config_id=await _metric_value_scan_config_id(metric_id),
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/versions")

        assert resp.status_code == 200, resp.text
        series_by_version = {item["version"]: item for item in resp.json()["series"]}
        assert set(series_by_version) == {"2.0.0", "Other"}
        assert series_by_version["Other"]["data"][0]["value"] == pytest.approx(5.0)


class TestMetricGridIsolation:
    async def test_series_plots_only_the_resolved_grids_values(
        self, client: AsyncClient, project: dict
    ):
        """two scan grids, one chart.

        ``_collect_event_composition`` writes a ``MetricValue`` series per source
        scan, and ``MetricValue``'s unique key is per ``(metric, scan_config,
        bucket)``. Reading them unfiltered anchored ``expand_series`` on the
        OLDEST grid's first bucket: with a retired 1h grid at B0 = 10.0 and the
        live 1d grid at B2 = 4.0, the chart showed ``[B0: 10.0, B0+1d: 0.0]`` —
        the live value dropped and a zero invented in a bucket nothing ever
        collected. This is the TOO-WIDE half of the population rule: dropping
        the interval predicate from ``_grid_population_filter`` restores exactly
        that — the retired grid's B0 re-enters, anchors the 1d densification on
        it, and the served data becomes ``[10.0, 0.0]`` — so both data
        assertions below redden. (Summing per bucket does not rescue it: these
        two rows sit in different buckets, and adding an hourly count to a daily
        one is the same category error in the buckets where they do meet.)
        """
        slug = project["slug"]
        metric_id = await _seed_event_composition_metric(project["id"], "two-grids")
        old_config = await _seed_scan_config(project["id"], interval="1h")
        new_config = await _seed_scan_config(project["id"], interval="1d")
        await _seed_metric_values(metric_id, [(B0, 10.0)], scan_config_id=old_config)
        await _seed_metric_values(metric_id, [(B2, 4.0)], scan_config_id=new_config)

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/series")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The newest stored bucket picks the grid (tripl.metric_grid).
        assert body["scan_config_id"] == str(new_config)
        assert body["interval"] == "1d"
        assert [point["value"] for point in body["data"]] == [4.0]
        assert _buckets(body["data"]) == [_naive(B2)]

    async def test_standalone_metric_series_is_unaffected_by_the_grid_filter(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """A ``sql`` metric stores every row with a NULL ``scan_config_id``, so
        the IS NULL branch of the grid filter is exact rather than merely
        narrower — it must not hide the metric's own values."""
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "standalone-grid")
        await _seed_metric_values(metric["id"], [(B0, 10.0), (B2, 4.0)])

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")

        assert resp.status_code == 200, resp.text
        assert [point["value"] for point in resp.json()["data"]] == [10.0, 4.0]

    async def test_two_live_configs_on_one_grid_are_summed_into_one_line(
        self, client: AsyncClient, project: dict
    ):
        """Second half: one chart must describe ONE population.

        Two LIVE scans on the same interval collecting one event type is the
        ordinary shape — an iOS scan and an Android scan feeding one checkout
        event — and ``_collect_event_composition`` writes a ``MetricValue`` row
        set per source, so the metric's total is their SUM. That is the series
        the detector scores, and the ``MetricAnomaly`` seeded below carries a
        NULL ``scan_config_id`` precisely because it describes that total: there
        is no narrower population its ``expected_count``/``stddev`` band could
        belong to.

        This is the TOO-NARROW half of the population rule, and it is the defect
        the first repair of .115 introduced. Putting ``scan_config_id == X``
        back in ``_grid_population_filter`` reddens two assertions here:
        ``metric_grid_stmt`` resolves this metric to ``android`` (it holds the
        newest bucket, B2), so B0 plots 5.0 under a band computed for 8.0, and
        B1 — a bucket only ``ios`` collected — is zero-filled by
        ``expand_series`` right on top of a real stored value. The served data
        becomes ``[5.0, 0.0, 7.0]``.
        """
        slug = project["slug"]
        metric_id = await _seed_event_composition_metric(project["id"], "two-live-sources")
        ios = await _seed_scan_config(project["id"], interval="1h")
        android = await _seed_scan_config(project["id"], interval="1h")
        await _seed_metric_values(metric_id, [(B0, 3.0), (B1, 3.0)], scan_config_id=ios)
        await _seed_metric_values(metric_id, [(B0, 5.0), (B2, 7.0)], scan_config_id=android)
        await _seed_metric_scope_anomaly(
            metric_id=metric_id, bucket=B0, actual_count=8.0, expected_count=2.0
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/series")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The grid still RESOLVES to one config — that is what the UI is told,
        # and B2 makes it android — but the grid selects an interval, not a row
        # set, so both 1h sources are plotted.
        assert body["scan_config_id"] == str(android)
        assert body["interval"] == "1h"
        assert [point["value"] for point in body["data"]] == [8.0, 3.0, 7.0]
        assert _buckets(body["data"]) == [_naive(B0), _naive(B1), _naive(B2)]
        # The line and the band are now the same population: the plotted value
        # at the flagged bucket IS the actual the detector scored, so the dot
        # lands exactly where the stored z-score says it should — 6 sigma above
        # ``expected_count``. Under a single-config read it plots at 5.0, which
        # is INSIDE the band that flagged it, and that is the "outside the band
        # = flagged" contract breaking on the same page.
        flagged = next(point for point in body["data"] if point["is_anomaly"])
        assert flagged["value"] == 8.0
        assert flagged["value"] - flagged["expected_count"] == 6.0 * flagged["stddev"]

    async def test_the_grid_tie_break_cannot_change_the_plotted_values(
        self, client: AsyncClient, project: dict
    ):
        """The mirror of the test above, with the tie broken the other way.

        ``metric_grid_stmt`` windows on ``ORDER BY bucket DESC`` with no
        secondary key, so which of two equally-current configs it names is
        undefined. Here ``ios`` holds the newest bucket and wins, where above
        ``android`` did — and the chart is byte-identical, because the values no
        longer depend on the winner at all.

        Without this mirror, the previous test could be satisfied by a
        single-config filter that happened to pick the config holding the larger
        value. Restoring ``scan_config_id == X`` reddens the data assertion here
        too, and in the opposite direction: B0 plots ios's 3.0 instead of 8.0.
        """
        slug = project["slug"]
        metric_id = await _seed_event_composition_metric(project["id"], "tie-broken-the-other-way")
        ios = await _seed_scan_config(project["id"], interval="1h")
        android = await _seed_scan_config(project["id"], interval="1h")
        await _seed_metric_values(metric_id, [(B0, 3.0), (B1, 3.0), (B2, 7.0)], scan_config_id=ios)
        await _seed_metric_values(metric_id, [(B0, 5.0)], scan_config_id=android)

        resp = await client.get(f"{_metrics_url(slug)}/{metric_id}/series")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["scan_config_id"] == str(ios)
        assert [point["value"] for point in body["data"]] == [8.0, 3.0, 7.0]
        assert _buckets(body["data"]) == [_naive(B0), _naive(B1), _naive(B2)]

    def test_the_detector_scores_the_population_the_chart_plots(
        self, sync_session_factory: sessionmaker[Session]
    ) -> None:
        """The worker half of the same rule, which is what makes the chart honest.

        A ``MetricAnomaly`` says nothing about which configs it was scored from,
        so the read can only be right if the detector's population is the read's
        population. ``detect._metric_grid_population`` is the sync mirror of
        ``metric_series_service._grid_population_filter`` and this pins them to
        the same answer on one seeded shape: two live 1d sources plus a retired
        1h grid sitting in the same bucket.

        Both halves redden:

        * drop the population predicate from ``_load_metric_value_points`` (what
          it did before this repair) and the retired grid's 100.0 is added to the
          daily bucket — ``[110.0, 5.0]``;
        * narrow it to the grid's own ``scan_config_id`` (the direction
          first proposed) and the sibling daily source drops
          out — ``[3.0, 5.0]``.

        ``_metric_source_config_ids`` is asserted alongside because it is the
        coupled function: it feeds the coverage UNION, and coverage that
        describes a different population than the series either vouches for
        buckets that are no longer in it (the retired grid) or drops buckets only
        the sibling contributed — ``expand_series`` EXCLUDES an uncovered bucket
        rather than zero-filling it.

        ``grid`` is deliberately not passed to ``_load_metric_value_points``:
        the default has to resolve the same population, because
        ``test_demo_metric_collection`` calls it without one.
        """
        window = (_BASE - _HOUR, _BASE + _HOUR * 10)
        with sync_session_factory() as session:
            running = _seed_project(session)
            daily = _add_sibling_config(session, running, interval="1d")
            daily_sibling = _add_sibling_config(session, running, interval="1d")
            retired_hourly = _add_sibling_config(session, running)
            metric = _add_metric(
                session,
                running,
                kind=MetricKind.event_composition,
                composition=MetricComposition.single,
                name="checkouts",
                interval=None,
            )
            # Hour 2 is the newest stored bucket, so the grid resolves to
            # ``daily`` — interval 1d, which is what selects the population.
            _seed_values(session, metric, {0: 3.0, 2: 5.0}, scan_config_id=daily.id)
            _seed_values(session, metric, {0: 7.0}, scan_config_id=daily_sibling.id)
            _seed_values(session, metric, {0: 100.0}, scan_config_id=retired_hourly.id)

            points = metrics_detect._load_metric_value_points(
                session,
                metric_definition_id=metric.id,
                history_from=window[0],
                time_to=window[1],
            )
            source_ids = metrics_detect._metric_source_config_ids(
                session,
                metric_definition_id=metric.id,
                grid=metrics_detect._resolve_metric_grid(session, metric),
                history_from=window[0],
                time_to=window[1],
            )

        assert [point.count for point in points] == [10.0, 5.0]
        assert source_ids == sorted([daily.id, daily_sibling.id])


class TestNonFiniteValues:
    async def test_series_survives_a_stored_non_finite_value(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """one poisoned bucket must not 500 the metric page.

        ClickHouse returns ``inf`` for ``x/0`` in a user-written sql metric, and
        neither ``_coerce_value`` nor ``_build_metric_value_rows`` checks
        finiteness, so the value reaches the plain Float column verbatim. Before
        the fix ``_forecast_from_series`` called ``round()`` on it and the
        request died with ``OverflowError`` — a 500 on every visit until
        retention dropped the row. Reverting the ``isfinite`` filter in
        ``_densify_value_rows`` raises that error out of this request again, so
        the test never reaches its assertions.

        ``inf`` rather than ``nan`` because SQLite silently rewrites a bound NaN
        to NULL; both come back from Postgres, and both crashed ``round()``.
        """
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "unguarded-divide")
        await _seed_metric_values(
            metric["id"],
            [(B0, 1.0), (B1, float("inf")), (B2, 3.0)],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The poisoned bucket is treated as absent — and this metric is
        # fractional, so an absent bucket is a null break, not a zero.
        assert [point["value"] for point in body["data"]] == [1.0, 3.0]
        assert _buckets(body["data"]) == [_naive(B0), _naive(B2)]


# --------------------------------------------------------------- events read
#
# The four below live one layer out from the catalog-metric series above, in
# ``services/metrics_service``: the Events page's Dynamics card, the Breakdowns
# tab, the Scan detail presence matrix and the owner-only data-source stats.


class TestBranchEventsMetrics:
    async def test_a_branch_tab_charts_the_volume_its_main_twin_collected(
        self, client: AsyncClient
    ):
        """the Dynamics card must not go blank on a branch.

        A working branch deep-copies every EventType under a NEW uuid, and the
        Events page on a branch sends that copy's id as ``event_type_id``. No
        ``event_metrics`` row ever references it — scans only ever see main — so
        the filter matched nothing, ``_resolve_events_metrics_scan_config``
        returned None and every tab rendered "No recent volume to chart" beside
        row sparklines that DID show traffic. Reverting
        ``_main_branch_event_type_id`` in ``get_events_metrics`` empties ``data``
        here and reddens the count assertion.
        """
        slug = "batch6-branch-tab"
        await _seed_plan(client, slug)
        main_event = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]
        # Branch first: what matters is the traffic that lands on main after.
        branch_id = await _create_branch(client, slug)
        await _seed_event_metrics_at(
            main_event["project_id"],
            main_event["id"],
            name="twin scan",
            points=[
                (datetime(2026, 9, 1, 10, tzinfo=UTC), 4),
                (datetime(2026, 9, 1, 11, tzinfo=UTC), 6),
            ],
        )
        main_type = next(
            et
            for et in (await client.get(f"/api/v1/projects/{slug}/event-types")).json()
            if et["name"] == "track"
        )
        branch_type = next(
            et
            for et in (
                await client.get(f"/api/v1/projects/{slug}/event-types?branch={branch_id}")
            ).json()
            if et["name"] == "track"
        )
        assert branch_type["id"] != main_type["id"], "the deep copy must own a fresh id"

        resp = await client.get(
            f"/api/v1/projects/{slug}/events-metrics",
            params={"event_type_id": branch_type["id"]},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [point["count"] for point in body["data"]] == [4, 6]
        assert body["scan_config_name"] == "twin scan"
        # The same tab on main is the control: the branch reads THROUGH to it,
        # it does not get a series of its own.
        on_main = await client.get(
            f"/api/v1/projects/{slug}/events-metrics",
            params={"event_type_id": main_type["id"]},
        )
        assert on_main.json()["data"] == body["data"]

    async def test_an_event_type_that_only_the_branch_has_still_charts_nothing(
        self, client: AsyncClient
    ):
        """The companion: the fallback must stay honest.

        A type authored ON the branch has no twin on main and genuinely has no
        collected volume, so the card is legitimately empty. A "fix" that
        ignored ``event_type_id`` whenever it failed to resolve would close
        .111 by charting the project total under this tab, and would redden
        this.
        """
        slug = "batch6-branch-new-type"
        await _seed_plan(client, slug)
        main_event = (await client.get(f"/api/v1/projects/{slug}/events")).json()["items"][0]
        branch_id = await _create_branch(client, slug)
        await _seed_event_metrics_at(
            main_event["project_id"],
            main_event["id"],
            name="twin scan",
            points=[(datetime(2026, 9, 1, 10, tzinfo=UTC), 4)],
        )
        branch_only = await client.post(
            f"/api/v1/projects/{slug}/event-types?branch={branch_id}",
            json={"name": "screen", "display_name": "Screen"},
        )
        assert branch_only.status_code == 201, branch_only.text

        resp = await client.get(
            f"/api/v1/projects/{slug}/events-metrics",
            params={"event_type_id": branch_only.json()["id"]},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == []


class TestPlatformBreakdownColumn:
    async def test_the_scans_platform_column_is_offered_on_the_breakdowns_tab(
        self, client: AsyncClient
    ):
        """the platform series must be selectable.

        ``platform_column`` is collected as a scan-level breakdown by the
        collector itself, and ``ScanConfigCreate`` REFUSES to let anyone list it
        in ``metric_breakdown_columns``. So the documented setup below — a scan
        with ``platform_column='platform'`` and nobody naming it anywhere else —
        produced ``columns == []`` ("No breakdown groups yet") over rows that
        were sitting right there, and ``?column=platform`` 400'd. Dropping
        ``config.platform_column`` back out of the column list in
        ``get_event_metric_breakdowns`` reddens every assertion below.
        """
        slug = "batch6-platform-column"
        scan_config_id, events = await _seed_platform_scan(client, slug, platform_column="platform")
        event_id = events["checkout"]
        bucket = datetime(2026, 1, 1, 10, tzinfo=UTC)
        async with TestSessionLocal() as session:
            # One event-scope metric row so the scope resolver finds this scan.
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=uuid.UUID(scan_config_id),
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    count=80,
                )
            )
            for platform, count in (("ios", 50), ("android", 30)):
                session.add(
                    EventMetricBreakdown(
                        id=uuid.uuid4(),
                        scan_config_id=uuid.UUID(scan_config_id),
                        event_id=uuid.UUID(event_id),
                        event_type_id=None,
                        bucket=bucket,
                        breakdown_column="platform",
                        breakdown_value=platform,
                        is_other=False,
                        count=count,
                    )
                )
            # The parity anomaly the docs promise as a before/after share badge.
            session.add(
                MetricBreakdownAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=uuid.UUID(scan_config_id),
                    scope_type="event",
                    scope_ref=event_id,
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=bucket,
                    breakdown_column="platform",
                    breakdown_value="android",
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

        resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/metrics/breakdowns")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == ["platform"]
        assert body["selected_column"] == "platform"
        by_value = {item["breakdown_value"]: item for item in body["series"]}
        assert by_value["ios"]["total_count"] == 50
        assert by_value["android"]["total_count"] == 30
        assert by_value["android"]["parity_anomalies"][0]["actual_share"] == pytest.approx(0.25)
        # …and asking for it explicitly is no longer a 400.
        selected = await client.get(
            f"/api/v1/projects/{slug}/events/{event_id}/metrics/breakdowns?column=platform"
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["selected_column"] == "platform"

    async def test_an_unconfigured_breakdown_column_is_still_rejected(self, client: AsyncClient):
        """The companion: appending the platform column must not open the gate.

        Any column the scan does not collect still 400s — the guard is a
        whitelist, not a formality.
        """
        slug = "batch6-platform-column-guard"
        scan_config_id, events = await _seed_platform_scan(client, slug, platform_column="platform")
        event_id = events["checkout"]
        async with TestSessionLocal() as session:
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=uuid.UUID(scan_config_id),
                    event_id=uuid.UUID(event_id),
                    event_type_id=None,
                    bucket=datetime(2026, 1, 1, 10, tzinfo=UTC),
                    count=80,
                )
            )
            await session.commit()

        resp = await client.get(
            f"/api/v1/projects/{slug}/events/{event_id}/metrics/breakdowns?column=country"
        )

        assert resp.status_code == 400, resp.text


class TestPlatformPresenceCost:
    async def test_presence_asks_the_database_for_distinct_pairs_not_every_bucket(
        self, client: AsyncClient
    ):
        """the answer is a set, so the query must return one.

        Breakdown rows are keyed per bucket and have no retention outside demo
        projects, so the undeduplicated select hydrated one row per (event,
        platform, bucket) since the scan began — linear in scan age for an
        answer bounded by events x platforms. Dropping ``.distinct()`` from
        ``get_platform_presence`` leaves the matrix correct but reddens the
        statement assertion below, which is the whole point of the fix.
        """
        slug = "batch6-presence-cost"
        scan_config_id, events = await _seed_platform_scan(client, slug, platform_column="platform")
        first = datetime(2026, 1, 1, 10, tzinfo=UTC)
        async with TestSessionLocal() as session:
            # 12 buckets x 2 platforms x 2 events = 48 stored rows behind an
            # answer that is 2 events x 2 platforms.
            for hour in range(12):
                for ev_name in ("checkout", "signup"):
                    for platform in ("ios", "android"):
                        session.add(
                            EventMetricBreakdown(
                                id=uuid.uuid4(),
                                scan_config_id=uuid.UUID(scan_config_id),
                                event_id=uuid.UUID(events[ev_name]),
                                event_type_id=None,
                                bucket=first + timedelta(hours=hour),
                                breakdown_column="platform",
                                breakdown_value=platform,
                                is_other=False,
                                count=5,
                            )
                        )
            await session.commit()

        with captured_sql() as statements:
            resp = await client.get(
                f"/api/v1/projects/{slug}/scans/{scan_config_id}/platform-presence"
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["platforms"] == ["android", "ios"]
        assert {item["event_name"]: item["present_platforms"] for item in body["items"]} == {
            "checkout": ["android", "ios"],
            "signup": ["android", "ios"],
        }
        presence_selects = [
            sql
            for sql in statements
            if "event_metric_breakdowns" in sql and " JOIN events " in sql.replace("\n", " ")
        ]
        assert presence_selects, statements
        assert all("SELECT DISTINCT" in sql for sql in presence_selects), presence_selects


class TestDataSourceStatsRowKinds:
    async def test_volume_counts_each_warehouse_row_once(self, client: AsyncClient):
        """event-level and type-level rollups overlap.

        Every collection chunk writes an event-level ``event_metrics`` row for
        each matched plan event AND a type-level row that re-counts the same
        warehouse rows, so summing the table flat reported matched volume twice.
        Seeded the way a real chunk writes: 100 + 50 matched plus 20 that
        matched no plan event, which is 170 rows of traffic and 320 if you add
        both kinds. Dropping the ``type_level`` filter from
        ``get_data_source_stats`` puts 320 back and reddens the volume and
        throughput assertions.
        """
        ctx = await _setup_metrics_project(client, slug="batch6-ds-stats")

        async def _event(name: str) -> str:
            resp = await client.post(
                "/api/v1/projects/batch6-ds-stats/events",
                json={
                    "event_type_id": ctx["page_type_id"],
                    "name": name,
                    "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
                },
            )
            assert resp.status_code == 201, resp.text
            return resp.json()["id"]

        ev1, ev2 = await _event("Ev1"), await _event("Ev2")
        recent = datetime.now(UTC) - timedelta(hours=1)
        scan_config_id = await _seed_event_metrics_at(
            ctx["project_id"], ev1, name="DS stats scan", points=[(recent, 100)]
        )
        async with TestSessionLocal() as session:
            config = await session.get(ScanConfig, scan_config_id)
            assert config is not None
            data_source_id = config.data_source_id
            session.add_all(
                [
                    EventMetric(
                        id=uuid.uuid4(),
                        scan_config_id=scan_config_id,
                        event_id=uuid.UUID(ev2),
                        event_type_id=None,
                        bucket=recent,
                        count=50,
                    ),
                    EventMetric(
                        id=uuid.uuid4(),
                        scan_config_id=scan_config_id,
                        event_id=None,
                        event_type_id=uuid.UUID(ctx["page_type_id"]),
                        bucket=recent,
                        count=170,
                    ),
                ]
            )
            await session.commit()

        resp = await client.get(f"/api/v1/data-sources/{data_source_id}/stats?window_hours=48")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["volume_window"] == 170
        assert [point["count"] for point in body["throughput"]] == [170]
        # events_tracked still reads the event-level rows: only they carry an id.
        assert body["events_tracked"] == 2


# ------------------------------------------------------------ forecast cost
#
# The four below are about WHEN the read path is allowed to fit an STL/MSTL
# model, not about what the model says. They spy on ``forecast_next_buckets``
# rather than reading numbers out of it, because the finding is that the fit
# ran at all.


class _ForecastSpy:
    """Stands in for ``forecast_next_buckets`` and records each fit requested.

    Returning ``[]`` is what the real function returns for a series too short to
    model, so a caller that still asks for a fit gets a well-formed answer and
    the request under test stays a 200 — the assertion is on ``calls``, not on a
    crash.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.thread_idents: list[int] = []

    def __call__(
        self,
        points: list[SeriesPoint],
        *,
        interval: timedelta,
        horizon: int = 1,
    ) -> list[ForecastPoint]:
        self.calls += 1
        self.thread_idents.append(threading.get_ident())
        return []


@pytest.fixture
def forecast_spy(monkeypatch: pytest.MonkeyPatch) -> _ForecastSpy:
    """Patch ``forecast_next_buckets`` in BOTH read services.

    By module attribute, because each service does ``from
    ...anomaly_detector import forecast_next_buckets`` at import time and so
    holds its own reference — patching the detector module would not be seen.
    """
    spy = _ForecastSpy()
    monkeypatch.setattr(metrics_service, "forecast_next_buckets", spy)
    monkeypatch.setattr(metric_series_service, "forecast_next_buckets", spy)
    return spy


class TestForecastCost:
    async def test_the_window_batch_skips_the_fit_the_drilldown_still_runs(
        self, client: AsyncClient, forecast_spy: _ForecastSpy
    ):
        """the batch copies ``.data`` out and drops the rest.

        ``get_events_window_metrics`` built a whole drilldown response per
        requested event — the Events page asks for up to 100 at a time — and
        every one of them fitted a fresh robust STL/MSTL whose ``forecast`` was
        thrown away on the very next line, where only ``.data`` is read.

        The second half of the test is what makes the first half mean something:
        the same event, fetched through the drilldown that DOES render a
        forecast, still fits exactly one. Dropping ``with_forecast=False`` from
        the batch call takes the first assertion from 0 to 1.
        """
        slug = "batch6-window-forecast"
        ctx = await _setup_metrics_project(client, slug=slug)
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": "Checkout",
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        base = datetime(2026, 1, 1, 10, tzinfo=UTC)
        await _seed_event_metrics_at(
            ctx["project_id"],
            event_id,
            name="Window forecast scan",
            points=[(base, 10), (base + timedelta(hours=1), 12), (base + timedelta(hours=2), 11)],
        )

        batch = await client.post(
            f"/api/v1/projects/{slug}/events/window-metrics",
            json={
                "event_ids": [event_id],
                "time_from": "2026-01-01T09:00:00Z",
                "time_to": "2026-01-01T13:00:00Z",
            },
        )

        assert batch.status_code == 200, batch.text
        # The sparkline the batch exists for is unchanged...
        assert [point["count"] for point in batch.json()[0]["data"]] == [10, 12, 11]
        # ...and no model was fitted to produce it.
        assert forecast_spy.calls == 0

        drilldown = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/metrics")

        assert drilldown.status_code == 200, drilldown.text
        assert forecast_spy.calls == 1

    async def test_the_fit_runs_off_the_event_loop(
        self, client: AsyncClient, forecast_spy: _ForecastSpy
    ):
        """an STL/MSTL fit must not park the uvicorn worker.

        The fit is pure CPU inside numpy/statsmodels. Run inline on the async
        request path it blocks the loop for its whole duration — measured on
        this hardware at 1.75 s for 720 points — so every unrelated request
        multiplexed onto that worker waits behind one chart's dashed tail.

        The thread identity is compared against the one this coroutine is
        running on rather than against ``main_thread()``, so the test does not
        depend on where the event loop happens to live. Replacing
        ``asyncio.to_thread`` with a direct call makes the two idents equal.
        """
        slug = "batch6-forecast-thread"
        ctx = await _setup_metrics_project(client, slug=slug)
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": "Signup",
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        base = datetime(2026, 1, 1, 10, tzinfo=UTC)
        await _seed_event_metrics_at(
            ctx["project_id"],
            event_id,
            name="Thread scan",
            points=[(base, 10), (base + timedelta(hours=1), 12)],
        )

        loop_thread = threading.get_ident()
        resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/metrics")

        assert resp.status_code == 200, resp.text
        assert forecast_spy.calls == 1
        assert forecast_spy.thread_idents[0] != loop_thread

    async def test_a_series_wider_than_the_cap_is_not_fitted_at_all(
        self, client: AsyncClient, forecast_spy: _ForecastSpy
    ):
        """long ranges pay the most and are the ones that hide it.

        A 30d or 90d drilldown on an hourly scan is 720 or 2160 densified
        points, which is an MSTL over periods 24 and 168 — 1.75 s and 5.3 s
        here. At the DEFAULT granularity the chart rolls those ranges up to
        day/week buckets and drops the dashed tail, because one native bucket is
        not a forecast for a whole display bucket, so the wide fit is bought and
        never shown. A reader who manually pins the native granularity does keep
        the tail rendered and does lose it to this cap; that trade is written out
        where ``_FORECAST_MAX_POINTS`` is defined.

        Two rows 401 hours apart densify to 402 grid points, one over
        ``_FORECAST_MAX_POINTS``; the neighbouring three-point series in
        ``test_the_window_batch_skips_the_fit_the_drilldown_still_runs`` shows a
        narrow range is still fitted. Removing the length test from
        ``_forecast_off_event_loop`` (or raising the cap past 402) makes the spy
        count below 1.
        """
        slug = "batch6-forecast-cap"
        ctx = await _setup_metrics_project(client, slug=slug)
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": "Wide",
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        last = datetime(2026, 1, 1, 10, tzinfo=UTC)
        await _seed_event_metrics_at(
            ctx["project_id"],
            event_id,
            name="Wide scan",
            points=[(last - timedelta(hours=401), 10), (last, 12)],
        )

        resp = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/metrics")

        assert resp.status_code == 200, resp.text
        assert len(resp.json()["data"]) == 402
        assert forecast_spy.calls == 0

    async def test_a_catalog_series_obeys_the_same_cap(
        self, client: AsyncClient, project: dict, forecast_spy: _ForecastSpy
    ):
        """the metric detail page pays it too.

        ``get_metric_series`` is the endpoint whose forecast the UI provably
        never draws — ``adaptMetricSeries`` replaces it with ``[]`` because a
        dashed tail trending to 0 is misleading on a ratio — and an hourly
        catalog metric at the default 30d range is a 720-point MSTL. The cap
        removes exactly that, while a short series is still modelled.
        """
        slug, project_id = project["slug"], project["id"]
        scan_config_id = await _seed_scan_config(project_id, interval="1h")

        narrow = await _seed_event_composition_metric(project_id, "narrow-series")
        await _seed_metric_values(
            narrow, [(B0, 7.0), (B1, 8.0), (B2, 9.0)], scan_config_id=scan_config_id
        )
        wide = await _seed_event_composition_metric(project_id, "wide-series")
        await _seed_metric_values(
            wide,
            [(B0 - timedelta(hours=401), 7.0), (B0, 9.0)],
            scan_config_id=scan_config_id,
        )

        narrow_resp = await client.get(f"{_metrics_url(slug)}/{narrow}/series")

        assert narrow_resp.status_code == 200, narrow_resp.text
        assert forecast_spy.calls == 1

        wide_resp = await client.get(f"{_metrics_url(slug)}/{wide}/series")

        assert wide_resp.status_code == 200, wide_resp.text
        assert len(wide_resp.json()["data"]) == 402
        # Still 1: the wide read added no fit of its own.
        assert forecast_spy.calls == 1


# ------------------------------------------------------- the served band
#
# "Outside the band = flagged" is the chart's contract. It holds only if the
# served ``sigma_threshold`` and ``stddev`` are the two numbers the DETECTOR
# actually used.


async def _set_dead_scan_sigma(scan_config_id: uuid.UUID, value: float) -> None:
    """Put a value in ``ScanConfig.sigma_threshold`` that nothing may serve.

    No API writes this column — the scan schemas do not expose it and settings
    updates never sync it — so any response echoing it back is reading a column
    the operator cannot reach.
    """
    async with TestSessionLocal() as session:
        await session.execute(
            update(ScanConfig).where(ScanConfig.id == scan_config_id).values(sigma_threshold=value)
        )
        await session.commit()


class TestServedSigmaThreshold:
    async def test_the_band_multiplier_follows_the_project_setting(self, client: AsyncClient):
        """Charts drew a band nobody configured.

        The detector scores with ``ProjectAnomalySettings.sigma_threshold``,
        which is the value Settings > Monitoring writes. The read path asked
        ``ScanConfig.sigma_threshold`` instead — a column no API has ever
        written — so after an operator moved the project sigma to 5.0 every
        drilldown kept drawing the band at the scan row's stale value, and
        buckets sat outside a band that had not flagged them.

        The scan row is deliberately set to 9.0, a value the project never had,
        so restoring ``_get_scan_config_sigma_threshold`` reddens all three
        assertions with a number that could only have come from that column.
        """
        slug = "batch6-sigma-project"
        ctx = await _setup_metrics_project(client, slug=slug)
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": "Purchase",
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        base = datetime(2026, 1, 1, 10, tzinfo=UTC)
        scan_config_id = await _seed_event_metrics_at(
            ctx["project_id"],
            event_id,
            name="Sigma scan",
            points=[(base, 10), (base + timedelta(hours=1), 12)],
        )
        await _set_dead_scan_sigma(scan_config_id, 9.0)
        async with TestSessionLocal() as session:
            # App-version responses only carry a sigma once the scan is
            # version-aware; without the column they short-circuit before it.
            await session.execute(
                update(ScanConfig)
                .where(ScanConfig.id == scan_config_id)
                .values(app_version_column="app_version")
            )
            await session.commit()

        patched = await client.patch(
            f"/api/v1/projects/{slug}/anomaly-settings",
            json={"sigma_threshold": 5.0},
        )
        assert patched.status_code == 200, patched.text

        total = await client.get(f"/api/v1/projects/{slug}/metrics/total")
        drilldown = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/metrics")
        versions = await client.get(f"/api/v1/projects/{slug}/scans/{scan_config_id}/app-versions")

        assert total.status_code == 200, total.text
        assert total.json()["sigma_threshold"] == 5.0
        assert drilldown.status_code == 200, drilldown.text
        assert drilldown.json()["sigma_threshold"] == 5.0
        assert versions.status_code == 200, versions.text
        assert versions.json()["sigma_threshold"] == 5.0

    async def test_a_scope_override_still_wins_over_the_project_setting(self, client: AsyncClient):
        """The detector's two-step is override first, project setting behind it.

        Moving the fallback off the scan row must not flatten that order: a
        scope the false-positive ratchet has tightened is scored at ITS sigma,
        so that is what its own drilldown has to draw.
        """
        from tripl.models.anomaly_scope_override import AnomalyScopeOverride

        slug = "batch6-sigma-override"
        ctx = await _setup_metrics_project(client, slug=slug)
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": "Refund",
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert created.status_code == 201, created.text
        event_id = created.json()["id"]
        base = datetime(2026, 1, 1, 10, tzinfo=UTC)
        scan_config_id = await _seed_event_metrics_at(
            ctx["project_id"],
            event_id,
            name="Override scan",
            points=[(base, 10), (base + timedelta(hours=1), 12)],
        )
        await _set_dead_scan_sigma(scan_config_id, 9.0)
        patched = await client.patch(
            f"/api/v1/projects/{slug}/anomaly-settings",
            json={"sigma_threshold": 5.0},
        )
        assert patched.status_code == 200, patched.text
        async with TestSessionLocal() as session:
            session.add(
                AnomalyScopeOverride(
                    id=uuid.uuid4(),
                    project_id=uuid.UUID(ctx["project_id"]),
                    scan_config_id=scan_config_id,
                    scope_type="event",
                    scope_ref=str(event_id),
                    scope_name="Refund",
                    sigma_threshold=6.5,
                    min_expected_count=99,
                    false_positive_count=1,
                )
            )
            await session.commit()

        drilldown = await client.get(f"/api/v1/projects/{slug}/events/{event_id}/metrics")
        # The project-total scope has no override, so it keeps the project value.
        total = await client.get(f"/api/v1/projects/{slug}/metrics/total")

        assert drilldown.status_code == 200, drilldown.text
        assert drilldown.json()["sigma_threshold"] == 6.5
        assert total.status_code == 200, total.text
        assert total.json()["sigma_threshold"] == 5.0


class TestServedCatalogStddev:
    async def test_a_catalog_point_and_its_signal_serve_the_floored_stddev(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """the band was narrower than the rule that drew the dot.

        The detector divides by the FLOORED effective stddev, and stores it
        alongside the raw one exactly so the chart can draw ``expected ± k *
        stddev`` around the same decision. Event-scope points already served it
        (``_served_stddev``); catalog-metric points served the raw column, and
        ``latest_signal`` served it on both scopes — so the card beside the
        chart quoted a different width than the chart.

        The seeded row floors 4.0 to 0.5, an eightfold difference: putting
        ``anomaly.stddev`` back makes both assertions read 4.0.
        """
        slug = project["slug"]
        metric = await _create_sql_metric(client, slug, data_source["id"], "floored-band")
        scan_config_id = await _seed_scan_config(project["id"])
        await _seed_metric_values(metric["id"], [(B0, 10.0), (B2, 0.0)])
        async with TestSessionLocal() as session:
            session.add(
                MetricAnomaly(
                    id=uuid.uuid4(),
                    scan_config_id=scan_config_id,
                    scope_type="metric",
                    scope_ref=metric["id"],
                    event_id=None,
                    event_type_id=None,
                    bucket=B2,
                    actual_count=0.0,
                    expected_count=10.0,
                    stddev=4.0,
                    effective_stddev=0.5,
                    z_score=-20.0,
                    direction="drop",
                )
            )
            await session.commit()

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/series")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        flagged = next(point for point in body["data"] if point["is_anomaly"])
        assert flagged["stddev"] == 0.5
        assert body["latest_signal"] is not None
        assert body["latest_signal"]["stddev"] == 0.5


class TestFractionalVersionActivation:
    async def test_a_fractional_metric_without_project_totals_marks_releases_active(
        self, client: AsyncClient, project: dict, data_source: dict
    ):
        """``is_active`` is NOT "always False" for a ratio.

        The schema comment claimed a fractional metric can never be active
        because a value-share gate is meaningless on a ratio. The code says
        something else, and something more useful: with no project-total
        maturity rows to gate on, every RELEASED version is active (a
        prerelease still is not), because the metric's own ratio rows cannot
        answer "does this release carry real traffic" and refusing to answer
        would retire every version at once.

        This pins the rule the rewritten comment now states. A comment cannot be
        reverted into a failure, but changing the branch it describes — for
        instance gating the no-maturity case on the ratio values themselves —
        turns ``1.1.0`` inactive and reddens the test.
        """
        slug = project["slug"]
        metric = await _create_sql_metric(
            client,
            slug,
            data_source["id"],
            "ratio-no-totals",
            app_version_column="app_version",
        )
        await _seed_breakdowns(
            metric["id"],
            [
                ("app_version", "1.0.0", B0, 0.31),
                ("app_version", "1.1.0", B0, 0.29),
                ("app_version", "1.2.0-rc1", B0, 0.30),
            ],
        )

        resp = await client.get(f"{_metrics_url(slug)}/{metric['id']}/versions")

        assert resp.status_code == 200, resp.text
        versions = {item["version"]: item for item in resp.json()["versions"]}
        assert versions["1.0.0"]["is_active"] is True
        assert versions["1.1.0"]["is_active"] is True
        # A prerelease is ineligible however the gate is resolved.
        assert versions["1.2.0-rc1"]["is_active"] is False


class TestSeasonalityScopeRef:
    async def test_a_non_uuid_scope_ref_is_rejected_not_crashed(self, client: AsyncClient):
        """the heatmap 500'd where its siblings answer 422.

        The route types ``scope_ref`` as free text (it only strips NUL bytes),
        and for the event and event-type scopes ``_scope_metric_filters`` hands
        it straight to ``uuid.UUID()``. The ValueError was unhandled and the
        catch-all turned it into a generic 500, while breakdown-timeline and
        distribution-drifts validate the same parameter and return 422.

        Removing the ``_parse_scope_uuid`` guard puts the 500 back on both
        scopes; the project-total case is here to show the guard did not start
        rejecting the scope that legitimately passes a scan id.
        """
        slug = "batch6-seasonality-scope"
        ctx = await _setup_metrics_project(client, slug=slug)
        created = await client.post(
            f"/api/v1/projects/{slug}/events",
            json={
                "event_type_id": ctx["page_type_id"],
                "name": "Open",
                "field_values": [{"field_definition_id": ctx["page_field_id"], "value": "x"}],
            },
        )
        assert created.status_code == 201, created.text
        base = datetime(2026, 1, 1, 10, tzinfo=UTC)
        scan_config_id = await _seed_event_metrics_at(
            ctx["project_id"],
            created.json()["id"],
            name="Seasonality scan",
            points=[(base, 10)],
        )
        url = f"/api/v1/projects/{slug}/scans/{scan_config_id}/seasonality"

        for scope_type in ("event", "event_type"):
            bad = await client.get(url, params={"scope_type": scope_type, "scope_ref": "abc"})
            assert bad.status_code == 422, (scope_type, bad.status_code, bad.text)

        ok = await client.get(
            url, params={"scope_type": "project_total", "scope_ref": str(scan_config_id)}
        )
        assert ok.status_code == 200, ok.text
