"""Celery task that collects per-bucket values for catalog MetricDefinitions.

Mirrors ``collect_metrics`` (event metrics) but writes Float values into
``metric_values`` / ``metric_value_breakdowns`` for one ``MetricDefinition`` per
run. Three kinds are supported:

* ``fact`` -- an aggregation over a column of a separately-defined ``FactTable``,
  via ``adapter.get_time_bucketed_aggregate`` (+ optional per-dimension
  breakdowns via ``get_time_bucketed_aggregate_breakdown``). ``single`` collects
  one operand series; ``ratio`` divides a numerator series by a denominator
  series (each operand a — possibly different — FactTable). The base query, time
  column and data source are taken from the referenced FactTable; a named row
  filter is resolved to its stored SQL fragment at collection time.
* ``sql`` -- a user-authored per-bucket SELECT, executed via
  ``adapter.get_preview_rows`` after a defensive ``validate_select_sql`` that
  binds the value/time column names.
* ``event_composition`` -- derived from already-collected ``event_metrics`` on
  the source scan grid: ``single`` count, ``ratio`` A/B, or
  ``per_distinct_user`` (numerator / per-bucket distinct-user count, the latter
  being a fresh warehouse ``count_distinct`` series).

Idempotency: every kind WINDOW-DELETEs the affected ``(definition[, config])``
rows before UPSERTing, so a re-run overwrites a window instead of duplicating
it. ``fact`` / ``sql`` write ``scan_config_id = NULL`` rows;
``event_composition`` writes rows keyed by the source ``scan_config_id`` grid.

NOTE on divide-by-zero: ``metric_values.value`` is NOT NULL (see the M2 model /
migration), so a ``ratio`` / ``per_distinct_user`` bucket whose denominator is
zero -- which the pure evaluator maps to ``None`` -- is NOT written. The
window-delete-then-insert pass means any previously stored value for that bucket
is cleared, so the bucket reads back as "no value" (absent) rather than a
misleading ``0``.

Tests monkey-patch ``_get_sync_session``, ``_build_adapter`` and
``_resolve_value_window`` as globals of THIS module.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.adapters.base import AggregateSpec, BaseAdapter, rank_top_n_once
from tripl.core.adapters.measure_validator import (
    SqlDialect,
    coerce_aggregation,
    dialect_for_db_type,
    requires_measure,
    validate_select_sql,
)
from tripl.core.bucketing import floor_to_bucket, to_utc
from tripl.core.collection_progress import collection_progress_to
from tripl.core.intervals import IntervalSpec, get_interval
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event_metric import EventMetric
from tripl.models.fact_table import FactTable

# Re-exported deliberately (``X as X`` is the explicit-re-export form): the
# constants moved to the model beside the one method that writes them, and
# ``schedule.py`` plus four test modules import them from HERE. Keeping the name
# reachable is what makes the relocation cost zero call sites.
from tripl.models.metric_definition import (
    COLLECTION_STATUS_ERROR as COLLECTION_STATUS_ERROR,
)
from tripl.models.metric_definition import (
    COLLECTION_STATUS_RUNNING as COLLECTION_STATUS_RUNNING,
)
from tripl.models.metric_definition import (
    COLLECTION_STATUS_SUCCESS as COLLECTION_STATUS_SUCCESS,
)
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.data_source_scope import data_source_out_of_project_scope
from tripl.worker.analyzers.metric_composition import evaluate_composition
from tripl.worker.celery_app import celery_app
from tripl.worker.tasks._errors import ScanError, user_facing_error
from tripl.worker.tasks.metrics._fact_conditions import (
    _conditions_from_config,
    _config_str,
    _effective_filter_names,
    _fact_operand_measure,
    _FactOperand,
    _operand_from_config,
    _resolve_batch_operand,
    _resolve_fact_operand_query,
    _validate_breakdown_columns,
    _validate_condition_columns,
    _validated_measure_column,
)
from tripl.worker.tasks.metrics._helpers import (
    _build_adapter,
    _get_sync_session,
    _parse_task_datetime,
)
from tripl.worker.tasks.metrics.generation import _iter_window_chunks
from tripl.worker.tasks.metrics.metric_rows import (
    _delete_metric_value_breakdowns_window,
    _delete_metric_values_window,
    _upsert_metric_value_breakdown_rows,
    _upsert_metric_values_rows,
)

logger = logging.getLogger(__name__)


def event_composition_binding_error(definition: MetricDefinition) -> str | None:
    """Why this metric can never produce a value, or ``None`` if it can.

    An ``event_composition`` metric reads an already-collected series through
    ``_read_event_metric_series``, which returns ``{}`` for TWO different
    situations that the collector then treated identically:

    * the operand is configured and simply has no rows yet — legitimately zero,
      and it must stay silent;
    * the metric has no operand AT ALL, both the event ref and the event-type
      ref being NULL — structurally unable to produce anything, ever.

    Reporting the second as ``{"values": 0, "grids": 0}`` is what made the flatline bug
    invisible for as long as it was: a SUCCESS with zero rows leaves
    ``last_collection_status`` green, and ``_event_composition_due`` then reads
    the same empty series and returns ``False`` forever, so the scheduler stops
    asking. The metric flatlines and nothing anywhere says why.

    A user cannot reach this state by hand — the schemas require a numerator on
    create and on update, and a denominator for ``ratio`` — so a NULL operand is
    ALWAYS the footprint of an ``ondelete="SET NULL"``: a deleted event
    (now also carried by the group merge) or a deleted event type.
    Checking the binding rather than the deleting door is the
    point: one kind-level guard covers every present and future door.

    Returned rather than raised so ``_event_composition_due`` can ask the same
    question without catching an exception.
    """
    if definition.composition is None:
        return "This metric has no composition set, so there is nothing to compute."
    composition = (
        definition.composition
        if isinstance(definition.composition, MetricComposition)
        else MetricComposition(definition.composition)
    )
    if definition.numerator_event_id is None and definition.numerator_event_type_id is None:
        return (
            "This metric's numerator no longer points at anything. The event or event type "
            "it was defined against has been deleted, so the metric can never produce a "
            "value. Point it at a live event or event type, or delete the metric."
        )
    if composition is MetricComposition.ratio and (
        definition.denominator_event_id is None and definition.denominator_event_type_id is None
    ):
        return (
            "This ratio's denominator no longer points at anything. The event or event type "
            "it was defined against has been deleted, so the metric can never produce a "
            "value. Point it at a live event or event type, or delete the metric."
        )
    return None


def mark_collection_error(definition: MetricDefinition, message: str) -> None:
    """Put one metric into the error state — the only way it is entered.

    The rule itself now lives on ``MetricDefinition.mark_collection_error``;
    read it there. It moved because the seventh call site is not a worker: the
    group-rule merge in ``core.analyzers`` has to fail a metric whose two ratio
    operands have collapsed onto one event, and ``core`` must never import
    ``worker``. Copying three assignments into a second module was the one
    option ruled out — that is exactly the drift
    ``test_no_error_path_can_set_the_status_without_the_timestamp`` exists to
    catch, and that test now walks the model module too.

    Kept as a delegator so the six existing call sites, and the tests importing
    it from here, do not move. Does NOT commit.
    """
    definition.mark_collection_error(message)


# Same per-task budget shape as collect_metrics, scaled down: catalog metrics do
# not run the multi-hour event-catalog replay path.
COLLECT_METRIC_DEFINITIONS_SOFT_TIME_LIMIT_SECONDS = 30 * 60
COLLECT_METRIC_DEFINITIONS_TIME_LIMIT_SECONDS = 35 * 60

# First-collection lookback for fact / sql metrics (in interval buckets),
# mirroring collect_metrics' ``time_to - delta * 30`` default.
DEFAULT_COLLECTION_BUCKETS = 30

# How far back an ``event_composition`` metric reaches on a grid it has never
# stored a value for, in that grid's own interval buckets.
#
# An event_composition metric has no interval and no watermark of its own, so
# its only resume signal is its last STORED bucket — and a ``per_distinct_user``
# metric whose denominator is permanently zero (a wrong ``user_id_column``, say)
# never stores one. Without this cap such a metric would re-query the whole
# retained event-metric history on every 300 s dispatch, and once that history
# held more than ``METRIC_QUERY_ROW_LIMIT`` non-empty buckets it would error out
# on every run with no way back. Two orders of magnitude below that ceiling, so
# ``_reject_truncated_rows`` is unreachable from the composition path.
EVENT_COMPOSITION_BACKFILL_BUCKETS = 5_000

# Manual "collect now" backfill: reach back this many interval buckets so a
# freshly-created metric's chart is not empty, regardless of prior collection
# state. Capped by ``MANUAL_COLLECT_MAX_WINDOW`` so a coarse interval (1d/1w)
# does not reach back months/years.
MANUAL_COLLECT_BACKFILL_BUCKETS = 48
MANUAL_COLLECT_MAX_WINDOW = timedelta(days=30)

# Per-query row ceiling; one row per bucket (no breakdown) or per
# (bucket, breakdown_value), so this comfortably bounds a normal window.
#
# Every call site asks for ``metric_query_fetch_limit()`` and runs the result
# through ``_reject_truncated_rows``. Asking for exactly the limit cannot tell a
# window that happens to have 100,000 rows from one the warehouse cut short, and
# the difference is not cosmetic here: collection WINDOW-DELETEs the chunk before
# UPSERTing, so a truncated read deletes rows it will never write back. The tail
# of the window is silently erased and the metric reads as a clean series with a
# hole in it.
METRIC_QUERY_ROW_LIMIT = 100_000


def metric_query_fetch_limit() -> int:
    """The ``LIMIT`` actually sent to the warehouse: the ceiling plus a probe row.

    Two numbers, one rule: ``METRIC_QUERY_ROW_LIMIT`` is the ceiling
    ``_reject_truncated_rows`` compares against, and this is what the query asks
    for. They must differ by exactly one or truncation detection stops working
    (see the comment on the constant above), so the ``+ 1`` is written once here
    rather than at each call site.

    It is a FUNCTION, not a derived module constant, for two reasons. First,
    ``METRIC_QUERY_ROW_LIMIT`` is a module global that tests rebind —
    ``monkeypatch.setattr(metric_collect, "METRIC_QUERY_ROW_LIMIT", n)`` appears
    in tests/test_scans.py and tests/test_metric_collector_column_guards.py — and a
    value computed at import time would not follow the patch, making the fetch limit and the
    ceiling disagree in exactly the tests that exist to make them agree. Second,
    ``services/metric_preview_service`` calls this to disclose the statement
    ``GET /metrics/{id}/generated-sql`` promises is "the exact adapter SQL used
    by collection"; before this existed that endpoint passed the bare ceiling and
    disclosed ``LIMIT 100000`` for a statement that runs ``LIMIT 100001``.
    """
    return METRIC_QUERY_ROW_LIMIT + 1


def _reject_truncated_rows[RowT](
    rows: Sequence[RowT],
    *,
    what: str,
    chunk_from: datetime | None = None,
    chunk_to: datetime | None = None,
) -> list[RowT]:
    """Fail the chunk rather than write a partial one, and trim the probe row.

    Mirrors the event-metrics path (``chunk_processing.py``): the caller asked
    for one row more than the ceiling, so ``> limit`` means the warehouse had
    more to give. Raising is the point — the alternative is a window-delete
    followed by a short re-insert, which loses the tail with no error anywhere.

    Raises ``ScanError``, not ``ValueError``: the message below is written for
    the user and names the two things they can change, but only a ``ScanError``
    is surfaced verbatim by ``user_facing_error`` — as a ``ValueError`` it was
    overwritten with the generic internal-error summary on
    ``last_collection_error`` / ``ScanJob.error_message``. Every
    caller funnels this into an ``except Exception`` that stamps the failure via
    ``user_facing_error``, so the change is confined to which text is persisted.
    """
    if len(rows) > METRIC_QUERY_ROW_LIMIT:
        window = ""
        if chunk_from is not None and chunk_to is not None:
            window = f" for chunk {chunk_from.isoformat()}..{chunk_to.isoformat()}"
        msg = (
            f"{what} reached the metric query row limit ({METRIC_QUERY_ROW_LIMIT})"
            f"{window}; narrow the metric's breakdown or use a shorter replay chunk "
            "interval — collecting it would overwrite the window with partial data"
        )
        raise ScanError(msg)
    return list(rows)


# Default value column a ``sql`` metric must project (alias the measure
# ``AS value``); override per metric with config ``value_column``. The time
# column name is taken from the metric config.
SQL_VALUE_COLUMN = "value"

# Column used for the per_distinct_user denominator when the metric config does
# not name one. Documented default; override with config ``user_id_column``.
DEFAULT_USER_ID_COLUMN = "user_id"

# Longest breakdown value we persist (matches the breakdown column width).
MAX_BREAKDOWN_VALUE_LENGTH = 500


def _coerce_value(raw: object) -> float:
    """Coerce a warehouse aggregate cell to ``float`` (ints, Decimals, strings)."""
    return float(cast("float | int | str", raw))


def _build_metric_value_rows(
    *,
    metric_definition_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
    values: Mapping[datetime, float | None],
) -> list[dict[str, object]]:
    """Build MetricValue UPSERT rows, dropping ``None`` (divide-by-zero) buckets.

    The ``value`` column is NOT NULL, so a ``None`` bucket cannot be stored; the
    surrounding window-delete clears any prior value so the bucket reads as
    absent rather than ``0``.
    """
    return [
        {
            "id": uuid.uuid4(),
            "metric_definition_id": metric_definition_id,
            "scan_config_id": scan_config_id,
            "bucket": bucket,
            "value": float(value),
        }
        for bucket, value in values.items()
        if value is not None
    ]


def _resolve_value_window(
    session: Session,
    *,
    metric_definition_id: uuid.UUID,
    interval_code: str,
) -> tuple[datetime, datetime]:
    """Resolve the [from, to) collection window for a fact/sql metric.

    Window end is the latest complete interval boundary. Window start resumes one
    interval before the last stored bucket (so the last bucket is recomputed), or
    falls back to ``DEFAULT_COLLECTION_BUCKETS`` intervals on first collection.
    Kept here (not in ``_helpers``) so tests can monkey-patch this module global.
    """
    delta = get_interval(interval_code).delta
    time_to = floor_to_bucket(datetime.now(UTC), interval_code)
    last_bucket = session.execute(
        select(sa_func.max(MetricValue.bucket)).where(
            MetricValue.metric_definition_id == metric_definition_id,
            MetricValue.scan_config_id.is_(None),
        )
    ).scalar()
    definition = session.get(MetricDefinition, metric_definition_id)
    progress_to = collection_progress_to(
        last_bucket=last_bucket,
        watermark=definition.last_collection_window_to if definition is not None else None,
        delta=delta,
    )
    if progress_to is not None:
        # Preserve the historical two-bucket overlap: the latest completed
        # bucket and the one immediately before it are recomputed for late data.
        effective_progress_to = min(progress_to, time_to)
        time_from = effective_progress_to - delta * 2
    else:
        time_from = time_to - delta * DEFAULT_COLLECTION_BUCKETS
    return time_from, time_to


def compute_manual_collect_window(interval_code: str) -> tuple[datetime, datetime]:
    """Resolve the [from, to) backfill window for a manual single-metric collect.

    ``to`` is the latest complete interval boundary; ``from`` reaches back
    ``MANUAL_COLLECT_BACKFILL_BUCKETS`` buckets, but the span is capped to
    ``MANUAL_COLLECT_MAX_WINDOW`` so coarse intervals stay bounded. Concretely:
    15m -> 48 buckets (12h), 1h -> 48 (2d), 6h -> 48 (12d), 1d -> 30 (30d),
    1w -> 4 (28d). Unlike ``_resolve_value_window`` this ignores any previously
    stored bucket so a re-trigger always backfills the same recent window.
    """
    delta = get_interval(interval_code).delta
    time_to = floor_to_bucket(datetime.now(UTC), interval_code)
    max_buckets = max(1, int(MANUAL_COLLECT_MAX_WINDOW / delta))
    buckets = min(MANUAL_COLLECT_BACKFILL_BUCKETS, max_buckets)
    time_from = time_to - delta * buckets
    return time_from, time_to


def _effective_value_window(
    session: Session,
    *,
    metric_definition_id: uuid.UUID,
    interval_code: str,
    window_override: tuple[datetime, datetime] | None,
    manual_backfill: bool = False,
) -> tuple[datetime, datetime]:
    """Use an explicit ``window_override`` when given, else the resume window.

    Keeps the override-vs-default choice in one place so every fact/sql
    collection path honours a manually-requested window identically.

    ``manual_backfill`` marks the bounded window a "collect now" click derives
    from ``compute_manual_collect_window``. That window is applied to every
    metric the click sweeps in — the whole fact-table dependency closure, not
    just the clicked one — so it may only ever reach FURTHER BACK than a metric's
    own resume point, never ahead of it. A metric lagging by more than the manual
    window (it had been failing, or was just reactivated) would otherwise collect
    only the recent slice while ``_stamp_metric_success`` advanced its watermark
    to the window end, stranding the un-queried buckets in between where no later
    resume reaches them. A legacy explicit replay window (``window_from`` /
    ``window_to`` on the task) is a deliberate request for one exact range and is
    still honoured verbatim.
    """
    if window_override is None:
        return _resolve_value_window(
            session,
            metric_definition_id=metric_definition_id,
            interval_code=interval_code,
        )
    if not manual_backfill:
        return window_override
    resume_from, _ = _resolve_value_window(
        session,
        metric_definition_id=metric_definition_id,
        interval_code=interval_code,
    )
    manual_from, manual_to = window_override
    return min(manual_from, resume_from), manual_to


def _parse_window_override(
    window_from: str | None, window_to: str | None
) -> tuple[datetime, datetime] | None:
    """Parse the optional manual-collection window passed to the Celery tasks.

    Both bounds are required together (mirrors ``collect_metrics`` replay). ISO
    strings are coerced to aware datetimes; ``None``/``None`` means "no override"
    (the scheduler's resume window is used).
    """
    if (window_from is None) != (window_to is None):
        msg = "Both window_from and window_to are required for a manual collection window"
        raise ValueError(msg)
    if window_from is None or window_to is None:
        return None
    time_from = _parse_task_datetime(window_from)
    time_to = _parse_task_datetime(window_to)
    if time_from >= time_to:
        msg = "window_from must be earlier than window_to"
        raise ValueError(msg)
    return time_from, time_to


def _read_event_metric_series(
    session: Session,
    *,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
) -> dict[uuid.UUID, dict[datetime, float]]:
    """Load an event-metric count series for one scope, grouped by scan grid.

    Returns ``{scan_config_id: {bucket: count}}``. An ``event_id`` scope reads
    per-event rows; an ``event_type_id`` scope reads per-type rows. Returns an
    empty mapping when neither ref is set.
    """
    if event_id is not None:
        condition = EventMetric.event_id == event_id
    elif event_type_id is not None:
        condition = EventMetric.event_type_id == event_type_id
    else:
        return {}

    series: dict[uuid.UUID, dict[datetime, float]] = {}
    rows = session.execute(
        select(EventMetric.scan_config_id, EventMetric.bucket, EventMetric.count).where(condition)
    ).all()
    for scan_config_id, bucket, count in rows:
        series.setdefault(scan_config_id, {})[bucket] = float(count)
    return series


def _composition_stored_bounds_by_grid(
    session: Session, *, metric_definition_id: uuid.UUID
) -> dict[uuid.UUID, tuple[datetime, datetime]]:
    """The ``(oldest, newest)`` bucket this composition metric has stored, per grid.

    One grouped query rather than a lookup per grid. This is the composition
    path's equivalent of ``_resolve_value_window``'s ``max(MetricValue.bucket)``:
    an event_composition metric has no interval and no watermark of its own, so
    its own stored rows are the only record of how far it has got. The MIN is the
    other half of that record — the backward frontier
    ``_composition_backfill_region`` walks down from.
    """
    rows = session.execute(
        select(
            MetricValue.scan_config_id,
            sa_func.min(MetricValue.bucket),
            sa_func.max(MetricValue.bucket),
        )
        .where(
            MetricValue.metric_definition_id == metric_definition_id,
            MetricValue.scan_config_id.is_not(None),
        )
        .group_by(MetricValue.scan_config_id)
    ).all()
    return {
        scan_config_id: (to_utc(stored_min), to_utc(stored_max))
        for scan_config_id, stored_min, stored_max in rows
        if scan_config_id is not None and stored_min is not None and stored_max is not None
    }


def _composition_series_floor(
    *, stored_max: datetime | None, head_bucket: datetime, delta: timedelta
) -> datetime:
    """Oldest bucket one event_composition run's RESUME region may reach on a grid.

    Resumes two buckets before the metric's own last stored bucket — the same
    overlap ``_resolve_value_window`` keeps for late data and for recomputing the
    most recent bucket — but never reaches further back than
    ``EVENT_COMPOSITION_BACKFILL_BUCKETS`` from the head of the source series.
    That outer bound is what makes the never-stored grid (and the grid whose
    every bucket divides by zero, which therefore never stores anything) safe.

    Clamped to ``head_bucket`` so the newest source bucket is always inside the
    region: a grid whose stored values run AHEAD of its source — history pruned
    behind them — would otherwise trim its whole series away and do nothing.

    This bound alone is a ONE-WAY RATCHET: once a value is stored, ``stored_max``
    only ever moves forward, so the region never widens and pre-history the first
    run did not reach would be out of reach for good.
    ``_composition_backfill_region`` is what un-ratchets it.
    """
    cap = head_bucket - delta * EVENT_COMPOSITION_BACKFILL_BUCKETS
    floor = cap if stored_max is None else max(stored_max - delta * 2, cap)
    return min(floor, head_bucket)


def _composition_backfill_region(
    *,
    stored_min: datetime | None,
    resume_floor: datetime,
    oldest_source: datetime,
    delta: timedelta,
) -> tuple[datetime, datetime] | None:
    """One bounded chunk of un-composed pre-history, as ``[floor, ceiling)``.

    ``None`` when there is nothing to do. Why this exists: the resume region is
    anchored on ``max(MetricValue.bucket)``, which only moves forward, so a grid
    carrying more history than ``EVENT_COMPOSITION_BACKFILL_BUCKETS`` intervals
    would be permanently truncated at whatever the first run happened to reach —
    and because ``_clear_collected_metric_data`` wipes every stored value on any
    material definition edit, editing such a metric would DESTROY the part of its
    chart the next run could not re-derive.

    So each run also walks the frontier backwards by one bounded step: at most
    ``EVENT_COMPOSITION_BACKFILL_BUCKETS`` intervals below the metric's own oldest
    stored bucket, and never below the oldest bucket the source series actually
    has. A long history is filled in over successive dispatches instead of being
    unreachable, while the work per run stays bounded by the same constant that
    bounds the first run — two bounded regions, never an unbounded re-query.

    The ceiling is clamped to ``resume_floor`` so the chunk cannot overlap the
    resume region and compose the same buckets twice in one run. Buckets BETWEEN
    the frontier and the resume floor are already composed and are still not
    revisited: this fills gaps, it does not repair a stored value whose source
    changed underneath it.

    One honest limit: the frontier is the stored MIN, and a bucket whose composed
    value is ``None`` (divide-by-zero) stores no row. A chunk in which every
    bucket divides by zero therefore leaves the frontier where it was and is
    retried on the next dispatch. That costs one bounded pass per run and cannot
    error; it is not a permanent failure.
    """
    if stored_min is None:
        # Nothing stored on this grid yet — the first-run cap in
        # ``_composition_series_floor`` owns the reach, not this.
        return None
    ceiling = min(stored_min, resume_floor)
    if oldest_source >= ceiling:
        return None
    floor = max(oldest_source, ceiling - delta * EVENT_COMPOSITION_BACKFILL_BUCKETS)
    return floor, ceiling


def _dialect_for_data_source(ds: DataSource) -> SqlDialect:
    """The SQL dialect a data source's filters/queries must be compiled for.

    A ``db_type`` with no dialect is a configuration error, and it is raised as a
    ``ScanError`` so it surfaces on the metric like every other collection failure
    rather than as an unhandled worker crash.
    """
    try:
        return dialect_for_db_type(ds.db_type)
    except ValueError as exc:
        raise ScanError(str(exc)) from exc


def _load_fact_table(
    session: Session, fact_table_id: uuid.UUID | None, *, project_id: uuid.UUID
) -> FactTable:
    """Load a bound FactTable for a fact metric or raise a ``ScanError``.

    The worker has global visibility (no project scope on ``session.get``), so
    the loaded fact table's ``project_id`` is checked against the metric's
    ``project_id`` as defence in depth against a cross-project reference.
    """
    if fact_table_id is None:
        msg = "fact metric requires a fact_table_id"
        raise ScanError(msg)
    fact_table = session.get(FactTable, fact_table_id)
    if fact_table is None:
        msg = f"FactTable {fact_table_id} for fact metric not found"
        raise ScanError(msg)
    if fact_table.project_id != project_id:
        msg = f"FactTable {fact_table_id} does not belong to project {project_id}"
        raise ScanError(msg)
    if fact_table.data_source_id is None:
        msg = f"FactTable {fact_table_id} has no data source bound"
        raise ScanError(msg)
    return fact_table


def _aggregate_fact_window(
    adapter: BaseAdapter,
    *,
    fact_table: FactTable,
    operand: _FactOperand,
    allowed_columns: set[str],
    dialect: SqlDialect,
    interval_code: str,
    chunk_from: datetime,
    chunk_to: datetime,
) -> tuple[dict[datetime, float], str, str | None]:
    """Run one fact operand's bucketed aggregate over a chunk window.

    Returns ``(values_by_bucket, base_query, validated_measure)`` so a SINGLE
    caller can reuse the base query / measure for its breakdown pass.

    ``allowed_columns`` is the caller's one-per-metric introspection of the RAW
    fact SQL. It is resolved by the caller, not here: this function runs once per
    chunk, and the guards below must also hold for a ``count`` metric, which
    needs no measure and so used to reach the warehouse with nothing validated at
    all. Mirrors ``_resolve_batch_operand`` on the batched path.
    """
    base_query = _resolve_fact_operand_query(fact_table, operand, dialect=dialect)
    measure = _fact_operand_measure(operand)
    if requires_measure(operand.aggregation):
        if measure is None:
            msg = (
                f"aggregation {operand.aggregation.value!r} requires a "
                "measure_column / distinct_column"
            )
            raise ScanError(msg)
        # Empty-allowlist rejection and the ValueError -> ScanError translation
        # both live in the shared helper, so this path and the batched
        # ``_resolve_batch_operand`` report the same named failure.
        measure = _validated_measure_column(measure, allowed_columns=allowed_columns)
    # Condition columns cleared ``validate_identifier`` when the metric was saved
    # and were checked against the fact table's columns AS THEY WERE THEN;
    # nothing rechecked them here, so a column dropped or renamed in the
    # warehouse since compiled into a query that failed deep inside the worker.
    _validate_condition_columns(operand, allowed_columns=allowed_columns)
    _cols, _json_value_names, rows = adapter.get_time_bucketed_aggregate(
        base_query,
        fact_table.timestamp_column,
        interval_code,
        operand.aggregation,
        measure,
        [],
        [],
        None,
        chunk_from,
        chunk_to,
        limit=metric_query_fetch_limit(),
    )
    rows = _reject_truncated_rows(
        rows,
        what="Fact metric aggregate",
        chunk_from=chunk_from,
        chunk_to=chunk_to,
    )
    values: dict[datetime, float] = {}
    for row in rows:
        cell = row[-1]
        if cell is None:
            # A bucket whose aggregate cell is NULL (``sum``/``avg`` over rows
            # that are all NULL for the measure) records an ABSENT bucket, the
            # same reading ``_index_multi_aggregate`` gives it on the batched
            # path — not a ``float(None)`` TypeError that fails the whole chunk.
            continue
        values[_coerce_bucket(row[0], interval_code)] = _coerce_value(cell)
    return values, base_query, measure


def _metric_breakdown_columns(definition: MetricDefinition) -> list[str]:
    """Return the metric's configured breakdown dimensions in collection order.

    Deduplicated, order preserved. The schema now refuses to SAVE a repeated
    ``breakdown_columns`` entry, but rows stored before it still carry one, and a
    column listed twice makes assembly emit each ``(bucket, breakdown_value)``
    row twice inside a single ``INSERT ... ON CONFLICT DO UPDATE``, which
    Postgres refuses with "command cannot affect row a second time" — the metric
    then errors on every tick. This guard is what protects the
    legacy rows; the ``app_version``/``platform`` extras were always deduplicated
    against the stored list, just never the stored list against itself.
    """
    breakdown_columns: list[str] = []
    seen: set[str] = set()
    extras = (definition.app_version_column, definition.platform_column)
    for column in (*(definition.breakdown_columns or []), *(extra for extra in extras if extra)):
        if column in seen:
            continue
        seen.add(column)
        breakdown_columns.append(column)
    return breakdown_columns


def _ratio_breakdown_value(
    numerator_cell: object,
    denominator_cell: object,
) -> float | None:
    """Compute one grouped ratio cell with the same zero-safe semantics as values."""
    denominator = 0.0 if denominator_cell is None else _coerce_value(denominator_cell)
    if denominator == 0:
        return None
    numerator = 0.0 if numerator_cell is None else _coerce_value(numerator_cell)
    return numerator / denominator


def _coerce_compare_bound(dt: datetime) -> datetime:
    """Normalize collection-window bounds for comparison with coerced buckets."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _append_ratio_breakdown_rows(
    rows_out: list[dict[str, object]],
    *,
    definition: MetricDefinition,
    rows: list[tuple[object, ...]],
    interval_code: str,
    time_from: datetime,
    time_to: datetime,
    breakdown_column: str,
    numerator_index: int,
    denominator_index: int,
) -> None:
    """Append finite ratio rows from one multi-aggregate breakdown result."""
    compare_from = _coerce_compare_bound(time_from)
    compare_to = _coerce_compare_bound(time_to)
    for row in rows:
        bucket = _coerce_bucket(row[0], interval_code)
        if not (compare_from <= bucket < compare_to):
            continue
        value = _ratio_breakdown_value(row[numerator_index], row[denominator_index])
        if value is None:
            continue
        rows_out.append(
            {
                "id": uuid.uuid4(),
                "metric_definition_id": definition.id,
                "scan_config_id": None,
                "bucket": bucket,
                "breakdown_column": breakdown_column,
                "breakdown_value": str(row[1])[:MAX_BREAKDOWN_VALUE_LENGTH],
                "is_other": bool(row[2]),
                "value": value,
            }
        )


