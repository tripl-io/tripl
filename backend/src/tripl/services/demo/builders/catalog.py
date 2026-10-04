"""Metrics-catalog builder: a fact table, four metric definitions, and values.

Definitions are built through the authoritative Pydantic create schemas
(``FactTableCreate``, ``SqlMetricCreate``, ``EventCompositionMetricCreate``,
``FactMetricCreate``) so their validators run (SQL safety, identifier allowlist,
fact-composition rules), then persisted as ORM rows via ``to_create_values()``.
The catalog services are deliberately NOT called — each commits internally, which
would break the seeder's single end-of-function commit.

Pre-collected ``MetricValue`` series make the catalog and drilldowns render with
data (the demo worker never runs). The ``sql`` and two ``fact`` series are NOT
invented: they are derived from the synthetic dataset by running the REAL
``SyntheticAdapter`` (via ``registry.build_adapter`` over the demo's synthetic
DataSource) with each metric's validated config — the same shapes the worker
collectors use — so the seeded values are reproducible from the synthetic rows.
The adapter is pure in-memory (no network/filesystem), but building its dataset
and scanning it is still CPU work, so it runs in a worker thread rather than on
the API event loop; only the row inserts happen on the loop.
Values are deterministic for a given clock/seed.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.adapters.base import AggregateSpec, BaseAdapter
from tripl.core.adapters.registry import build_adapter
from tripl.core.intervals import get_interval
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricStatus,
    ScanInterval,
)
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.schemas.fact_table import (
    FactTableColumnSchema,
    FactTableCreate,
    FactTableRowFilter,
)
from tripl.schemas.metric_definition import (
    EventCompositionMetricCreate,
    FactMetricCreate,
    FactOperand,
    SqlConfig,
    SqlMetricCreate,
)
from tripl.services.demo import noise
from tripl.services.demo.scenario import DemoContext

# The seeded ``active_sessions`` statement, as ONE constant rather than a literal
# buried in the create schema. Two readers need the exact text: the synthetic
# adapter recognises the sql-metric statements it can compute by EXACT match
# (``synthetic._ACTIVE_SESSIONS_STATEMENTS``) rather than by probing for
# substrings, and ``test_synthetic_warehouse_numbers`` imports this constant to pin the two
# together. The adapter carries its own copy rather than importing this one, and
# the reason is not that Python forbids it: ``core/`` importing ``services/`` is a
# direction this repo allows exactly once and deliberately
# (``core/analyzers/release_regression.py`` imports ``services.version_activation``);
# nothing enforces a rule against it. What is true is that THIS module imports
# ``core.adapters.registry`` at module level, so an import back would make
# ``core.adapters`` and ``services.demo`` depend on each other, and it would pull
# the demo seeder's import graph into every synthetic adapter build. (It would not
# actually cycle at import time — ``registry`` imports each adapter inside a
# function — so if the duplication is ever collapsed, that is the constraint to
# check, not a blanket layering rule.) Naming the statement here is what makes the
# duplication testable instead of silent.
#
# ``GROUP BY ts`` is not decoration. Without it real ClickHouse rejects the
# statement ("not under aggregate function and not in GROUP BY"), so the demo —
# whose job is to teach the product — shipped a metric whose SQL would fail on
# the warehouse it was demonstrating. Demos created before
# that fix still carry the GROUP BY-less text in their stored config; the adapter
# keeps a legacy entry for exactly that reason.
ACTIVE_SESSIONS_METRIC_SQL = (
    "SELECT toStartOfDay(event_time) AS ts, "
    "count(DISTINCT session_id) AS value FROM events GROUP BY ts"
)


async def build_catalog(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_fact_table(session, ctx)
    metric_defs = await _build_metric_definitions(session, ctx)
    await _build_metric_values(session, ctx, metric_defs)


async def _build_fact_table(session: AsyncSession, ctx: DemoContext) -> None:
    orders_fact_create = FactTableCreate(
        name="orders",
        display_name="Orders",
        description="One row per store order, used by the revenue fact metrics.",
        color="#0ea5e9",
        data_source_id=ctx.data_source_id,
        sql=("SELECT created_at, amount, currency, user_id, country, status FROM orders"),
        timestamp_column="created_at",
        columns=[
            FactTableColumnSchema(name="created_at", type="timestamp"),
            FactTableColumnSchema(name="amount", type="number"),
            FactTableColumnSchema(name="currency", type="string"),
            FactTableColumnSchema(name="user_id", type="string"),
            FactTableColumnSchema(name="country", type="string"),
            FactTableColumnSchema(name="status", type="string"),
        ],
        identifier_columns=["user_id"],
        row_filters=[FactTableRowFilter(name="completed", sql="status = 'completed'")],
    )
    orders_fact = FactTable(
        project_id=ctx.project_id, order=0, **orders_fact_create.to_create_values()
    )
    session.add(orders_fact)
    await session.flush()
    ctx.fact_table_id = orders_fact.id


async def _build_metric_definitions(
    session: AsyncSession, ctx: DemoContext
) -> dict[str, MetricDefinition]:
    """Four definitions: one sql, one event_composition ratio, two fact (single + ratio)."""
    sql_metric_create = SqlMetricCreate(
        name="active_sessions",
        display_name="Active Sessions",
        description="Distinct sessions seen per day.",
        color="#6366f1",
        order=0,
        unit="sessions",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        config=SqlConfig(metric_sql=ACTIVE_SESSIONS_METRIC_SQL, time_column="ts"),
        data_source_id=ctx.data_source_id,
        interval=ScanInterval.d1,
    )
    conversion_metric_create = EventCompositionMetricCreate(
        name="purchase_conversion",
        display_name="Purchase conversion",
        description="Completed purchases per Home Screen View.",
        color="#22c55e",
        order=1,
        unit="%",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.ratio,
        numerator_event_id=ctx.event_ids["Purchase Completed"],
        denominator_event_id=ctx.event_ids["Home Screen View"],
    )
    revenue_metric_create = FactMetricCreate(
        name="revenue_completed",
        display_name="Revenue (completed)",
        description="Sum of completed-order amounts per day.",
        color="#f59e0b",
        order=2,
        unit="$",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.single,
        interval=ScanInterval.d1,
        fact_table_id=ctx.fact_table_id,
        aggregation=MetricAggregation.sum,
        measure_column="amount",
        row_filters=["completed"],
    )
    aov_metric_create = FactMetricCreate(
        name="average_order_value",
        display_name="Average order value",
        description="Completed-order revenue divided by completed-order count.",
        color="#a855f7",
        order=3,
        unit="$",
        status=MetricStatus.active,
        anomaly_detection_enabled=True,
        composition=MetricComposition.ratio,
        interval=ScanInterval.d1,
        numerator=FactOperand(
            fact_table_id=ctx.fact_table_id,
            aggregation=MetricAggregation.sum,
            measure_column="amount",
        ),
        denominator=FactOperand(
            fact_table_id=ctx.fact_table_id,
            aggregation=MetricAggregation.count,
        ),
    )

    metric_defs: dict[str, MetricDefinition] = {}
    for metric_create in (
        sql_metric_create,
        conversion_metric_create,
        revenue_metric_create,
        aov_metric_create,
    ):
        metric_def = MetricDefinition(project_id=ctx.project_id, **metric_create.to_create_values())
        metric_def.last_collected_at = ctx.now
        metric_def.last_collection_status = "success"
        session.add(metric_def)
        metric_defs[metric_create.name] = metric_def
    await session.flush()
    return metric_defs


# 30-day DAILY window for the sql + fact metrics, matching the daily interval
# (``ScanInterval.d1``) the definitions carry.
_DEMO_METRIC_HISTORY_DAYS = 30
# The orders fact-table SQL (kept in sync with ``_build_fact_table``); the
# adapter reads the ``orders`` table from it and applies the operand filters.
_ORDERS_SQL = "SELECT created_at, amount, currency, user_id, country, status FROM orders"
# Per-query row ceiling for the derivation reads (one row per day here).
_METRIC_QUERY_ROW_LIMIT = 100_000


async def _build_metric_values(
    session: AsyncSession, ctx: DemoContext, metric_defs: dict[str, MetricDefinition]
) -> None:
    """Seed each metric's per-bucket values.

    ``purchase_conversion`` (event_composition) is a small live-looking fraction.
    The ``sql`` and two ``fact`` metrics are derived FROM the synthetic dataset by
    running the real ``SyntheticAdapter`` with each metric's config, so the seeded
    values are reproducible from the synthetic rows rather than invented.
    """
    _build_conversion_values(session, ctx, metric_defs["purchase_conversion"])

    data_source = await session.get(DataSource, ctx.data_source_id)
    if data_source is not None:
        sql_metric = metric_defs["active_sessions"]
        metric_sql = str((sql_metric.config or {}).get("metric_sql", ""))
        series = await asyncio.to_thread(_read_adapter_series, data_source, ctx.now, metric_sql)
        _add_adapter_derived_values(session, metric_defs, series)
    await session.flush()


def _build_conversion_values(
    session: AsyncSession, ctx: DemoContext, conversion_metric: MetricDefinition
) -> None:
    # event_composition ratio -> ~7 days HOURLY, aligned to the source scan grid
    # (scan_config_id set). Conversion drifts gently upward with a daily ripple so
    # the ratio reads as a live line; stays a small fraction (~0.05-0.11).
    conversion_buckets = noise.hour_buckets(ctx.now, days=7)
    conversion_span = max(len(conversion_buckets) - 1, 1)
    for idx, bucket in enumerate(conversion_buckets):
        progress = idx / conversion_span
        daily_ripple = 0.015 * math.sin(bucket.hour * math.pi / 12)
        session.add(
            MetricValue(
                metric_definition_id=conversion_metric.id,
                scan_config_id=ctx.scan_config_id,
                bucket=bucket,
                value=0.05 + 0.045 * progress + daily_ripple,
            )
        )


@dataclass(frozen=True)
class _AdapterSeries:
    """The raw adapter reads behind the sql + fact metric values."""

    revenue_rows: list[tuple[object, ...]]
    aov_rows: list[tuple[object, ...]]
    sql_columns: list[str]
    sql_rows: list[tuple[object, ...]]


def _read_adapter_series(data_source: DataSource, now: datetime, metric_sql: str) -> _AdapterSeries:
    """Build the synthetic adapter and derive the series. The worker-thread target.

    Runs off the event loop, so it touches nothing but the
    already-loaded ``data_source`` columns: the adapter's constructor generates
    the whole synthetic dataset and every read scans it, all CPU.
    """
    adapter = build_adapter(data_source)
    try:
        return _derive_adapter_series(adapter, now, metric_sql)
    finally:
        adapter.close()


def _derive_adapter_series(adapter: BaseAdapter, now: datetime, metric_sql: str) -> _AdapterSeries:
    """Derive the sql + fact series from the synthetic adapter. No session.

    Mirrors what the worker collectors compute: a conditional daily SUM for the
    filtered single (revenue), a two-operand daily ratio (average order value),
    and the seeded distinct-sessions SQL read back through ``get_preview_rows``.
    """
    day = timedelta(days=1)
    interval_code = get_interval(ScanInterval.d1.value).code
    end_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_from = end_day - day * _DEMO_METRIC_HISTORY_DAYS
    # Ends at the start of TODAY, so only COMPLETE days are seeded. This is
    # exactly the window the worker would compute — ``metric_collect
    # ._resolve_value_window`` sets ``time_to = floor_to_bucket(now, interval)``
    # — so the seeded series and every later recollection agree about what a
    # finished bucket is. It used to run to ``end_day + day``, which seeded a
    # partial "today" bucket that no collection would ever reproduce: the newest
    # point of each daily chart was a fraction of a day's data sitting next to
    # thirty full ones, and it read as a drop rather than as a day in progress.
    time_to = end_day

    # revenue_completed: SUM(amount) WHERE status = 'completed' per day -- the
    # same conditional aggregate the batched fact collector runs for a
    # filtered single.
    revenue_specs = [
        AggregateSpec(
            key="revenue",
            aggregation=MetricAggregation.sum,
            column="amount",
            filter_sql="status = 'completed'",
        )
    ]
    _revenue_cols, revenue_rows = adapter.get_time_bucketed_multi_aggregate(
        _ORDERS_SQL, "created_at", interval_code, revenue_specs, time_from, time_to
    )
    # average_order_value: SUM(amount) / COUNT(*) per day -- the ratio's two
    # operands read from one shared daily scan, divided per bucket.
    aov_specs = [
        AggregateSpec(key="numerator", aggregation=MetricAggregation.sum, column="amount"),
        AggregateSpec(key="denominator", aggregation=MetricAggregation.count),
    ]
    _aov_cols, aov_rows = adapter.get_time_bucketed_multi_aggregate(
        _ORDERS_SQL, "created_at", interval_code, aov_specs, time_from, time_to
    )
    # active_sessions (sql): distinct sessions per day, read back through the
    # same get_preview_rows path the sql collector uses (the adapter
    # recognises the seeded metric SQL and computes it from the synthetic
    # events).
    column_names, rows = adapter.get_preview_rows(
        metric_sql,
        limit=_METRIC_QUERY_ROW_LIMIT,
        time_column="ts",
        time_from=time_from,
        time_to=time_to,
    )
    return _AdapterSeries(
        revenue_rows=revenue_rows,
        aov_rows=aov_rows,
        sql_columns=column_names,
        sql_rows=rows,
    )


def _add_adapter_derived_values(
    session: AsyncSession,
    metric_defs: dict[str, MetricDefinition],
    series: _AdapterSeries,
) -> None:
    """Insert the adapter-derived series as ``MetricValue`` rows (scan_config_id NULL)."""
    for bucket, revenue in series.revenue_rows:
        if revenue is None:
            continue
        session.add(
            MetricValue(
                metric_definition_id=metric_defs["revenue_completed"].id,
                scan_config_id=None,
                bucket=bucket,
                value=float(revenue),  # type: ignore[arg-type]
            )
        )

    for bucket, numerator, denominator in series.aov_rows:
        if not denominator:
            continue
        session.add(
            MetricValue(
                metric_definition_id=metric_defs["average_order_value"].id,
                scan_config_id=None,
                bucket=bucket,
                value=float(numerator) / float(denominator),  # type: ignore[arg-type]
            )
        )

    sql_metric = metric_defs["active_sessions"]
    ts_index = series.sql_columns.index("ts")
    value_index = series.sql_columns.index("value")
    for row in series.sql_rows:
        session.add(
            MetricValue(
                metric_definition_id=sql_metric.id,
                scan_config_id=None,
                bucket=row[ts_index],
                value=float(row[value_index]),  # type: ignore[arg-type]
            )
        )
