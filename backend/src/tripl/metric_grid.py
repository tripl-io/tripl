"""The bucket grid a catalog metric is scored on — resolved in ONE place.

Every surface that classifies a ``metric``-scope signal has to measure it
against the metric's OWN grid: the freshness horizon is
``max(recent_window, 3 x interval)``, so a daily metric judged against a bare
24h window closes on the day it fires (see
``services.monitoring_utils._freshness_horizon``).

Where that grid is recorded depends on the kind:

* ``sql`` / ``fact`` metrics collect on their own ``MetricDefinition.interval``;
* ``event_composition`` metrics leave ``interval`` NULL and align onto a SOURCE
  SCAN's grid, which is recorded on the ``scan_config_id`` stamped on their
  stored ``MetricValue`` rows (NULL for the other two kinds).

A metric whose values were collected under more than one ``scan_config_id``
(e.g. after a source config was recreated) must resolve deterministically to the
grid currently in use, so the most-recent bucket's ``scan_config_id`` wins.

This module owns that rule so the request path (async) and the worker (sync)
cannot drift: three call sites had already grown their own answers — the sidebar
badge used no interval at all, the metrics/anomalies list paths used only the
metric's own column, and alert dispatch substituted whichever scan happened to
be dispatching. It is a pure leaf (models + SQLAlchemy,
no service or worker imports) and hands back a statement rather than running it,
because only the caller knows whether its session is sync or async.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import ColumnExpressionArgument, Row, Select, and_, func, select
from sqlalchemy.orm import InstrumentedAttribute

from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.scan_config import ScanConfig


@dataclass(frozen=True)
class MetricGrid:
    """The resolved grid of one catalog metric."""

    metric_definition_id: uuid.UUID
    project_id: uuid.UUID
    # Own interval, else the inherited source-scan interval, else None. A metric
    # with neither has no grid to be scored on; callers fall back to the bare
    # recent window (classification) or skip the metric entirely (detection).
    interval: str | None
    # The source scan its newest stored value was collected under, or None. Kept
    # alongside the interval because the series read path reports it to the UI.
    scan_config_id: uuid.UUID | None


MetricGridRow = Row[uuid.UUID, uuid.UUID, str | None, uuid.UUID | None]


def metric_grid_stmt(
    *criteria: ColumnExpressionArgument[bool],
) -> Select[uuid.UUID, uuid.UUID, str | None, uuid.UUID | None]:
    """Select ``(metric id, project id, effective interval, source scan)`` per metric.

    ``criteria`` are ``MetricDefinition`` predicates — one metric
    (``MetricDefinition.id == x``) or a whole project's monitored catalog. Every
    matching metric yields exactly one row, whether or not it has stored values.

    The source scan is picked with a ``ROW_NUMBER() OVER (PARTITION BY
    metric_definition_id ORDER BY bucket DESC)`` window (the same idiom
    ``metric_definition_service._load_latest_values`` uses) rather than a
    per-metric ``LIMIT 1``, so a batch of N metrics still costs one query. The
    window is bounded twice over — to the matching metrics, and to rows that
    carry a ``scan_config_id`` at all, which excludes every ``sql``/``fact``
    value row — so it never widens into a full ``metric_values`` scan.
    """
    matching_metric_ids = select(MetricDefinition.id).where(*criteria).scalar_subquery()
    source_scans = (
        select(
            MetricValue.metric_definition_id.label("metric_definition_id"),
            MetricValue.scan_config_id.label("scan_config_id"),
            func.row_number()
            .over(
                partition_by=MetricValue.metric_definition_id,
                order_by=MetricValue.bucket.desc(),
            )
            .label("row_num"),
        )
        .where(
            MetricValue.metric_definition_id.in_(matching_metric_ids),
            MetricValue.scan_config_id.is_not(None),
        )
        .subquery()
    )
    return (
        select(
            MetricDefinition.id,
            MetricDefinition.project_id,
            # The rule itself: own grid first, inherited source-scan grid second.
            func.coalesce(MetricDefinition.interval, ScanConfig.interval),
            source_scans.c.scan_config_id,
        )
        .select_from(MetricDefinition)
        .outerjoin(
            source_scans,
            and_(
                source_scans.c.metric_definition_id == MetricDefinition.id,
                source_scans.c.row_num == 1,
            ),
        )
        .outerjoin(ScanConfig, ScanConfig.id == source_scans.c.scan_config_id)
        .where(*criteria)
    )


def metric_grids(rows: Iterable[MetricGridRow]) -> dict[uuid.UUID, MetricGrid]:
    """Fold :func:`metric_grid_stmt` rows into a lookup keyed by metric id."""
    return {
        metric_definition_id: MetricGrid(
            metric_definition_id=metric_definition_id,
            project_id=project_id,
            # A native enum column comes back as a StrEnum member; normalize to
            # the plain value so callers can compare and dict-key it freely.
            interval=None if interval is None else str(interval),
            scan_config_id=scan_config_id,
        )
        for metric_definition_id, project_id, interval, scan_config_id in rows
    }


def grid_population_filter(
    scan_config_column: InstrumentedAttribute[uuid.UUID | None],
    *,
    interval: str | None,
    scan_config_id: uuid.UUID | None,
) -> ColumnExpressionArgument[bool]:
    """Restrict value rows to the metric's grid POPULATION.

    The population is every source config collecting the metric ON THE
    RESOLVED GRID'S INTERVAL — not the single config :func:`metric_grid_stmt`
    names. Every reader SUMS across it: the series read
    (``metric_series_service._load_metric_values``), the open-anchor probe, the
    breakdown read, and the detector (``detect._load_metric_value_points``), so
    the line a chart draws and the ``expected_count`` / ``stddev`` band drawn
    around it describe one series. ``scan_config_column`` is
    ``MetricValue.scan_config_id`` or ``MetricValueBreakdown.scan_config_id`` —
    the breakdown rows are written per source config exactly like the totals.

    NOT the single resolved config: an ``event_composition`` metric stores one
    value series per source scan (``metric_collect._compose_grid_region`` writes
    a row set per config that collected the numerator), so two LIVE configs may
    legally hold the same bucket — one event type collected by an iOS scan and
    an Android scan is the ordinary shape. Filtering to one of them plots an
    ADDEND, and ``metric_grid_stmt`` picks that one with an
    ``ORDER BY bucket DESC`` tie-break that is undefined between two
    equally-current configs, so which addend could also flap.

    NOT every config either: two grids of DIFFERENT intervals are not addable (a
    1h count and a 1d count are not the same unit), and mixing them added an
    hour's count to a day's at every shared midnight. The interval is the line
    between "another source of this series" and "a retired grid".

    ``scan_config_id is None`` means the metric is ``sql``/``fact`` (or the
    metric row vanished mid-run): those rows are written with a NULL
    ``scan_config_id`` exclusively, so the IS NULL branch is exact rather than
    merely narrower and the interval never enters.

    KNOWN OPEN (grid-population follow-up): two configs scanning the SAME
    warehouse rows on the same interval are summed, i.e. double-counted. Nothing
    stored tells them apart from two configs covering disjoint traffic, so the
    read cannot decide it — that is a collection-side question.

    One definition for the async read path and the sync worker:
    the two used to spell it separately, which is how the read and the detector
    drifted in the first place.
    """
    if scan_config_id is None:
        return scan_config_column.is_(None)
    on_grid = ScanConfig.interval.is_(None) if interval is None else ScanConfig.interval == interval
    return scan_config_column.in_(select(ScanConfig.id).where(on_grid).scalar_subquery())