def _collect_fact_breakdown_rows(
    session: Session,
    *,
    adapter: BaseAdapter,
    definition: MetricDefinition,
    base_query: str,
    time_column: str,
    interval_code: str,
    agg: MetricAggregation,
    measure_column: str | None,
    allowed_columns: set[str],
    chunk_from: datetime,
    chunk_to: datetime,
) -> int:
    """Collect per-dimension breakdown values for a single fact-metric chunk.

    Each configured breakdown column (plus the optional app-version / platform
    columns) runs one ``get_time_bucketed_aggregate_breakdown`` query. Rows are
    window-deleted then UPSERTed so re-runs do not duplicate. Returns the number
    of breakdown rows written.

    The breakdown dimensions are held to the caller's ``allowed_columns``. Nothing
    validates them against the fact table when the metric is SAVED — see
    ``_validate_breakdown_columns`` — and a ``count`` metric needs no measure, so
    nothing on this path had ever introspected the table at all.
    """
    breakdown_columns = _metric_breakdown_columns(definition)
    if not breakdown_columns:
        return 0
    _validate_breakdown_columns(breakdown_columns, allowed_columns=allowed_columns)

    rows_out: list[dict[str, object]] = []
    for column in breakdown_columns:
        _cols, _json_value_names, rows = adapter.get_time_bucketed_aggregate_breakdown(
            base_query,
            time_column,
            interval_code,
            agg,
            measure_column,
            column,
            [column],
            [],
            None,
            chunk_from,
            chunk_to,
            values_limit=definition.breakdown_values_limit,
            limit=metric_query_fetch_limit(),
        )
        rows = _reject_truncated_rows(
            rows,
            what=f"Fact metric breakdown {column!r}",
            chunk_from=chunk_from,
            chunk_to=chunk_to,
        )
        for row in rows:
            cell = row[-1]
            if cell is None:
                # An all-NULL group records an absent value, exactly as the
                # batched breakdown pass does (``_assemble_single_metric``).
                continue
            rows_out.append(
                {
                    "id": uuid.uuid4(),
                    "metric_definition_id": definition.id,
                    "scan_config_id": None,
                    "bucket": _coerce_bucket(row[0], interval_code),
                    "breakdown_column": column,
                    "breakdown_value": str(row[1])[:MAX_BREAKDOWN_VALUE_LENGTH],
                    "is_other": bool(row[2]),
                    "value": _coerce_value(cell),
                }
            )

    _delete_metric_value_breakdowns_window(
        session,
        metric_definition_id=definition.id,
        time_from=chunk_from,
        time_to=chunk_to,
    )
    # The upsert's own return, not ``len(rows_out)``: it drops non-finite values
    # first, and the count the task reports has to be what was stored.
    return _upsert_metric_value_breakdown_rows(session, rows=rows_out)


def _collect_fact_ratio_breakdown_rows(
    session: Session,
    *,
    adapter: BaseAdapter,
    definition: MetricDefinition,
    fact_table: FactTable,
    numerator_op: _FactOperand,
    denominator_op: _FactOperand,
    dialect: SqlDialect,
    interval_code: str,
    chunk_from: datetime,
    chunk_to: datetime,
) -> int:
    """Collect same-table ratio breakdown values for one fact-metric chunk.

    Holds the breakdown dimensions to the fact table's columns for the same reason
    ``_collect_fact_breakdown_rows`` does; the guard belongs on every path that turns
    a configured dimension into a warehouse query, not just the SINGLE one.
    """
    breakdown_columns = _metric_breakdown_columns(definition)
    if not breakdown_columns:
        return 0

    allowed_columns = {column.name for column in adapter.get_columns(fact_table.sql)}
    _validate_breakdown_columns(breakdown_columns, allowed_columns=allowed_columns)
    numerator_measure, numerator_filter = _resolve_batch_operand(
        numerator_op,
        fact_table=fact_table,
        allowed_columns=allowed_columns,
        dialect=dialect,
    )
    denominator_measure, denominator_filter = _resolve_batch_operand(
        denominator_op,
        fact_table=fact_table,
        allowed_columns=allowed_columns,
        dialect=dialect,
    )

    rows_out: list[dict[str, object]] = []
    for column in breakdown_columns:
        registry = _SpecRegistry()
        numerator_key = registry.register(
            aggregation=numerator_op.aggregation,
            measure=numerator_measure,
            filter_sql=numerator_filter,
        )
        denominator_key = registry.register(
            aggregation=denominator_op.aggregation,
            measure=denominator_measure,
            filter_sql=denominator_filter,
        )
        col_names, rows = adapter.get_time_bucketed_multi_aggregate_breakdown(
            fact_table.sql,
            fact_table.timestamp_column,
            interval_code,
            column,
            registry.specs,
            chunk_from,
            chunk_to,
            values_limit=definition.breakdown_values_limit,
            limit=metric_query_fetch_limit(),
        )
        rows = _reject_truncated_rows(
            rows,
            what=f"Ratio metric breakdown {column!r}",
            chunk_from=chunk_from,
            chunk_to=chunk_to,
        )
        index_by_name = {name: index for index, name in enumerate(col_names)}
        _append_ratio_breakdown_rows(
            rows_out,
            definition=definition,
            rows=list(rows),
            interval_code=interval_code,
            time_from=chunk_from,
            time_to=chunk_to,
            breakdown_column=column,
            numerator_index=index_by_name[numerator_key],
            denominator_index=index_by_name[denominator_key],
        )

    _delete_metric_value_breakdowns_window(
        session,
        metric_definition_id=definition.id,
        time_from=chunk_from,
        time_to=chunk_to,
    )
    # The upsert's own return, not ``len(rows_out)``: it drops non-finite values
    # first, and the count the task reports has to be what was stored.
    return _upsert_metric_value_breakdown_rows(session, rows=rows_out)


def _resolve_fact_composition(definition: MetricDefinition) -> MetricComposition:
    """Coerce a fact metric's composition, defaulting an unset value to single."""
    if definition.composition is None:
        return MetricComposition.single
    if isinstance(definition.composition, MetricComposition):
        return definition.composition
    return MetricComposition(definition.composition)


def _collect_fact(
    session: Session,
    *,
    definition: MetricDefinition,
    window: tuple[datetime, datetime] | None = None,
    manual_backfill: bool = False,
) -> dict[str, object]:
    """Collect a ``fact`` metric: an aggregation over a FactTable per bucket.

    SINGLE collects one operand series (+ optional per-dimension breakdowns).
    RATIO divides a numerator series by a denominator series (each over a —
    possibly different — FactTable); a zero/absent denominator maps to ``None``
    (divide-by-zero), which the NOT-NULL row builder drops. ``window`` overrides
    the resume window for a manual backfill; ``manual_backfill`` marks that
    window a "collect now" slice, which may only reach FURTHER BACK than the
    metric's own resume point (see ``_effective_value_window``).
    """
    if definition.interval is None:
        msg = "fact metric requires an interval"
        raise ScanError(msg)
    interval_spec = get_interval(definition.interval)
    delta = interval_spec.delta
    time_from, time_to = _effective_value_window(
        session,
        metric_definition_id=definition.id,
        interval_code=interval_spec.code,
        window_override=window,
        manual_backfill=manual_backfill,
    )
    composition = _resolve_fact_composition(definition)
    if composition is MetricComposition.ratio:
        summary = _collect_fact_ratio(
            session,
            definition=definition,
            interval_spec=interval_spec,
            delta=delta,
            time_from=time_from,
            time_to=time_to,
        )
    else:
        summary = _collect_fact_single(
            session,
            definition=definition,
            interval_spec=interval_spec,
            delta=delta,
            time_from=time_from,
            time_to=time_to,
        )
    # Private orchestration metadata: the task removes it before returning its
    # public summary and persists it as source-grid progress even for zero rows.
    summary["_collection_window_to"] = time_to
    return summary


def _collect_fact_single(
    session: Session,
    *,
    definition: MetricDefinition,
    interval_spec: IntervalSpec,
    delta: timedelta,
    time_from: datetime,
    time_to: datetime,
) -> dict[str, object]:
    """Collect a single-operand fact metric (agg(measure) per bucket + breakdowns)."""
    if definition.aggregation is None:
        msg = "single fact metric requires an aggregation"
        raise ScanError(msg)
    config = definition.config or {}
    fact_table = _load_fact_table(
        session, definition.fact_table_id, project_id=definition.project_id
    )
    operand = _FactOperand(
        fact_table_id=fact_table.id,
        aggregation=coerce_aggregation(definition.aggregation),
        measure_column=_config_str(config, "measure_column"),
        distinct_column=_config_str(config, "distinct_column"),
        row_filters=_effective_filter_names(config),
        filter_sql=_config_str(config, "filter_sql"),
        conditions=_conditions_from_config(config),
    )
    ds = session.get(DataSource, fact_table.data_source_id)
    if ds is None:
        msg = "DataSource for fact metric not found"
        raise ScanError(msg)
    # ``_load_fact_table`` scoped the fact TABLE to this project; this scopes the
    # warehouse credential behind it, which nothing else on this path does.
    _reject_foreign_data_source(session, project_id=definition.project_id, data_source=ds)
    interval_code = interval_spec.code
    dialect = _dialect_for_data_source(ds)

    adapter = _build_adapter(ds)
    total_values = 0
    total_breakdowns = 0
    try:
        adapter.test_connection()
        # One introspection per metric, taken from the RAW fact SQL rather than
        # the operand's filtered wrapper: a condition column is compiled INTO
        # that wrapper's WHERE clause, so probing the wrapper would fail at the
        # warehouse before ``_validate_condition_columns`` could name the column.
        # It is the same source the batched path introspects, and the call also
        # arms the adapter's own ``_validate_column`` membership check, which a
        # ``count`` metric's breakdown query has no other way to get.
        allowed_columns = {column.name for column in adapter.get_columns(fact_table.sql)}
        chunks = _iter_window_chunks(
            time_from,
            time_to,
            interval_delta=delta,
            chunk_interval_code=definition.replay_chunk_interval,
        )
        # Rank top-N breakdown values once over the whole window.
        # That pre-query is not chunked: it is the one statement spanning it.
        with rank_top_n_once(adapter, time_from, time_to):
            for chunk_from, chunk_to in chunks:
                values, base_query, measure = _aggregate_fact_window(
                    adapter,
                    fact_table=fact_table,
                    operand=operand,
                    allowed_columns=allowed_columns,
                    dialect=dialect,
                    interval_code=interval_code,
                    chunk_from=chunk_from,
                    chunk_to=chunk_to,
                )
                value_rows = _build_metric_value_rows(
                    metric_definition_id=definition.id,
                    scan_config_id=None,
                    values=values,
                )
                _delete_metric_values_window(
                    session,
                    metric_definition_id=definition.id,
                    time_from=chunk_from,
                    time_to=chunk_to,
                )
                total_values += _upsert_metric_values_rows(session, rows=value_rows)
                total_breakdowns += _collect_fact_breakdown_rows(
                    session,
                    adapter=adapter,
                    definition=definition,
                    base_query=base_query,
                    time_column=fact_table.timestamp_column,
                    interval_code=interval_code,
                    agg=operand.aggregation,
                    measure_column=measure,
                    allowed_columns=allowed_columns,
                    chunk_from=chunk_from,
                    chunk_to=chunk_to,
                )
                session.commit()
    finally:
        adapter.close()

    return {"values": total_values, "breakdown_values": total_breakdowns}


def _collect_fact_ratio(
    session: Session,
    *,
    definition: MetricDefinition,
    interval_spec: IntervalSpec,
    delta: timedelta,
    time_from: datetime,
    time_to: datetime,
) -> dict[str, object]:
    """Collect a ratio fact metric: numerator / denominator per bucket.

    Each operand may reference a different FactTable / data source, so a separate
    adapter is built per operand. The two series are divided via
    ``evaluate_composition``; divide-by-zero buckets map to ``None`` and are
    dropped by the NOT-NULL row builder. Breakdown rows are supported for the
    same-table case: both operand aggregates are sliced by the same dimension
    values in one grouped query, then divided per ``(bucket, value)``.
    """
    config = definition.config or {}
    numerator_raw = config.get("numerator")
    denominator_raw = config.get("denominator")
    if not isinstance(numerator_raw, Mapping) or not isinstance(denominator_raw, Mapping):
        msg = "ratio fact metric requires numerator and denominator operands in config"
        raise ScanError(msg)
    numerator_op = _operand_from_config(numerator_raw)
    denominator_op = _operand_from_config(denominator_raw)
    numerator_ft = _load_fact_table(
        session, numerator_op.fact_table_id, project_id=definition.project_id
    )
    denominator_ft = _load_fact_table(
        session, denominator_op.fact_table_id, project_id=definition.project_id
    )
    numerator_ds = session.get(DataSource, numerator_ft.data_source_id)
    denominator_ds = session.get(DataSource, denominator_ft.data_source_id)
    if numerator_ds is None or denominator_ds is None:
        msg = "DataSource for fact ratio operand not found"
        raise ScanError(msg)
    # Both operands, because they may sit on different warehouses and either one
    # is a credential this collection is about to open.
    for operand_ds in (numerator_ds, denominator_ds):
        _reject_foreign_data_source(
            session, project_id=definition.project_id, data_source=operand_ds
        )
    breakdown_columns = _metric_breakdown_columns(definition)
    if breakdown_columns and numerator_ft.id != denominator_ft.id:
        msg = (
            "ratio fact metric breakdowns require numerator and denominator "
            "to use the same fact table"
        )
        raise ScanError(msg)
    interval_code = interval_spec.code
    numerator_dialect = _dialect_for_data_source(numerator_ds)
    denominator_dialect = _dialect_for_data_source(denominator_ds)

    numerator_adapter = _build_adapter(numerator_ds)
    total_values = 0
    total_breakdowns = 0
    try:
        denominator_adapter = _build_adapter(denominator_ds)
        try:
            numerator_adapter.test_connection()
            denominator_adapter.test_connection()
            # One introspection per operand, from the RAW fact SQL — see
            # ``_collect_fact_single``. Each operand gets its own, because the two
            # may reference different fact tables on different data sources; and
            # each adapter needs its own ``_allowed_columns`` armed regardless,
            # since they are separate instances even for a same-table ratio.
            numerator_allowed = {
                column.name for column in numerator_adapter.get_columns(numerator_ft.sql)
            }
            denominator_allowed = {
                column.name for column in denominator_adapter.get_columns(denominator_ft.sql)
            }
            chunks = _iter_window_chunks(
                time_from,
                time_to,
                interval_delta=delta,
                chunk_interval_code=definition.replay_chunk_interval,
            )
            # Rank top-N breakdown values once over the whole window;
            # only the numerator adapter serves the ratio breakdown pass. That
            # pre-query is not chunked: it is the one statement spanning it.
            with rank_top_n_once(numerator_adapter, time_from, time_to):
                for chunk_from, chunk_to in chunks:
                    numerator_values, _nq, _nm = _aggregate_fact_window(
                        numerator_adapter,
                        fact_table=numerator_ft,
                        operand=numerator_op,
                        allowed_columns=numerator_allowed,
                        dialect=numerator_dialect,
                        interval_code=interval_code,
                        chunk_from=chunk_from,
                        chunk_to=chunk_to,
                    )
                    denominator_values, _dq, _dm = _aggregate_fact_window(
                        denominator_adapter,
                        fact_table=denominator_ft,
                        operand=denominator_op,
                        allowed_columns=denominator_allowed,
                        dialect=denominator_dialect,
                        interval_code=interval_code,
                        chunk_from=chunk_from,
                        chunk_to=chunk_to,
                    )
                    values = evaluate_composition(
                        MetricComposition.ratio,
                        numerator=numerator_values,
                        denominator=denominator_values,
                    )
                    value_rows = _build_metric_value_rows(
                        metric_definition_id=definition.id,
                        scan_config_id=None,
                        values=values,
                    )
                    _delete_metric_values_window(
                        session,
                        metric_definition_id=definition.id,
                        time_from=chunk_from,
                        time_to=chunk_to,
                    )
                    total_values += _upsert_metric_values_rows(session, rows=value_rows)
                    if breakdown_columns:
                        total_breakdowns += _collect_fact_ratio_breakdown_rows(
                            session,
                            adapter=numerator_adapter,
                            definition=definition,
                            fact_table=numerator_ft,
                            numerator_op=numerator_op,
                            denominator_op=denominator_op,
                            dialect=numerator_dialect,
                            interval_code=interval_code,
                            chunk_from=chunk_from,
                            chunk_to=chunk_to,
                        )
                    session.commit()
        finally:
            denominator_adapter.close()
    finally:
        numerator_adapter.close()

    return {"values": total_values, "breakdown_values": total_breakdowns}


# ── batched fact collection ──────────────────────────────────────────────────
#
# The per-metric path above runs one warehouse query per metric (per operand for
# a ratio, plus one per breakdown column). The batched path below collapses every
# fact metric of a fact table into ONE multi-aggregate scan (plus one scan per
# distinct breakdown dimension), turning each per-metric row filter into a
# per-aggregate conditional so metrics with different filters still share one
# scan. The per-bucket VALUES are identical to the per-metric path: an unfiltered
# aggregate over the fact SQL equals the single-aggregate path, and a conditional
# aggregate equals the same aggregate over the row-filtered subquery.


# A spec's dedup identity: same aggregation, same measure/distinct column and
# the same combined filter expression aggregate to the SAME warehouse column, so
# two metrics needing the same aggregate share one column in the scan. The
# combined filter string (built deterministically by ``_resolve_combined_filter``)
# is the true identity, so equal effective filters — whatever named/free-text mix
# produced them — dedup to one column.
_SpecDedupKey = tuple[MetricAggregation, str | None, str | None]


@dataclass
class _SpecRegistry:
    """Dedup'd ``AggregateSpec`` list for one scan (a fact table or a breakdown).

    ``register`` returns the stable column alias key for an operand, reusing the
    same key (and column) for operands that share aggregation / measure / filter.
    """

    specs: list[AggregateSpec] = field(default_factory=list)
    _keys: dict[_SpecDedupKey, str] = field(default_factory=dict)

    def register(
        self,
        *,
        aggregation: MetricAggregation,
        measure: str | None,
        filter_sql: str | None,
    ) -> str:
        dedup: _SpecDedupKey = (aggregation, measure, filter_sql)
        key = self._keys.get(dedup)
        if key is None:
            key = f"k{len(self.specs)}"
            self._keys[dedup] = key
            self.specs.append(
                AggregateSpec(
                    key=key,
                    aggregation=aggregation,
                    column=measure,
                    filter_sql=filter_sql,
                )
            )
        return key


@dataclass(frozen=True)
class _BreakdownPlan:
    """One breakdown dimension a single metric reads from a shared scan."""

    column: str
    values_limit: int | None
    spec_key: str


@dataclass(frozen=True)
class _RatioBreakdownPlan:
    """One breakdown dimension a ratio metric reads from a shared scan."""

    column: str
    values_limit: int | None
    numerator_key: str
    denominator_key: str


@dataclass(frozen=True)
class _SingleMetricPlan:
    definition: MetricDefinition
    fact_table_id: uuid.UUID
    spec_key: str
    window: tuple[datetime, datetime]
    breakdowns: tuple[_BreakdownPlan, ...]


@dataclass(frozen=True)
class _RatioMetricPlan:
    definition: MetricDefinition
    numerator_fact_table_id: uuid.UUID
    numerator_key: str
    denominator_fact_table_id: uuid.UUID
    denominator_key: str
    window: tuple[datetime, datetime]
    breakdowns: tuple[_RatioBreakdownPlan, ...]


# A breakdown scan is keyed by (fact table, column, values_limit, ranking window):
# the top-N "Other" rollup depends on values_limit, so metrics that share a column
# but not a limit cannot share one scan (different rollups). A limited rollup also
# depends on the window its values are ranked over, which is the metric's OWN
# collection window — the per-metric collectors rank there too — so limited
# metrics share a scan only when their windows match. An
# unlimited breakdown ranks nothing and keeps ``None`` there, sharing one scan
# across windows as before. Everything else (the per-spec aggregate columns) is
# layered on top of the shared GROUP BY.
_BreakdownScanKey = tuple[uuid.UUID, str, int | None, tuple[datetime, datetime] | None]


def _breakdown_scan_key(
    fact_table_id: uuid.UUID,
    column: str,
    values_limit: int | None,
    window: tuple[datetime, datetime],
) -> _BreakdownScanKey:
    """The shared breakdown scan a metric's ``column`` breakdown reads from."""
    return (fact_table_id, column, values_limit, window if values_limit is not None else None)


def _index_multi_aggregate(
    col_names: list[str], rows: list[tuple[object, ...]], interval_code: str
) -> dict[str, dict[datetime, float]]:
    """Index a multi-aggregate result into ``{spec_key: {bucket: value}}``.

    ``col_names`` is ``["bucket", key1, key2, ...]``. ``NULL`` cells (a
    conditional aggregate over an empty group, e.g. ``avgIf`` with no matching
    rows) are skipped so the bucket reads as absent for that key — matching the
    per-metric path, where a filtered scan would not emit that bucket at all.
    """
    out: dict[str, dict[datetime, float]] = {name: {} for name in col_names[1:]}
    for row in rows:
        bucket = _coerce_bucket(row[0], interval_code)
        for index, name in enumerate(col_names[1:], start=1):
            cell = row[index]
            if cell is None:
                continue
            out[name][bucket] = _coerce_value(cell)
    return out


def _clip_series(
    series: Mapping[datetime, float], window: tuple[datetime, datetime]
) -> dict[datetime, float]:
    """Restrict a per-bucket series to a metric's own ``[from, to)`` window."""
    time_from, time_to = window
    return {bucket: value for bucket, value in series.items() if time_from <= bucket < time_to}


def _merge_multi_aggregate(
    dst: dict[str, dict[datetime, float]], src: Mapping[str, dict[datetime, float]]
) -> None:
    """Merge one chunk's ``{spec_key: {bucket: value}}`` into the accumulator.

    Chunks are interval-aligned (see ``_iter_window_chunks``), so a bucket never
    straddles two chunks and the merged series is IDENTICAL to what a single
    covering scan would have produced — value-identity is preserved.
    """
    for key, bucket_values in src.items():
        dst.setdefault(key, {}).update(bucket_values)


def _batch_chunk_interval_code(definitions: list[MetricDefinition]) -> str | None:
    """Smallest ``replay_chunk_interval`` configured across the group's metrics.

    The batch's covering window is chunked by the most aggressive (smallest)
    chunk any member requested, so no member's warehouse scan ends up wider than
    it would have been on the per-metric path. ``None`` (no member configures
    chunking) keeps the legacy single covering query. Unknown codes are ignored
    so one metric's bad config never aborts the whole group.
    """
    codes = {definition.replay_chunk_interval for definition in definitions}
    deltas: list[tuple[str, timedelta]] = []
    for code in codes:
        if not code:
            continue
        try:
            deltas.append((code, get_interval(code).delta))
        except ValueError:
            logger.warning("Ignoring unknown replay_chunk_interval %r in batch", code)
    if not deltas:
        return None
    return min(deltas, key=lambda item: item[1])[0]


def _stamp_metric_success(
    session: Session,
    definition: MetricDefinition,
    *,
    window_to: datetime | None = None,
) -> None:
    """Mark a metric successful and persist its source-window watermark."""
    definition.last_collected_at = datetime.now(UTC)
    if window_to is not None and (
        definition.last_collection_window_to is None
        or to_utc(window_to) > to_utc(definition.last_collection_window_to)
    ):
        definition.last_collection_window_to = window_to
    definition.last_collection_status = COLLECTION_STATUS_SUCCESS
    definition.last_collection_error = None
    session.commit()


def _stamp_batch_dependency_error(session: Session, definition: MetricDefinition) -> None:
    """Report a sibling's batch failure on the clicked metric WITHOUT dropping its data.

    The clicked metric is what the UI's collection watcher polls, so a partially
    failed shared batch has to surface through it. Its own scan succeeded and was
    already committed by ``_stamp_metric_success``; routing this through
    ``_stamp_metric_error`` would roll that commit's successor transaction back
    and discard correct rows because an unrelated metric failed. Overwrite the
    status only, leaving the collected values and the watermark in place.
    """
    mark_collection_error(
        definition, "One or more dependent metrics failed during the shared collection batch"
    )
    session.commit()


def _stamp_metric_error(session: Session, definition: MetricDefinition, exc: Exception) -> None:
    """Roll back any partial writes for one metric and persist a sanitized error."""
    try:
        session.rollback()
        mark_collection_error(definition, user_facing_error(exc))
        session.commit()
    except Exception:  # pragma: no cover - best-effort status write
        session.rollback()


@dataclass
class _FactBatchContext:
    """Lazily-built fact-table / data-source / adapter caches for one batch."""

    session: Session
    #: Fact tables that already passed ``_load_fact_table``'s project scope for
    #: the metric that resolved them. Keyed by id alone, which is sound only
    #: because :meth:`resolve` re-applies the scope on every cache HIT: one batch
    #: mixes projects (``schedule.check_metric_definitions_due`` groups fact
    #: metrics by interval alone), so an entry cached under project A used to be
    #: served to project B's metric past the check that would refuse it
    #:. The out-of-resolve readers in
    #: ``_run_fact_interval_group`` only index ids a successful ``resolve``
    #: registered, so every entry they read has already been scoped.
    fact_tables: dict[uuid.UUID, FactTable] = field(default_factory=dict)
    adapters: dict[uuid.UUID, BaseAdapter] = field(default_factory=dict)
    columns: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    dialects: dict[uuid.UUID, SqlDialect] = field(default_factory=dict)
    #: ``(data_source_id, project_id)`` pairs already cleared by
    #: ``_reject_foreign_data_source``. Keyed by BOTH because one batch mixes
    #: projects: ``check_metric_definitions_due`` groups the fact metrics it
    #: dispatches by interval alone, so an adapter this project legitimately
    #: opened is sitting in ``adapters`` when the next project's metric asks for
    #: the same source. A per-source-only memo would let that second project
    #: inherit the first one's verdict.
    scoped: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)

    def resolve(
        self, fact_table_id: uuid.UUID | None, *, project_id: uuid.UUID
    ) -> tuple[FactTable, BaseAdapter]:
        fact_table = self.fact_tables.get(fact_table_id) if fact_table_id else None
        # A cached table is reused only for its own project; for any other the
        # load runs again and ``_load_fact_table`` refuses it.
        if fact_table is None or fact_table.project_id != project_id:
            fact_table = _load_fact_table(self.session, fact_table_id, project_id=project_id)
            self.fact_tables[fact_table.id] = fact_table
        # ``_load_fact_table`` rejects a fact table without a bound data source.
        data_source_id = fact_table.data_source_id
        assert data_source_id is not None
        adapter = self.adapters.get(data_source_id)
        scope_key = (data_source_id, project_id)
        if adapter is None or scope_key not in self.scoped:
            ds = self.session.get(DataSource, data_source_id)
            if ds is None:
                msg = "DataSource for fact metric not found"
                raise ScanError(msg)
            # Before the credential is opened, and again for a project that has
            # not been cleared for this source yet — see ``scoped`` above. The
            # repeat ``session.get`` costs no query: the row is already in the
            # session's identity map.
            _reject_foreign_data_source(self.session, project_id=project_id, data_source=ds)
            self.scoped.add(scope_key)
            if adapter is None:
                adapter = _build_adapter(ds)
                adapter.test_connection()
                self.adapters[data_source_id] = adapter
                # Cached under the same key as the adapter: the dialect a filter must be
                # compiled for is a property of the data source, not of the fact table.
                self.dialects[data_source_id] = _dialect_for_data_source(ds)
        return fact_table, adapter

    def adapter_for(self, fact_table: FactTable) -> BaseAdapter:
        """Return the cached adapter that serves ``fact_table``'s data source."""
        data_source_id = fact_table.data_source_id
        assert data_source_id is not None
        return self.adapters[data_source_id]

    def dialect_for(self, fact_table: FactTable) -> SqlDialect:
        """Return the SQL dialect ``fact_table``'s data source must be compiled for."""
        data_source_id = fact_table.data_source_id
        assert data_source_id is not None
        return self.dialects[data_source_id]

    def allowed_columns(self, fact_table: FactTable, adapter: BaseAdapter) -> set[str]:
        cached = self.columns.get(fact_table.id)
        if cached is None:
            cached = {column.name for column in adapter.get_columns(fact_table.sql)}
            self.columns[fact_table.id] = cached
        return cached

    def close(self) -> None:
        for adapter in self.adapters.values():
            adapter.close()


def _plan_single_metric(
    context: _FactBatchContext,
    *,
    definition: MetricDefinition,
    window: tuple[datetime, datetime],
    ft_registries: dict[uuid.UUID, _SpecRegistry],
    bd_registries: dict[_BreakdownScanKey, _SpecRegistry],
) -> _SingleMetricPlan:
    """Resolve a single-operand fact metric into a shared-scan plan."""
    if definition.aggregation is None:
        msg = "single fact metric requires an aggregation"
        raise ScanError(msg)
    config = definition.config or {}
    fact_table, adapter = context.resolve(
        definition.fact_table_id, project_id=definition.project_id
    )
    operand = _FactOperand(
        fact_table_id=fact_table.id,
        aggregation=coerce_aggregation(definition.aggregation),
        measure_column=_config_str(config, "measure_column"),
        distinct_column=_config_str(config, "distinct_column"),
        row_filters=_effective_filter_names(config),
        filter_sql=_config_str(config, "filter_sql"),
        conditions=_conditions_from_config(config),
    )
    allowed_columns = context.allowed_columns(fact_table, adapter)
    measure, filter_sql = _resolve_batch_operand(
        operand,
        fact_table=fact_table,
        allowed_columns=allowed_columns,
        dialect=context.dialect_for(fact_table),
    )
    registry = ft_registries.setdefault(fact_table.id, _SpecRegistry())
    spec_key = registry.register(
        aggregation=operand.aggregation,
        measure=measure,
        filter_sql=filter_sql,
    )

    breakdown_columns = _metric_breakdown_columns(definition)
    # This is the LIVE fact path (``collect_fact_metrics_batch``); the per-metric
    # collectors are kept only as the conformance oracle. Without this the unknown
    # dimension is caught by the adapter's ``_validate_column`` as a bare
    # ``ValueError``, which ``_stamp_metric_error`` persists as the generic
    # "Scan failed due to an internal error." and the metric card names nothing.
    _validate_breakdown_columns(breakdown_columns, allowed_columns=allowed_columns)
    breakdowns: list[_BreakdownPlan] = []
    for column in breakdown_columns:
        scan_key = _breakdown_scan_key(
            fact_table.id, column, definition.breakdown_values_limit, window
        )
        bd_registry = bd_registries.setdefault(scan_key, _SpecRegistry())
        bd_key = bd_registry.register(
            aggregation=operand.aggregation,
            measure=measure,
            filter_sql=filter_sql,
        )
        breakdowns.append(
            _BreakdownPlan(
                column=column,
                values_limit=definition.breakdown_values_limit,
                spec_key=bd_key,
            )
        )

    return _SingleMetricPlan(
        definition=definition,
        fact_table_id=fact_table.id,
        spec_key=spec_key,
        window=window,
        breakdowns=tuple(breakdowns),
    )


def _plan_ratio_metric(
    context: _FactBatchContext,
    *,
    definition: MetricDefinition,
    window: tuple[datetime, datetime],
    ft_registries: dict[uuid.UUID, _SpecRegistry],
    bd_registries: dict[_BreakdownScanKey, _SpecRegistry],
) -> _RatioMetricPlan:
    """Resolve a ratio fact metric's two operands into shared-scan plans."""
    config = definition.config or {}
    numerator_raw = config.get("numerator")
    denominator_raw = config.get("denominator")
    if not isinstance(numerator_raw, Mapping) or not isinstance(denominator_raw, Mapping):
        msg = "ratio fact metric requires numerator and denominator operands in config"
        raise ScanError(msg)
    keys: list[tuple[uuid.UUID, str]] = []
    operand_plans: list[tuple[_FactOperand, uuid.UUID, str | None, str | None]] = []
    allowed_by_fact_table: dict[uuid.UUID, set[str]] = {}
    for raw in (numerator_raw, denominator_raw):
        operand = _operand_from_config(raw)
        fact_table, adapter = context.resolve(
            operand.fact_table_id, project_id=definition.project_id
        )
        allowed_columns = context.allowed_columns(fact_table, adapter)
        allowed_by_fact_table[fact_table.id] = allowed_columns
        measure, filter_sql = _resolve_batch_operand(
            operand,
            fact_table=fact_table,
            allowed_columns=allowed_columns,
            dialect=context.dialect_for(fact_table),
        )
        registry = ft_registries.setdefault(fact_table.id, _SpecRegistry())
        spec_key = registry.register(
            aggregation=operand.aggregation,
            measure=measure,
            filter_sql=filter_sql,
        )
        keys.append((fact_table.id, spec_key))
        operand_plans.append((operand, fact_table.id, measure, filter_sql))
    (numerator_ft_id, numerator_key), (denominator_ft_id, denominator_key) = keys

    ratio_breakdowns: list[_RatioBreakdownPlan] = []
    breakdown_columns = _metric_breakdown_columns(definition)
    if breakdown_columns:
        if numerator_ft_id != denominator_ft_id:
            msg = (
                "ratio fact metric breakdowns require numerator and denominator "
                "to use the same fact table"
            )
            raise ScanError(msg)
        # Same guard as ``_plan_single_metric``: a dimension the shared fact table
        # does not project must fail by name here, not as a bare adapter
        # ``ValueError`` that reaches the operator as an internal error.
        _validate_breakdown_columns(
            breakdown_columns, allowed_columns=allowed_by_fact_table[numerator_ft_id]
        )
        numerator_op, _num_ft_id, numerator_measure, numerator_filter = operand_plans[0]
        denominator_op, _den_ft_id, denominator_measure, denominator_filter = operand_plans[1]
        for column in breakdown_columns:
            scan_key = _breakdown_scan_key(
                numerator_ft_id, column, definition.breakdown_values_limit, window
            )
            bd_registry = bd_registries.setdefault(scan_key, _SpecRegistry())
            numerator_bd_key = bd_registry.register(
                aggregation=numerator_op.aggregation,
                measure=numerator_measure,
                filter_sql=numerator_filter,
            )
            denominator_bd_key = bd_registry.register(
                aggregation=denominator_op.aggregation,
                measure=denominator_measure,
                filter_sql=denominator_filter,
            )
            ratio_breakdowns.append(
                _RatioBreakdownPlan(
                    column=column,
                    values_limit=definition.breakdown_values_limit,
                    numerator_key=numerator_bd_key,
                    denominator_key=denominator_bd_key,
                )
            )

    return _RatioMetricPlan(
        definition=definition,
        numerator_fact_table_id=numerator_ft_id,
        numerator_key=numerator_key,
        denominator_fact_table_id=denominator_ft_id,
        denominator_key=denominator_key,
        window=window,
        breakdowns=tuple(ratio_breakdowns),
    )


def _assemble_single_metric(
    session: Session,
    *,
    plan: _SingleMetricPlan,
    results: Mapping[uuid.UUID, Mapping[str, dict[datetime, float]]],
    breakdown_results: Mapping[_BreakdownScanKey, tuple[dict[str, int], list[tuple[object, ...]]]],
    interval_code: str,
) -> tuple[int, int]:
    """Write one single metric's values + breakdowns from the shared scan results.

    Returns ``(values_written, breakdown_values_written)``.
    """
    definition = plan.definition
    series = _clip_series(results.get(plan.fact_table_id, {}).get(plan.spec_key, {}), plan.window)
    value_rows = _build_metric_value_rows(
        metric_definition_id=definition.id,
        scan_config_id=None,
        values=series,
    )
    time_from, time_to = plan.window
    _delete_metric_values_window(
        session,
        metric_definition_id=definition.id,
        time_from=time_from,
        time_to=time_to,
    )
    values_written = _upsert_metric_values_rows(session, rows=value_rows)

    breakdown_rows: list[dict[str, object]] = []
    for breakdown in plan.breakdowns:
        scan_key = _breakdown_scan_key(
            plan.fact_table_id, breakdown.column, breakdown.values_limit, plan.window
        )
        entry = breakdown_results.get(scan_key)
        if entry is None:
            continue
        index_by_key, rows = entry
        value_index = index_by_key[breakdown.spec_key]
        for row in rows:
            bucket = _coerce_bucket(row[0], interval_code)
            if not (time_from <= bucket < time_to):
                continue
            cell = row[value_index]
            if cell is None:
                continue
            breakdown_rows.append(
                {
                    "id": uuid.uuid4(),
                    "metric_definition_id": definition.id,
                    "scan_config_id": None,
                    "bucket": bucket,
                    "breakdown_column": breakdown.column,
                    "breakdown_value": str(row[1])[:MAX_BREAKDOWN_VALUE_LENGTH],
                    "is_other": bool(row[2]),
                    "value": _coerce_value(cell),
                }
            )

    breakdowns_written = 0
    if plan.breakdowns:
        _delete_metric_value_breakdowns_window(
            session,
            metric_definition_id=definition.id,
            time_from=time_from,
            time_to=time_to,
        )
        breakdowns_written = _upsert_metric_value_breakdown_rows(session, rows=breakdown_rows)

    return values_written, breakdowns_written


def _assemble_ratio_metric(
    session: Session,
    *,
    plan: _RatioMetricPlan,
    results: Mapping[uuid.UUID, Mapping[str, dict[datetime, float]]],
    breakdown_results: Mapping[_BreakdownScanKey, tuple[dict[str, int], list[tuple[object, ...]]]],
    interval_code: str,
) -> tuple[int, int]:
    """Write one ratio metric's values + same-table breakdowns from shared scans."""
    definition = plan.definition
    numerator = _clip_series(
        results.get(plan.numerator_fact_table_id, {}).get(plan.numerator_key, {}), plan.window
    )
    denominator = _clip_series(
        results.get(plan.denominator_fact_table_id, {}).get(plan.denominator_key, {}), plan.window
    )
    values = evaluate_composition(
        MetricComposition.ratio,
        numerator=numerator,
        denominator=denominator,
    )
    value_rows = _build_metric_value_rows(
        metric_definition_id=definition.id,
        scan_config_id=None,
        values=values,
    )
    time_from, time_to = plan.window
    _delete_metric_values_window(
        session,
        metric_definition_id=definition.id,
        time_from=time_from,
        time_to=time_to,
    )
    values_written = _upsert_metric_values_rows(session, rows=value_rows)

    breakdown_rows: list[dict[str, object]] = []
    for breakdown in plan.breakdowns:
        scan_key = _breakdown_scan_key(
            plan.numerator_fact_table_id, breakdown.column, breakdown.values_limit, plan.window
        )
        entry = breakdown_results.get(scan_key)
        if entry is None:
            continue
        index_by_key, rows = entry
        _append_ratio_breakdown_rows(
            breakdown_rows,
            definition=definition,
            rows=rows,
            interval_code=interval_code,
            time_from=time_from,
            time_to=time_to,
            breakdown_column=breakdown.column,
            numerator_index=index_by_key[breakdown.numerator_key],
            denominator_index=index_by_key[breakdown.denominator_key],
        )

    breakdowns_written = 0
    if plan.breakdowns:
        _delete_metric_value_breakdowns_window(
            session,
            metric_definition_id=definition.id,
            time_from=time_from,
            time_to=time_to,
        )
        breakdowns_written = _upsert_metric_value_breakdown_rows(session, rows=breakdown_rows)

    return values_written, breakdowns_written


def _run_fact_interval_group(
    session: Session,
    *,
    definitions: list[MetricDefinition],
    interval_spec: IntervalSpec,
    window_override: tuple[datetime, datetime] | None = None,
    manual_backfill: bool = False,
    completion_metric_id: uuid.UUID | None = None,
    prior_errors: int = 0,
) -> dict[str, int]:
    """Collect every fact metric of one interval group in shared warehouse scans.

    Builds dedup'd ``AggregateSpec`` lists per fact table (and per breakdown
    dimension), runs ONE multi-aggregate scan per fact table plus one per
    breakdown scan over a covering window, then assembles each metric over its
    OWN window with isolated per-metric error capture. ``window_override``
    replaces each metric's resume window when set; with ``manual_backfill`` it
    only widens it (see ``_effective_value_window``), so a swept-in metric that
    is further behind than the manual window keeps its backlog.
    """
    delta = interval_spec.delta
    interval_code = interval_spec.code
    context = _FactBatchContext(session=session)

    totals = {"metrics": 0, "collected": 0, "errors": 0, "values": 0, "breakdown_values": 0}
    try:
        ft_registries: dict[uuid.UUID, _SpecRegistry] = {}
        bd_registries: dict[_BreakdownScanKey, _SpecRegistry] = {}
        single_plans: list[_SingleMetricPlan] = []
        ratio_plans: list[_RatioMetricPlan] = []
        completion_planning_error: tuple[MetricDefinition, Exception] | None = None

        for definition in definitions:
            totals["metrics"] += 1
            try:
                window = _effective_value_window(
                    session,
                    metric_definition_id=definition.id,
                    interval_code=interval_code,
                    window_override=window_override,
                    manual_backfill=manual_backfill,
                )
                if _resolve_fact_composition(definition) is MetricComposition.ratio:
                    ratio_plans.append(
                        _plan_ratio_metric(
                            context,
                            definition=definition,
                            window=window,
                            ft_registries=ft_registries,
                            bd_registries=bd_registries,
                        )
                    )
                else:
                    single_plans.append(
                        _plan_single_metric(
                            context,
                            definition=definition,
                            window=window,
                            ft_registries=ft_registries,
                            bd_registries=bd_registries,
                        )
                    )
            except Exception as exc:
                logger.exception("Batch planning failed for metric %s", definition.id)
                if definition.id == completion_metric_id:
                    completion_planning_error = (definition, exc)
                else:
                    _stamp_metric_error(session, definition, exc)
                totals["errors"] += 1

        windows = [plan.window for plan in single_plans] + [plan.window for plan in ratio_plans]
        if not windows:
            if completion_planning_error is not None:
                _stamp_metric_error(session, *completion_planning_error)
            return totals
        covering_from = min(window[0] for window in windows)
        covering_to = max(window[1] for window in windows)

        results: dict[uuid.UUID, dict[str, dict[datetime, float]]] = {}
        fact_errors: dict[uuid.UUID, Exception] = {}
        breakdown_results: dict[
            _BreakdownScanKey, tuple[dict[str, int], list[tuple[object, ...]]]
        ] = {}
        breakdown_errors: dict[_BreakdownScanKey, Exception] = {}

        # Bound each warehouse scan's time range the way the per-metric path does:
        # split the covering window into interval-aligned chunks (smallest
        # replay_chunk_interval among the group's metrics) so no bucketed scan
        # spans the whole window, which on fine-grained intervals could exceed the
        # task soft_time_limit. Merging the per-chunk results reproduces the same
        # per-bucket series a single covering scan would (value-identity holds).
        # The one exception is a limited breakdown's top-N ranking pre-query: it
        # runs once over its metric's whole window by design,
        # an un-bucketed GROUP BY over the breakdown column rather than a scan of
        # every bucket's aggregates.
        chunks = _iter_window_chunks(
            covering_from,
            covering_to,
            interval_delta=delta,
            chunk_interval_code=_batch_chunk_interval_code(definitions),
        )
        # One ranking block per adapter spans the whole chunk loop, so each top-N
        # pre-query runs once rather than once per chunk. The window it ranks over
        # is set per limited breakdown scan below — the scan's metric window, not
        # this covering one; the outer window only owns the cache.
        with ExitStack() as ranking:
            for group_adapter in context.adapters.values():
                ranking.enter_context(rank_top_n_once(group_adapter, covering_from, covering_to))
            for chunk_from, chunk_to in chunks:
                for fact_table_id, registry in ft_registries.items():
                    if not registry.specs or fact_table_id in fact_errors:
                        continue
                    try:
                        fact_table = context.fact_tables[fact_table_id]
                        adapter = context.adapter_for(fact_table)
                        # Refresh the adapter's column allowlist for THIS fact table's
                        # query (a shared adapter serves several fact tables in turn).
                        adapter.get_columns(fact_table.sql)
                        col_names, rows = adapter.get_time_bucketed_multi_aggregate(
                            fact_table.sql,
                            fact_table.timestamp_column,
                            interval_code,
                            registry.specs,
                            chunk_from,
                            chunk_to,
                            limit=metric_query_fetch_limit(),
                        )
                        rows = _reject_truncated_rows(
                            rows,
                            what=f"Batched fact aggregate for fact table {fact_table_id}",
                            chunk_from=chunk_from,
                            chunk_to=chunk_to,
                        )
                        _merge_multi_aggregate(
                            results.setdefault(fact_table_id, {}),
                            _index_multi_aggregate(col_names, rows, interval_code),
                        )
                    except Exception as exc:  # noqa: BLE001 - attributed per metric below
                        logger.exception(
                            "Batch multi-aggregate failed for fact table %s", fact_table_id
                        )
                        fact_errors[fact_table_id] = exc

                for scan_key, registry in bd_registries.items():
                    if not registry.specs or scan_key in breakdown_errors:
                        continue
                    fact_table_id, column, values_limit, rank_window = scan_key
                    # A limited scan serves only metrics sharing ``rank_window``,
                    # so chunks outside it would be read only to be clipped away.
                    if rank_window is not None and not (
                        chunk_from < rank_window[1] and rank_window[0] < chunk_to
                    ):
                        continue
                    try:
                        fact_table = context.fact_tables[fact_table_id]
                        adapter = context.adapter_for(fact_table)
                        adapter.get_columns(fact_table.sql)
                        with (
                            rank_top_n_once(adapter, *rank_window)
                            if rank_window is not None
                            else nullcontext()
                        ):
                            col_names, rows = adapter.get_time_bucketed_multi_aggregate_breakdown(
                                fact_table.sql,
                                fact_table.timestamp_column,
                                interval_code,
                                column,
                                registry.specs,
                                chunk_from,
                                chunk_to,
                                values_limit=values_limit,
                                limit=metric_query_fetch_limit(),
                            )
                        rows = _reject_truncated_rows(
                            rows,
                            what=f"Batched fact breakdown {column!r}",
                            chunk_from=chunk_from,
                            chunk_to=chunk_to,
                        )
                        index_by_name = {name: index for index, name in enumerate(col_names)}
                        existing = breakdown_results.get(scan_key)
                        if existing is None:
                            breakdown_results[scan_key] = (index_by_name, list(rows))
                        else:
                            existing[1].extend(rows)
                    except Exception as exc:  # noqa: BLE001 - attributed per metric below
                        logger.exception("Batch breakdown scan failed for %s", scan_key)
                        breakdown_errors[scan_key] = exc

        plans: list[_SingleMetricPlan | _RatioMetricPlan] = [*single_plans, *ratio_plans]
        plans.sort(
            key=lambda plan: (
                plan.definition.id == completion_metric_id,
                str(plan.definition.id),
            )
        )
        for plan in plans:
            try:
                if isinstance(plan, _SingleMetricPlan):
                    fact_error = fact_errors.get(plan.fact_table_id)
                    if fact_error is not None:
                        raise fact_error
                    for breakdown in plan.breakdowns:
                        bd_error = breakdown_errors.get(
                            _breakdown_scan_key(
                                plan.fact_table_id,
                                breakdown.column,
                                breakdown.values_limit,
                                plan.window,
                            )
                        )
                        if bd_error is not None:
                            raise bd_error
                    values_written, breakdown_written = _assemble_single_metric(
                        session,
                        plan=plan,
                        results=results,
                        breakdown_results=breakdown_results,
                        interval_code=interval_code,
                    )
                else:
                    for fact_table_id in (
                        plan.numerator_fact_table_id,
                        plan.denominator_fact_table_id,
                    ):
                        fact_error = fact_errors.get(fact_table_id)
                        if fact_error is not None:
                            raise fact_error
                    for ratio_breakdown in plan.breakdowns:
                        bd_error = breakdown_errors.get(
                            _breakdown_scan_key(
                                plan.numerator_fact_table_id,
                                ratio_breakdown.column,
                                ratio_breakdown.values_limit,
                                plan.window,
                            )
                        )
                        if bd_error is not None:
                            raise bd_error
                    values_written, breakdown_written = _assemble_ratio_metric(
                        session,
                        plan=plan,
                        results=results,
                        breakdown_results=breakdown_results,
                        interval_code=interval_code,
                    )

                _stamp_metric_success(session, plan.definition, window_to=plan.window[1])
                totals["collected"] += 1
                totals["values"] += values_written
                totals["breakdown_values"] += breakdown_written
                # The clicked metric carries the batch's outcome to the UI, but its
                # own rows are already committed above and stay that way: a sibling
                # metric failing is not a reason to discard data this metric
                # collected correctly.
                if plan.definition.id == completion_metric_id and (
                    prior_errors + totals["errors"] > 0
                ):
                    _stamp_batch_dependency_error(session, plan.definition)
            except Exception as exc:
                logger.exception("Batch assembly failed for metric %s", plan.definition.id)
                _stamp_metric_error(session, plan.definition, exc)
                totals["errors"] += 1

        if completion_planning_error is not None:
            _stamp_metric_error(session, *completion_planning_error)

        return totals
    finally:
        context.close()


def _reject_foreign_data_source(
    session: Session,
    *,
    project_id: uuid.UUID,
    data_source: DataSource,
) -> None:
    """Fail the collection if the stored data source is another project's.

    The save doors (``load_project_data_source`` for a ``sql`` metric,
    ``fact_table_service._verify_data_source`` for a fact table) are SAVE-time,
    and rows written before them walked in while they were open: this collector
    resolves the credential by primary key alone and the beat dispatches every
    active metric with no scoping join, so such a row kept running its project's
    free-text SELECT under a foreign warehouse credential, unattended, forever.

    Called on EVERY credential this module opens, not just the ``sql`` one: the
    fact half reaches its warehouse through ``fact_tables.data_source_id``, and
    ``_load_fact_table`` scopes the fact TABLE to the metric's project while
    nothing scoped the source behind it. That half needs the guard MORE, because
    a later change tightened its save door from "a ScanConfig binds this source
    to this project" to ownership — so a stored binding the save door would now
    refuse (a workspace-global source this project has since stopped scanning,
    or one owned by another project) survives untouched in ``fact_tables`` and
    is honoured on every tick.

    The verdict is the SAME predicate the save doors apply
    (:func:`data_source_out_of_project_scope`) — one rule, every caller — so a
    legacy row now fails loudly, with a red metric and a message an owner can
    act on, instead of quietly succeeding.

    Takes ``project_id`` rather than the metric definition because the batched
    fact path resolves data sources inside ``_FactBatchContext``, which is keyed
    by fact table and holds no single definition.
    """
    scanning_project_ids: set[uuid.UUID] = set()
    if data_source.project_id is None:
        scanning_project_ids = set(
            session.scalars(
                select(ScanConfig.project_id).where(ScanConfig.data_source_id == data_source.id)
            ).all()
        )
    project_org_id = session.scalar(select(Project.organization_id).where(Project.id == project_id))
    if project_org_id is None or data_source_out_of_project_scope(
        data_source,
        project_id=project_id,
        project_organization_id=project_org_id,
        scanning_project_ids=scanning_project_ids,
    ):
        msg = (
            "This metric's data source belongs to another project and cannot be "
            "used here. Repoint the metric at a data source this project may use."
        )
        raise ScanError(msg)


def _collect_sql(
    session: Session,
    *,
    definition: MetricDefinition,
    window: tuple[datetime, datetime] | None = None,
    manual_backfill: bool = False,
) -> dict[str, object]:
    """Collect a sql metric: execute the user SELECT and bucket its rows.

    The SELECT must project the configured value column (default ``value``)
    and the configured time column (re-checked here with
    ``validate_select_sql``). Each returned row is floored to the interval;
    later rows for the same bucket overwrite earlier ones. A ``NULL`` value
    cell records that bucket as ABSENT — a gap, the same reading
    ``_index_multi_aggregate`` gives a NULL aggregate on the fact path — rather
    than failing the whole collection; a non-numeric cell is still an error.
    ``window`` overrides the resume window for a manual backfill; with
    ``manual_backfill`` that window may only reach FURTHER BACK than the
    metric's own resume point, never skip ahead of it, so a lagging metric
    keeps its backlog instead of stranding it (a legacy explicit replay window
    is still honoured verbatim).
    """
    if definition.data_source_id is None or definition.interval is None:
        msg = "sql metric requires a data source and interval"
        raise ScanError(msg)

    config = definition.config or {}
    metric_sql = _config_str(config, "metric_sql")
    time_column = _config_str(config, "time_column")
    if metric_sql is None or time_column is None:
        msg = "sql metric requires metric_sql and time_column in config"
        raise ScanError(msg)
    value_column = _config_str(config, "value_column") or SQL_VALUE_COLUMN

    try:
        safe_sql = validate_select_sql(
            metric_sql, value_column=value_column, time_column=time_column
        )
    except ValueError as exc:
        # The validator's message is tripl-authored English naming the missing
        # or unsafe part ("SELECT must project a 'value' column", ...). Letting
        # the bare ValueError escape made ``user_facing_error`` fall back to
        # "Scan failed due to an internal error." on a mistake the user can
        # actually fix.
        raise ScanError(str(exc)) from exc

    ds = session.get(DataSource, definition.data_source_id)
    if ds is None:
        msg = "DataSource for sql metric not found"
        raise ScanError(msg)
    _reject_foreign_data_source(session, project_id=definition.project_id, data_source=ds)

    adapter = _build_adapter(ds)
    total_values = 0
    try:
        adapter.test_connection()
        interval_spec = get_interval(definition.interval)
        delta = interval_spec.delta
        time_from, time_to = _effective_value_window(
            session,
            metric_definition_id=definition.id,
            interval_code=interval_spec.code,
            window_override=window,
            manual_backfill=manual_backfill,
        )
        chunks = _iter_window_chunks(
            time_from,
            time_to,
            interval_delta=delta,
            chunk_interval_code=definition.replay_chunk_interval,
        )
        for chunk_from, chunk_to in chunks:
            column_names, rows = adapter.get_preview_rows(
                safe_sql,
                limit=metric_query_fetch_limit(),
                time_column=time_column,
                time_from=chunk_from,
                time_to=chunk_to,
            )
            rows = _reject_truncated_rows(
                rows,
                what="SQL metric query",
                chunk_from=chunk_from,
                chunk_to=chunk_to,
            )
            index_by_name = {name: i for i, name in enumerate(column_names)}
            if value_column not in index_by_name or time_column not in index_by_name:
                msg = f"sql metric must project {value_column!r} and {time_column!r} columns"
                raise ScanError(msg)
            value_idx = index_by_name[value_column]
            time_idx = index_by_name[time_column]
            values: dict[datetime, float | None] = {}
            for row in rows:
                bucket = _coerce_bucket(row[time_idx], interval_spec.code)
                cell = row[value_idx]
                values[bucket] = None if cell is None else _coerce_value(cell)
            value_rows = _build_metric_value_rows(
                metric_definition_id=definition.id,
                scan_config_id=None,
                values=values,
            )
            _delete_metric_values_window(
                session,
                metric_definition_id=definition.id,
                time_from=chunk_from,
                time_to=chunk_to,
            )
            total_values += _upsert_metric_values_rows(session, rows=value_rows)
            session.commit()
    finally:
        adapter.close()

    return {"values": total_values, "_collection_window_to": time_to}


def _coerce_bucket(raw: object, interval_code: str) -> datetime:
    """Coerce a projected time cell to an interval-floored aware datetime."""
    dt = raw if isinstance(raw, datetime) else _parse_task_datetime(str(raw))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return floor_to_bucket(dt, interval_code)


def _collect_distinct_user_series(
    session: Session,
    *,
    scan_config: ScanConfig,
    user_id_column: str,
    time_from: datetime,
    time_to: datetime,
) -> dict[datetime, float]:
    """Collect a per-bucket ``count_distinct(user_id)`` series from the warehouse.

    The denominator for ``per_distinct_user``: a fresh warehouse query against
    the source scan config's data source / base query, bucketed on the config's
    interval. Builds and closes its own adapter (each grid can have a different
    data source).
    """
    if (
        scan_config.data_source_id is None
        or scan_config.time_column is None
        or scan_config.interval is None
    ):
        return {}
    ds = session.get(DataSource, scan_config.data_source_id)
    if ds is None:
        msg = "DataSource for the composition source scan config not found"
        raise ScanError(msg)

    interval_spec = get_interval(scan_config.interval)
    adapter = _build_adapter(ds)
    try:
        adapter.test_connection()
        # Populate the adapter's column allowlist BEFORE the aggregate. Every
        # adapter fills ``_allowed_columns`` only in ``get_columns``, and both
        # ``validate_measure_column`` and the adapters' ``_validate_column``
        # short-circuit their membership check on an empty allowlist — so
        # skipping this call left the only column guard the identifier regex
        # does not already cover switched off on this path alone. Mirrors
        # ``_aggregate_fact_window`` / ``collect_metrics``.
        columns = adapter.get_columns(scan_config.base_query)
        if not columns:
            msg = (
                "The composition source scan's query returned no columns; the "
                "distinct-user column cannot be validated."
            )
            raise ScanError(msg)
        allowed_columns = {column.name for column in columns}
        if user_id_column not in allowed_columns:
            # Named explicitly rather than left to the adapter: the adapter would
            # raise a plain ``ValueError``, which ``user_facing_error`` refuses to
            # surface, so the metric would read "Scan failed due to an internal
            # error." with nothing pointing at the misconfigured column.
            msg = (
                f"Distinct-user column {user_id_column!r} is not projected by the "
                "composition source scan's query; set the metric's user_id_column "
                "to one of the columns that scan selects."
            )
            raise ScanError(msg)
        _cols, _json_value_names, rows = adapter.get_time_bucketed_aggregate(
            scan_config.base_query,
            scan_config.time_column,
            interval_spec.code,
            MetricAggregation.count_distinct,
            user_id_column,
            [],
            [],
            None,
            time_from,
            time_to,
            limit=metric_query_fetch_limit(),
        )
    finally:
        adapter.close()
    rows = _reject_truncated_rows(
        rows,
        what="Distinct-user denominator query",
        chunk_from=time_from,
        chunk_to=time_to,
    )
    # Launder the bucket cell exactly like every sibling collection path does
    # (_aggregate_fact_window, _index_multi_aggregate). A bare cast() was a lie: the
    # adapters disagree on tz-awareness — ClickHouse hands back a NAIVE datetime while
    # PostgreSQL returns the same instant as aware — so the naive key met the aware
    # bucket read back from event_metrics, and every per_distinct_user metric on a
    # ClickHouse source died with "can't compare offset-naive and offset-aware
    # datetimes". _coerce_bucket stamps UTC and floors to the interval.
    return {_coerce_bucket(row[0], interval_spec.code): _coerce_value(row[-1]) for row in rows}


def _compose_grid_region(
    session: Session,
    *,
    definition: MetricDefinition,
    composition: MetricComposition,
    scan_config: ScanConfig,
    delta: timedelta,
    numerator: Mapping[datetime, float],
    denominator: Mapping[datetime, float],
    user_id_column: str,
    floor: datetime,
    ceiling: datetime | None,
) -> tuple[int, bool]:
    """Evaluate and store ONE bounded ``[floor, ceiling)`` region of one source grid.

    Returns ``(rows_written, evaluated)``; ``evaluated`` says whether the region
    produced any composed bucket at all, which is what makes the grid count toward
    the task's ``grids`` total even when every bucket divided by zero.

    Trim BOTH operand series to the region before anything reads their bounds.
    Everything downstream is bounded by that alone: the warehouse distinct-user
    query spans min(numerator)..max(numerator), and the window-delete spans the
    union of the evaluated buckets. Trimming the denominator matters as much as the
    numerator — ``_divide_over_buckets`` densifies onto the UNION, so an untrimmed
    denominator would drag every historical bucket straight back into both.
    """

    def _inside(bucket: datetime) -> bool:
        stamped = to_utc(bucket)
        return stamped >= floor and (ceiling is None or stamped < ceiling)

    numerator = {bucket: value for bucket, value in numerator.items() if _inside(bucket)}
    denominator = {bucket: value for bucket, value in denominator.items() if _inside(bucket)}
    if not numerator and not denominator:
        return 0, False

    if composition is MetricComposition.single:
        values = evaluate_composition(composition, numerator=numerator)
    elif composition is MetricComposition.ratio:
        values = evaluate_composition(
            composition,
            numerator=numerator,
            denominator=denominator,
        )
    else:
        if not numerator:  # pragma: no cover - per_distinct_user has no denominator series
            return 0, False
        # Bound the warehouse distinct-user query to the numerator range so its
        # densified union with the numerator can never exceed that range.
        distinct = _collect_distinct_user_series(
            session,
            scan_config=scan_config,
            user_id_column=user_id_column,
            time_from=min(numerator),
            time_to=max(numerator) + delta,
        )
        values = evaluate_composition(composition, numerator=numerator, denominator=distinct)

    if not values:
        return 0, False
    # Derive the delete window from the UNION of both series -- i.e. every
    # evaluated bucket, including divide-by-zero buckets mapped to None -- not
    # the numerator alone. A ``ratio`` denominator can carry buckets that
    # precede min(numerator) or follow max(numerator); ``evaluate_composition``
    # densifies value rows onto those buckets, so the window cleared before the
    # UPSERT must cover the full union. A numerator-only window would leave such
    # a row stranded once its denominator later drops to zero (value -> None,
    # no UPSERT row), because no future window-delete would ever reach it.
    window_from = min(values)
    window_to = max(values) + delta

    value_rows = _build_metric_value_rows(
        metric_definition_id=definition.id,
        scan_config_id=scan_config.id,
        values=values,
    )
    _delete_metric_values_window(
        session,
        metric_definition_id=definition.id,
        time_from=window_from,
        time_to=window_to,
        scan_config_id=scan_config.id,
    )
    # ``True`` regardless: the window was processed and its watermark must move
    # even if every bucket the warehouse answered was non-finite and dropped.
    return _upsert_metric_values_rows(session, rows=value_rows), True


def _collect_event_composition(
    session: Session,
    *,
    definition: MetricDefinition,
    window: tuple[datetime, datetime] | None = None,
    manual_backfill: bool = False,
) -> dict[str, object]:
    """Collect an event_composition metric from already-stored event_metrics.

    Reads the numerator (and, for ``ratio``, denominator) event-metric series on
    each source scan grid, evaluates the composition per grid, and writes
    ``MetricValue`` rows keyed by that ``scan_config_id``. ``per_distinct_user``
    additionally fetches a warehouse distinct-user denominator per grid.

    Each grid is composed in at most TWO bounded regions per run, never over its
    full retained history:

    * the RESUME region (``_composition_series_floor``) — two buckets before the
      metric's own last stored bucket on that grid plus everything newer, or at
      most ``EVENT_COMPOSITION_BACKFILL_BUCKETS`` back from the head of the source
      series when it has stored none;
    * one BACKFILL chunk (``_composition_backfill_region``) — up to
      ``EVENT_COMPOSITION_BACKFILL_BUCKETS`` intervals below the metric's own
      OLDEST stored bucket, so pre-history the first run could not reach is filled
      in over successive dispatches rather than being stranded for good.

    That bound is why: the previous full-history re-derivation meant a
    ``per_distinct_user`` metric re-queried the warehouse over its entire retained
    history on EVERY dispatch, and died permanently once that history exceeded
    ``METRIC_QUERY_ROW_LIMIT`` buckets. Two bounded regions keep that fixed while
    still reaching the whole series eventually.

    What is still given up: buckets the metric has ALREADY composed, between its
    oldest stored bucket and the resume floor, are not revisited. A historical
    event-metric bucket that changes after its composed value has scrolled out of
    the resume region is therefore not recomputed — the backfill fills gaps, it
    does not repair a stored value whose source changed underneath it.

    ``window`` and ``manual_backfill`` are accepted for a uniform collector
    signature but ignored: an event_composition metric has no interval of its
    own, so there is no grid for the service to compute a window against, and
    ``trigger_metric_collection`` dispatches it with neither.
    """
    binding_error = event_composition_binding_error(definition)
    if binding_error is not None:
        raise ScanError(binding_error)
    raw_composition = definition.composition
    if raw_composition is None:  # pragma: no cover - the guard above already raised
        # Unreachable, and kept anyway: the guard owns the rule so that
        # ``_event_composition_due`` can ask the same question, which leaves the
        # type checker unable to see the narrowing it used to get here.
        msg = "event_composition metric requires a composition"
        raise ScanError(msg)
    composition = (
        raw_composition
        if isinstance(raw_composition, MetricComposition)
        else MetricComposition(raw_composition)
    )

    numerator_by_grid = _read_event_metric_series(
        session,
        event_id=definition.numerator_event_id,
        event_type_id=definition.numerator_event_type_id,
    )
    if not numerator_by_grid:
        return {"values": 0, "grids": 0}

    denominator_by_grid: dict[uuid.UUID, dict[datetime, float]] = {}
    if composition is MetricComposition.ratio:
        denominator_by_grid = _read_event_metric_series(
            session,
            event_id=definition.denominator_event_id,
            event_type_id=definition.denominator_event_type_id,
        )

    config = definition.config or {}
    user_id_column = _config_str(config, "user_id_column") or DEFAULT_USER_ID_COLUMN
    stored_bounds_by_grid = _composition_stored_bounds_by_grid(
        session, metric_definition_id=definition.id
    )

    total_values = 0
    grids = 0
    for scan_config_id, numerator in numerator_by_grid.items():
        scan_config = session.get(ScanConfig, scan_config_id)
        if scan_config is None or not scan_config.interval:
            # No interval -> cannot align a delete window for this grid; skip.
            continue
        delta = get_interval(scan_config.interval).delta

        denominator = denominator_by_grid.get(scan_config_id, {})
        source_buckets = [to_utc(bucket) for bucket in (*numerator, *denominator)]
        head_bucket = max(source_buckets)
        oldest_source = min(source_buckets)
        stored_bounds = stored_bounds_by_grid.get(scan_config_id)
        stored_min, stored_max = stored_bounds if stored_bounds is not None else (None, None)

        resume_floor = _composition_series_floor(
            stored_max=stored_max,
            head_bucket=head_bucket,
            delta=delta,
        )
        regions: list[tuple[datetime, datetime | None]] = [(resume_floor, None)]
        # One bounded step of the backward frontier, so a grid whose history is
        # longer than the first run's reach is filled in over successive runs
        # instead of being truncated for good.
        backfill = _composition_backfill_region(
            stored_min=stored_min,
            resume_floor=resume_floor,
            oldest_source=oldest_source,
            delta=delta,
        )
        if backfill is not None:
            regions.append(backfill)

        touched = False
        for floor, ceiling in regions:
            rows_written, evaluated = _compose_grid_region(
                session,
                definition=definition,
                composition=composition,
                scan_config=scan_config,
                delta=delta,
                numerator=numerator,
                denominator=denominator,
                user_id_column=user_id_column,
                floor=floor,
                ceiling=ceiling,
            )
            total_values += rows_written
            touched = touched or evaluated
        if touched:
            grids += 1

    session.commit()
    return {"values": total_values, "grids": grids}


_COLLECTORS = {
    MetricKind.fact: _collect_fact,
    MetricKind.sql: _collect_sql,
    MetricKind.event_composition: _collect_event_composition,
}


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.metrics.collect_metric_definitions",
    bind=True,
    max_retries=0,
    soft_time_limit=COLLECT_METRIC_DEFINITIONS_SOFT_TIME_LIMIT_SECONDS,
    time_limit=COLLECT_METRIC_DEFINITIONS_TIME_LIMIT_SECONDS,
)
def collect_metric_definitions(
    self: object,
    metric_definition_id: str,
    window_from: str | None = None,
    window_to: str | None = None,
    force: bool = False,
    manual_backfill: bool = False,
) -> dict[str, object]:
    """Collect one catalog metric's per-bucket values into ``metric_values``.

    Dispatches by ``kind`` and stamps ``last_collected_at`` /
    ``last_collection_status`` / ``last_collection_error`` inline (success or a
    sanitized error). The full exception is logged; only a safe summary is
    persisted. An optional ``window_from`` / ``window_to`` pair (ISO strings)
    backfills an explicit recent window for a manual "collect now"; omitted, the
    scheduler's resume window is used. event_composition ignores the window.

    ``force`` is set only by the manual "collect now" trigger: it bypasses the
    active-status skip so a freshly-created (draft) metric still produces data.
    The scheduler always dispatches with ``force=False``, so scheduled collection
    stays active-only.

    ``manual_backfill`` marks the bounded window as one the "collect now" click
    derived from ``compute_manual_collect_window``, which is a floor the metric
    may reach past, not a replacement: a metric lagging by more than that window
    would otherwise collect the recent slice only while its progress jumped to
    the window end, stranding the buckets in between. The scheduler (no window)
    and legacy explicit-replay callers pass ``False`` and are unaffected. Mirrors
    ``manual_backfill_all`` on ``collect_fact_metrics_batch``.
    """
    session = _get_sync_session()
    definition: MetricDefinition | None = None
    try:
        window = _parse_window_override(window_from, window_to)
        definition = session.get(MetricDefinition, uuid.UUID(metric_definition_id))
        if definition is None:
            msg = f"MetricDefinition {metric_definition_id} not found"
            raise ValueError(msg)

        if not force and definition.status != MetricStatus.active:
            logger.info(
                "MetricDefinition %s is %s, not active; skipping",
                metric_definition_id,
                definition.status,
            )
            return {"skipped": True, "metric_definition_id": metric_definition_id}

        kind = (
            definition.kind
            if isinstance(definition.kind, MetricKind)
            else MetricKind(definition.kind)
        )
        collector = _COLLECTORS.get(kind)
        if collector is None:
            # Every supported kind is registered above; a missing collector means
            # an unsupported kind was dispatched directly. Fail with a clear
            # message rather than a cryptic KeyError.
            msg = f"Metric collection is not implemented for kind {kind.value!r}"
            raise NotImplementedError(msg)
        summary = collector(
            session, definition=definition, window=window, manual_backfill=manual_backfill
        )
        raw_window_to = summary.pop("_collection_window_to", None)
        collection_window_to = raw_window_to if isinstance(raw_window_to, datetime) else None
        _stamp_metric_success(session, definition, window_to=collection_window_to)
        return {"metric_definition_id": metric_definition_id, "kind": kind.value, **summary}

    except Exception as exc:
        logger.exception("Metric collection failed for %s", metric_definition_id)
        if definition is not None:
            try:
                session.rollback()
                mark_collection_error(definition, user_facing_error(exc))
                session.commit()
            except Exception:  # pragma: no cover - best-effort status write
                session.rollback()
        else:
            session.rollback()
        raise
    finally:
        session.close()


def _metric_kind(definition: MetricDefinition) -> MetricKind:
    """Coerce a definition's stored kind to the ``MetricKind`` enum."""
    if isinstance(definition.kind, MetricKind):
        return definition.kind
    return MetricKind(definition.kind)


def _run_fact_metrics_batch(
    session: Session,
    *,
    definitions: list[MetricDefinition],
    window_override: tuple[datetime, datetime] | None = None,
    manual_backfill_all: bool = False,
    completion_metric_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Group fact metrics by interval and collect each group in shared scans.

    Metrics dispatched together usually share one interval (the scheduler groups
    by interval before dispatch), but grouping here as well keeps the collector
    correct if a caller mixes intervals: each interval has its own bucket grid /
    ``interval_code``, so it gets its own set of shared scans. ``window_override``
    is forwarded to every group for legacy explicit-window callers. A manual
    fact-table refresh sets ``manual_backfill_all`` instead: each interval group
    receives its own bounded backfill window, so a clicked 1h metric never forces
    a dependent 1d/1w metric onto the wrong bucket grid.
    """
    totals = {"metrics": 0, "collected": 0, "errors": 0, "values": 0, "breakdown_values": 0}
    by_interval: dict[str, list[MetricDefinition]] = {}
    for definition in definitions:
        if definition.interval is None:
            totals["metrics"] += 1
            totals["errors"] += 1
            _stamp_metric_error(session, definition, ScanError("fact metric requires an interval"))
            continue
        by_interval.setdefault(str(definition.interval), []).append(definition)

    completion_interval = next(
        (
            str(definition.interval)
            for definition in definitions
            if definition.id == completion_metric_id and definition.interval is not None
        ),
        None,
    )
    ordered_groups = sorted(
        by_interval.items(),
        key=lambda item: (item[0] == completion_interval, item[0]),
    )
    for interval_code, raw_group in ordered_groups:
        group = sorted(
            raw_group,
            key=lambda definition: (
                definition.id == completion_metric_id,
                str(definition.id),
            ),
        )
        group_window = (
            compute_manual_collect_window(interval_code) if manual_backfill_all else window_override
        )
        group_totals = _run_fact_interval_group(
            session,
            definitions=group,
            interval_spec=get_interval(interval_code),
            window_override=group_window,
            manual_backfill=manual_backfill_all,
            completion_metric_id=(
                completion_metric_id if interval_code == completion_interval else None
            ),
            prior_errors=totals["errors"],
        )
        for key, value in group_totals.items():
            totals[key] += value
    return totals


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.metrics.collect_fact_metrics_batch",
    bind=True,
    max_retries=0,
    soft_time_limit=COLLECT_METRIC_DEFINITIONS_SOFT_TIME_LIMIT_SECONDS,
    time_limit=COLLECT_METRIC_DEFINITIONS_TIME_LIMIT_SECONDS,
)
def collect_fact_metrics_batch(
    self: object,
    metric_ids: list[str],
    window_from: str | None = None,
    window_to: str | None = None,
    force: bool = False,
    manual_backfill_all: bool = False,
    completion_metric_id: str | None = None,
) -> dict[str, int]:
    """Collect a batch of fact metrics, sharing one warehouse scan per fact table.

    Loads the requested metrics, keeps only the active ``fact`` ones, and runs the
    batched collector. Per-metric ``last_collected_at`` / ``last_collection_status``
    stamping and error capture happen inside the collector, so one metric failing
    never aborts the others; this wrapper only owns the session lifecycle. An
    optional ``window_from`` / ``window_to`` pair (ISO strings) backfills an
    explicit common window for legacy callers. ``manual_backfill_all`` is used
    when collect-now expands to all fact-table dependants: every compatible
    interval group computes its own bounded manual window, while all metrics on
    the same fact table and interval still share one multi-aggregate query.

    ``force`` is set only by the manual "collect now" trigger: it keeps non-active
    (draft/archived) fact metrics in the batch so a freshly-created metric still
    produces data. The scheduler always dispatches with ``force=False``, so
    scheduled collection stays active-only.
    """
    session = _get_sync_session()
    try:
        window = _parse_window_override(window_from, window_to)
        requested = [uuid.UUID(metric_id) for metric_id in metric_ids]
        loaded = (
            session.execute(select(MetricDefinition).where(MetricDefinition.id.in_(requested)))
            .scalars()
            .all()
        )
        definitions = sorted(
            (
                definition
                for definition in loaded
                if (force or definition.status == MetricStatus.active)
                and _metric_kind(definition) is MetricKind.fact
            ),
            key=lambda definition: str(definition.id),
        )
        if not definitions:
            return {"metrics": 0, "collected": 0, "errors": 0, "values": 0, "breakdown_values": 0}
        return _run_fact_metrics_batch(
            session,
            definitions=definitions,
            window_override=window,
            manual_backfill_all=manual_backfill_all,
            completion_metric_id=(
                uuid.UUID(completion_metric_id) if completion_metric_id is not None else None
            ),
        )
    except Exception:
        logger.exception("Batch fact metric collection failed for %s", metric_ids)
        session.rollback()
        raise
    finally:
        session.close()
