"""Stateless dry-run previews for draft catalog metrics.

Two flavours, one purpose: the metric's SQL is EXECUTED against the selected
warehouse while the editor is open, so a query that would otherwise only fail
inside a Celery worker — where nobody is watching — can fail here instead, in
front of the person writing it.

Preview is an OFFER, not a gate. Nothing in the save path calls it and the form
does not require it, so "it previewed" is never a precondition for 201 and
"it did not" is never a refusal. Whatever the save path must guarantee it has to
check for itself: the projection rule the collector enforces lives in
``SqlConfig`` on the schema boundary, not here (tripl-0zpq.173). Preview's job is
the half a schema cannot do — actually running the statement against the real
warehouse.

* ``preview_sql_metric`` runs a user-authored ``sql``-kind SELECT through the
  SAME safety gate (``validate_select_sql``) and time-window wrapping
  (``get_preview_rows``) the worker's ``_collect_sql`` uses, plus the dialect lint
  (``lint_dialect_sql``). The save path runs the same lint on new or changed SQL
  and refuses with the same message (tripl-0zpq.371); the worker's collection
  does not call it (tripl-0zpq.355).
* ``preview_fact_operand`` compiles a draft ``fact`` operand's row filter with
  the worker's OWN ``_resolve_fact_operand_query`` (fed the very config dict a
  save would persist) and executes the result, bounded to one row.
* ``preview_metric_series`` is the series dry run for the two kinds that have
  no SELECT of their own (MT-9): a ``fact`` draft runs the collector's own
  ``_aggregate_fact_window`` per operand over the last closed buckets
  (``fact_series_preview_window``), and an ``event_composition`` draft
  composes the already-collected event counts of one scan grid with the
  collector's ``evaluate_composition``.

None of them persists anything. Expected user mistakes — bad SQL, an unknown named
filter, a measure column the filtered query does not project, a warehouse
rejection — come back as a 200 payload with ``error`` set so the editor can
render them inline; only auth, unknown project, unknown fact table and unknown
data source use 4xx.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    SqlDialect,
    dialect_for_db_type,
    lint_dialect_sql,
    requires_measure,
    validate_identifier,
    validate_measure_column,
    validate_select_sql,
)
from tripl.core.bucketing import floor_to_bucket, to_utc
from tripl.core.intervals import get_interval
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricComposition, MetricKind
from tripl.models.event_metric import EventMetric
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.schemas.metric_definition import (
    EventCompositionMetricDefinition,
    FactMetricDefinition,
    FactOperand,
    FactOperandPreviewResponse,
    MetricGeneratedSqlQuery,
    MetricGeneratedSqlResponse,
    MetricPreviewPoint,
    MetricPreviewRequest,
    MetricPreviewResponse,
    MetricSeriesPreviewRequest,
)
from tripl.services.fact_table_service import get_fact_table
from tripl.services.project_lookup import resolve_project_id

if TYPE_CHECKING:
    # Type-only: the worker package stays out of this module's import graph.
    from tripl.worker.tasks.metrics._fact_conditions import _FactOperand

logger = logging.getLogger(__name__)

# Preview window: the last N interval buckets ending now. Small enough to stay
# interactive, large enough to show the series shape.
PREVIEW_WINDOW_BUCKETS = 50

# A fact draft's series preview runs a warehouse aggregation per operand, and the
# editor re-runs it as filters change — so on a coarse grid it is capped by wall
# clock too: 50 weekly buckets would aggregate most of a year each time. The
# floor keeps a weekly draft drawable as a shape rather than a handful of dots.
FACT_SERIES_PREVIEW_MAX_SPAN = timedelta(days=31)
FACT_SERIES_PREVIEW_MIN_BUCKETS = 8

# Hard row cap (LIMIT) on the wrapped query; the response flags truncation.
PREVIEW_ROW_LIMIT = 200

# A fact operand's dry run is a SYNTAX/SEMANTICS probe, not a data preview: the
# filtered query is executed over a bounded recent window and capped at one row,
# so pointing it at a huge fact table cannot turn into a full scan on a metered
# warehouse (BigQuery bills bytes scanned).
FACT_PREVIEW_WINDOW = timedelta(days=7)
FACT_PREVIEW_ROW_LIMIT = 1

# Generated SQL is a diagnostic response, not an export endpoint. A metric can
# reference a transitive dependency graph and replay windows can split into
# multiple chunks, so cap both dimensions before returning attacker-amplified
# JSON to an authenticated viewer.
MAX_GENERATED_SQL_QUERIES = 100
MAX_GENERATED_SQL_CHARS = 1_000_000
MAX_GENERATED_AGGREGATE_SPECS = 200
MAX_GENERATED_METRIC_ID_REFERENCES = 10_000
MAX_SAVED_FACT_FILTERS = 100
MAX_SAVED_FACT_FILTER_CHARS = 32768

# Warehouse error strings are trimmed to this many characters so a driver's
# kilobyte-long dump never reaches the client (full context stays in logs).
_ERROR_MESSAGE_MAX_CHARS = 500

# Driver strings that mean the statement never ran: we could not reach or could
# not authenticate against the warehouse. Those messages routinely carry the
# host, the port and the user (that is exactly why
# ``datasource_service._friendly_test_error`` masks them), so they are replaced
# wholesale rather than echoed. Deliberately narrow — bare "connection", "host",
# "port" or "permission" appear in legitimate SQL diagnostics ("permission denied
# for table orders") and masking those would destroy the actionability that is
# the entire point of a dry run.
_UNREACHABLE_HINTS: tuple[str, ...] = (
    "getaddrinfo",
    "name or service not known",
    "connection refused",
    "could not connect",
    "connection reset",
    "network is unreachable",
)
_AUTH_HINTS: tuple[str, ...] = (
    "authentication failed",
    "password authentication",
    "access denied",
    "invalid credentials",
    "invalid_grant",
)
_TIMEOUT_HINTS: tuple[str, ...] = ("timed out", "timeout")


def _trimmed_error(exc: Exception) -> str:
    """A user-safe, bounded error message: str(exc) only, never a traceback."""
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > _ERROR_MESSAGE_MAX_CHARS:
        return text[:_ERROR_MESSAGE_MAX_CHARS] + "…"
    return text


def _warehouse_error_message(exc: Exception) -> str:
    """A user-safe message for a warehouse failure during a dry run.

    A failure to *reach* the warehouse and a failure to *run the query* are not
    the same class of thing and must not be reported the same way:

    * A ``WarehouseCapabilityError`` is a message tripl authored about a
      configuration the operator can act on ("PostgreSQL 13 is too old"). It holds
      no host, port or credential — surfaced verbatim (see ``core/adapters/errors``).
    * A connection / auth / timeout failure is a DRIVER string that routinely names
      the host, the port and the user. It is replaced with a generic sentence, the
      same rule ``datasource_service._friendly_test_error`` enforces; the full text
      stays in the server log.
    * Anything else is the ENGINE's own diagnosis of the user's SQL ("Unrecognized
      name: amont at [1:8]") — the whole reason to execute a dry run — and is
      surfaced trimmed. It names the user's query, not our infrastructure.
    """
    if isinstance(exc, WarehouseCapabilityError):
        return _trimmed_error(exc)
    lowered = str(exc).lower()
    if any(hint in lowered for hint in _TIMEOUT_HINTS):
        return "The data source did not respond in time."
    if any(hint in lowered for hint in _UNREACHABLE_HINTS):
        return "Could not reach the data source — check its host, port, and network."
    if any(hint in lowered for hint in _AUTH_HINTS):
        return "The data source rejected the credentials — check its connection settings."
    return _trimmed_error(exc)


def _error_response(message: str, *, columns: list[str] | None = None) -> MetricPreviewResponse:
    return MetricPreviewResponse(
        columns=columns or [],
        points=[],
        point_count=0,
        truncated=False,
        error=message,
    )


def _fact_error_response(
    message: str, *, columns: list[str] | None = None
) -> FactOperandPreviewResponse:
    return FactOperandPreviewResponse(columns=columns or [], row_count=0, error=message)


def _run_preview_query(
    ds: DataSource,
    sql: str,
    *,
    limit: int,
    time_column: str,
    time_from: datetime,
    time_to: datetime,
) -> tuple[list[str], list[tuple[object, ...]]]:
    """Open a sync adapter, run the wrapped SELECT, return (columns, rows).

    Mirrors ``datasource_service._run_adapter_test``: the adapter is built and
    closed per call, and the caller executes this via ``asyncio.to_thread`` so
    the sync warehouse driver never blocks the event loop. ``build_adapter``
    applies the data source's ``timeout_seconds`` to the connect/query budget.
    """
    from tripl.core.adapters.registry import build_adapter

    adapter = build_adapter(ds)
    try:
        return adapter.get_preview_rows(
            sql,
            limit=limit,
            time_column=time_column,
            time_from=time_from,
            time_to=time_to,
        )
    finally:
        with contextlib.suppress(Exception):
            adapter.close()


def _saved_fact_column_types(fact_table: FactTable, data_source: DataSource) -> dict[str, str]:
    """Return the saved native type map used for side-effect-free SQL disclosure.

    The normalized ``type`` remains the form/validation contract. ``native_type``
    is captured by Fact table -> Preview columns and is required where a dialect
    generates different SQL for members of the same normalized family. Existing
    BigQuery tables without that snapshot must be re-previewed rather than being
    shown a plausible but non-executable TIMESTAMP guess for DATETIME/DATE.
    """
    columns = [column for column in (fact_table.columns or []) if isinstance(column, Mapping)]
    column_types = {
        str(column["name"]): str(column.get("native_type") or column.get("type") or "")
        for column in columns
        if column.get("name")
    }
    if not column_types:
        # An empty map becomes an empty column allowlist downstream, and an empty
        # allowlist is exactly what makes a membership check vacuous. The batch
        # resolver does refuse it, but that guard lives in another module — say so
        # here, for every dialect, rather than depend on remembering that.
        msg = (
            "Re-preview and save this fact table before viewing generated SQL; "
            "its columns have not been recorded yet."
        )
        raise ValueError(msg)
    if str(data_source.db_type) == "bigquery":
        timestamp_column = next(
            (column for column in columns if column.get("name") == fact_table.timestamp_column),
            None,
        )
        if timestamp_column is None or not timestamp_column.get("native_type"):
            msg = (
                "Re-preview and save this BigQuery fact table before viewing generated SQL; "
                "its native timestamp type has not been recorded yet."
            )
            raise ValueError(msg)
    return column_types


async def _load_project_fact_table(
    session: AsyncSession, slug: str, fact_table_id: uuid.UUID
) -> FactTable:
    """``get_fact_table``, with its 404 translated into this module's vocabulary.

    ``get_saved_fact_metric_sql`` walks a saved metric's operands, and an operand
    can name a fact table that is gone. That is a fact about ONE metric in the
    batch, so it must reach the ``except (ScanError, ValueError)`` handler and come
    back as the 422 that names the metric — not escape as a bare
    ``HTTPException(404, "Fact table not found")`` telling the client the metric
    it asked for does not exist.

    A ``ValueError`` rather than a ``ScanError`` because nothing about this is a
    warehouse failure, and because ``ValueError`` costs the module no worker
    import. The 404 caught here can only be the fact table's own: the caller
    resolved the slug before reaching this, through ``get_metric_definition``,
    which raises 404 for an unknown project itself.
    """
    try:
        return await get_fact_table(session, slug, fact_table_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        msg = f"fact table {fact_table_id} is no longer in this project"
        raise ValueError(msg) from exc


def _ensure_generated_sql_query_capacity(query_count: int) -> None:
    if query_count >= MAX_GENERATED_SQL_QUERIES:
        raise HTTPException(
            status_code=422,
            detail=(
                "Generated SQL exceeds the 100-statement preview limit. "
                "Reduce the metric dependency graph or replay window."
            ),
        )


def _consume_generated_sql_size(current_chars: int, sql: str) -> int:
    next_chars = current_chars + len(sql)
    if next_chars > MAX_GENERATED_SQL_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Generated SQL exceeds the 1,000,000-character preview limit. "
                "Reduce the fact query, filters, or replay window."
            ),
        )
    return next_chars


def _consume_generated_metric_id_references(current_count: int, metric_count: int) -> int:
    """Bound UUID metadata repeated across generated replay-chunk statements."""
    next_count = current_count + metric_count
    if next_count > MAX_GENERATED_METRIC_ID_REFERENCES:
        raise HTTPException(
            status_code=422,
            detail=(
                "Generated SQL exceeds the 10,000 metric-reference preview limit. "
                "Reduce the metric dependency graph or replay window."
            ),
        )
    return next_count


def _ensure_generated_sql_compile_budget(
    *, current_chars: int, base_query: str, filter_sqls: list[str | None]
) -> None:
    """Reject oversized aggregate inputs before constructing a large SQL string."""
    if len(filter_sqls) > MAX_GENERATED_AGGREGATE_SPECS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Generated SQL exceeds the 200-aggregate preview limit. "
                "Reduce the metric dependency graph."
            ),
        )
    # The compiler repeats each filter once and adds bounded syntax/aliases per
    # aggregate. This conservative estimate blocks huge stored legacy filters or
    # base queries before string assembly; the exact post-build limit remains the
    # final guard.
    estimated_chars = (
        current_chars
        + len(base_query)
        + sum(len(filter_sql or "") for filter_sql in filter_sqls)
        + 512 * len(filter_sqls)
    )
    if estimated_chars > MAX_GENERATED_SQL_CHARS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Generated SQL inputs exceed the 1,000,000-character preview limit. "
                "Reduce the fact query, filters, or dependency graph."
            ),
        )


def _ensure_saved_fact_filter_input_budget(
    fact_table: FactTable,
    *,
    operand_filter_sql: str | None,
    named_filter_count: int,
    condition_count: int,
) -> None:
    """Guard legacy rows before their filter strings are combined into a copy."""
    raw_filters = fact_table.row_filters or []
    if (
        len(raw_filters) > MAX_SAVED_FACT_FILTERS
        or named_filter_count > MAX_SAVED_FACT_FILTERS
        or condition_count > MAX_SAVED_FACT_FILTERS
    ):
        raise ValueError("Fact metric filters exceed the 100-item preview limit")
    if operand_filter_sql is not None and len(operand_filter_sql) > MAX_SAVED_FACT_FILTER_CHARS:
        raise ValueError("Fact metric filter SQL exceeds the 32,768-character preview limit")
    for raw_filter in raw_filters:
        if not isinstance(raw_filter, Mapping):
            continue
        raw_sql = raw_filter.get("sql")
        if isinstance(raw_sql, str) and len(raw_sql) > MAX_SAVED_FACT_FILTER_CHARS:
            raise ValueError("Fact table filter SQL exceeds the 32,768-character preview limit")


async def preview_sql_metric(
    session: AsyncSession, slug: str, data: MetricPreviewRequest
) -> MetricPreviewResponse:
    """Validate + execute a draft sql-metric SELECT and return bucketed points.

    The window is the last ``PREVIEW_WINDOW_BUCKETS`` buckets of the requested
    interval ending now, applied by the adapter's time-window wrapping (the
    same wrapping ``_collect_sql`` uses), with a hard ``PREVIEW_ROW_LIMIT`` row
    cap. Rows are floored to interval buckets exactly like a real collection
    (later rows for the same bucket overwrite earlier ones), and a ``NULL``
    value cell yields no point for that bucket — the same gap a collection
    would store. Nothing is persisted and the full SQL is never logged here.
    """
    # Reuses the worker's bucket/value coercion so preview points match what a
    # collection would store. Imported lazily to keep the Celery worker stack
    # out of module import (mirrors metric_definition_service).
    # Function-local for the same reason ``get_saved_fact_metric_sql`` imports
    # ``_fact_collection_group`` that way: this module is the light preview layer
    # and must not pull the whole catalog service into its module import graph.
    from tripl.services.metric_definition_service import load_project_data_source
    from tripl.worker.tasks.metrics.metric_collect import (
        SQL_VALUE_COLUMN,
        _coerce_bucket,
        _coerce_value,
    )

    project_id = await resolve_project_id(session, slug)  # 404 for an unknown project
    # Project-scoped, not a bare id lookup. This function OPENS A CONNECTION with
    # the selected data source's credential and runs the user's free-text SELECT
    # through it, so resolving the id without a project term let an editor in
    # project A read project B's warehouse by supplying its UUID. Same predicate,
    # same single not-found message as the metric save path.
    ds = await load_project_data_source(session, project_id, data.data_source_id)

    value_column = data.value_column or SQL_VALUE_COLUMN
    try:
        validate_identifier(data.time_column)
        if data.value_column is not None:
            validate_identifier(data.value_column)
        safe_sql = validate_select_sql(
            data.sql, value_column=value_column, time_column=data.time_column
        )
        dialect = dialect_for_db_type(ds.db_type)
    except ValueError as exc:
        return _error_response(str(exc))

    # Dialect gate, AFTER the read-only gate and never instead of it. Free-text SQL
    # is explicitly dialect-specific: a query written against ClickHouse cannot run
    # on the BigQuery source the user just pointed it at, and without this the first
    # sign of that is a driver stack trace from inside a Celery worker — the query
    # having been saved and scheduled long before. Catching it here turns it into a
    # sentence naming the function and the form to use instead.
    mismatch = lint_dialect_sql(safe_sql, dialect)
    if mismatch is not None:
        return _error_response(mismatch)

    delta = get_interval(data.interval.value).delta
    time_to = datetime.now(UTC)
    time_from = time_to - PREVIEW_WINDOW_BUCKETS * delta

    try:
        columns, rows = await asyncio.to_thread(
            _run_preview_query,
            ds,
            safe_sql,
            limit=PREVIEW_ROW_LIMIT,
            time_column=data.time_column,
            time_from=time_from,
            time_to=time_to,
        )
    except Exception as exc:  # noqa: BLE001 - warehouse failures are user-facing
        logger.exception("Metric SQL preview failed for data source %s", ds.id)
        return _error_response(_warehouse_error_message(exc))

    index_by_name = {name: index for index, name in enumerate(columns)}
    missing = [column for column in (data.time_column, value_column) if column not in index_by_name]
    if missing:
        names = ", ".join(repr(column) for column in missing)
        return _error_response(
            f"Query result does not include the required column(s): {names}",
            columns=list(columns),
        )

    time_index = index_by_name[data.time_column]
    value_index = index_by_name[value_column]
    # A NULL value cell is a gap, not an error — the same reading ``_collect_sql``
    # gives it — so the preview must drop the bucket rather than render CPython's
    # "float() argument must be ... not 'NoneType'" at the user. The except still
    # owns a genuinely non-numeric cell (a value column projected as text).
    values: dict[datetime, float | None] = {}
    try:
        for row in rows:
            bucket = _coerce_bucket(row[time_index], data.interval)
            cell = row[value_index]
            values[bucket] = None if cell is None else _coerce_value(cell)
    except (TypeError, ValueError) as exc:
        return _error_response(_trimmed_error(exc), columns=list(columns))

    points = [
        MetricPreviewPoint(bucket=bucket, value=value)
        for bucket, value in sorted(values.items())
        if value is not None
    ]
    return MetricPreviewResponse(
        columns=list(columns),
        points=points,
        point_count=len(points),
        truncated=len(rows) >= PREVIEW_ROW_LIMIT,
        error=None,
    )


async def preview_fact_operand(
    session: AsyncSession, slug: str, data: FactOperand
) -> FactOperandPreviewResponse:
    """Compile a draft fact operand's row filter and EXECUTE it, persisting nothing.

    A ``fact`` metric's filters are compiled per dialect and validated at compile
    time, but until this existed nothing ever *ran* them: the first execution was
    inside a Celery worker, long after the user saved and walked away. This closes
    that — the same dry-run contract the ``sql`` kind already had.

    Divergence is designed out rather than tested for. The request body IS a
    :class:`FactOperand`, so ``to_config()`` produces the exact dict a save would
    persist; that dict is parsed by the worker's own ``_operand_from_config`` and
    compiled by the worker's own ``_resolve_fact_operand_query`` for the fact
    table's data-source dialect. The SQL previewed here is therefore the SQL the
    collection will run — not a re-implementation of it. The measure/distinct
    column is then checked against the columns the filtered query actually
    returned, exactly as ``_aggregate_fact_window`` does before aggregating.

    Read-only boundary: unchanged and un-weakened. Free-text filters clear
    ``validate_sql_fragment`` at the ``FactOperand`` schema boundary (no comments,
    no ``;``, no DDL/DML/UNION/SELECT/WITH), stored named filters are re-validated
    by the same gate when they are resolved, and structured conditions are compiled
    through ``quote_identifier`` / ``quote_sql_literal``. This function adds no SQL
    of its own and relaxes nothing: it can only execute a string the collector
    would have executed anyway.
    """
    # Imported lazily so the Celery worker stack stays out of module import (same
    # reason ``preview_sql_metric`` defers its import of the collector helpers).
    from tripl.worker.tasks._errors import ScanError
    from tripl.worker.tasks.metrics._fact_conditions import (
        _fact_operand_measure,
        _operand_from_config,
        _resolve_fact_operand_query,
    )

    # Project-scoped: 404 for an unknown project or a fact table in another one.
    fact_table = await get_fact_table(session, slug, data.fact_table_id)
    if fact_table.data_source_id is None:
        return _fact_error_response(
            "This fact table has no data source bound; bind one before previewing filters."
        )
    ds = await session.scalar(select(DataSource).where(DataSource.id == fact_table.data_source_id))
    if ds is None:
        raise HTTPException(status_code=404, detail="Data source not found")

    try:
        dialect = dialect_for_db_type(ds.db_type)
        operand = _operand_from_config(data.to_config())
        base_query = _resolve_fact_operand_query(fact_table, operand, dialect=dialect)
    except (ScanError, ValueError) as exc:
        # Compile-time rejections (unknown named filter, unsupported operator,
        # unquotable value). tripl-authored messages — safe verbatim.
        return _fact_error_response(_trimmed_error(exc))

    time_to = datetime.now(UTC)
    time_from = time_to - FACT_PREVIEW_WINDOW
    try:
        columns, rows = await asyncio.to_thread(
            _run_preview_query,
            ds,
            base_query,
            limit=FACT_PREVIEW_ROW_LIMIT,
            time_column=fact_table.timestamp_column,
            time_from=time_from,
            time_to=time_to,
        )
    except Exception as exc:  # noqa: BLE001 - warehouse failures are user-facing
        logger.exception("Fact operand preview failed for fact table %s", fact_table.id)
        return _fact_error_response(_warehouse_error_message(exc))

    if requires_measure(operand.aggregation):
        measure = _fact_operand_measure(operand)
        if measure is None:
            return _fact_error_response(
                f"Aggregation {operand.aggregation.value!r} requires a "
                "measure column / distinct column.",
                columns=list(columns),
            )
        if not columns:
            # Mirrors the collector: an empty allowlist makes
            # ``validate_measure_column`` skip its membership check, so an empty
            # column list must fail loudly rather than silently pass.
            return _fact_error_response(
                "The filtered fact query returned no columns; the measure column "
                "cannot be validated.",
                columns=list(columns),
            )
        try:
            validate_measure_column(measure, set(columns))
        except ValueError as exc:
            return _fact_error_response(_trimmed_error(exc), columns=list(columns))

    return FactOperandPreviewResponse(columns=list(columns), row_count=len(rows), error=None)


def _series_response(values: Mapping[datetime, float | None]) -> MetricPreviewResponse:
    """Points from a composed series; a ``None`` bucket (divide-by-zero) is a gap."""
    points = [
        MetricPreviewPoint(bucket=bucket, value=value)
        for bucket, value in sorted(values.items())
        if value is not None
    ]
    return MetricPreviewResponse(
        columns=[], points=points, point_count=len(points), truncated=False, error=None
    )


def fact_series_preview_window(interval_code: str, *, now: datetime) -> tuple[datetime, datetime]:
    """``[time_from, time_to)`` of a fact draft's series preview, on the bucket grid.

    Both edges sit on bucket boundaries, as the collector's windows do
    (``_floor_to_interval``): an unaligned start would draw a first bucket
    holding a fraction of its interval, and ``now`` as the end would draw the
    still-open bucket — neither is a value a collection would store. The span
    is ``PREVIEW_WINDOW_BUCKETS`` buckets, capped at
    ``FACT_SERIES_PREVIEW_MAX_SPAN`` but never fewer than
    ``FACT_SERIES_PREVIEW_MIN_BUCKETS``.
    """
    delta = get_interval(interval_code).delta
    buckets = max(
        FACT_SERIES_PREVIEW_MIN_BUCKETS,
        min(PREVIEW_WINDOW_BUCKETS, FACT_SERIES_PREVIEW_MAX_SPAN // delta),
    )
    time_to = floor_to_bucket(now, interval_code)
    return time_to - buckets * delta, time_to


def _run_fact_series(
    targets: list[tuple[_FactOperand, FactTable, DataSource, SqlDialect]],
    *,
    interval_code: str,
    time_from: datetime,
    time_to: datetime,
) -> list[dict[datetime, float]]:
    """Aggregate each operand over one window with the collector's own function.

    Runs in a worker thread (sync warehouse drivers). Each operand gets its own
    adapter and its own introspection of the RAW fact SQL, exactly as
    ``_collect_fact_single`` / ``_collect_fact_ratio`` arm theirs, so the
    measure and condition columns are checked against what the warehouse
    returns today rather than what was recorded when the table was saved.
    """
    from tripl.core.adapters.registry import build_adapter
    from tripl.worker.tasks.metrics.metric_collect import _aggregate_fact_window

    series: list[dict[datetime, float]] = []
    for operand, fact_table, ds, dialect in targets:
        adapter = build_adapter(ds)
        try:
            allowed_columns = {column.name for column in adapter.get_columns(fact_table.sql)}
            values, _base_query, _measure = _aggregate_fact_window(
                adapter,
                fact_table=fact_table,
                operand=operand,
                allowed_columns=allowed_columns,
                dialect=dialect,
                interval_code=interval_code,
                chunk_from=time_from,
                chunk_to=time_to,
            )
        finally:
            with contextlib.suppress(Exception):
                adapter.close()
        series.append(values)
    return series


async def _preview_fact_series(
    session: AsyncSession, slug: str, project_id: uuid.UUID, data: FactMetricDefinition
) -> MetricPreviewResponse:
    """Dry-run a ``fact`` draft: one bucketed aggregate per operand, then compose."""
    from tripl.services.metric_definition_service import load_project_data_source
    from tripl.worker.analyzers.metric_composition import evaluate_composition
    from tripl.worker.tasks._errors import ScanError
    from tripl.worker.tasks.metrics._fact_conditions import (
        _operand_from_config,
        _resolve_fact_operand_query,
    )

    # The exact config dict a save would persist, parsed by the worker's own
    # reader — the same route ``get_saved_fact_metric_sql`` takes.
    config = data.to_definition_values()["config"]
    assert isinstance(config, dict)
    is_ratio = data.composition is MetricComposition.ratio
    if is_ratio:
        raw_operands: list[Mapping[str, object]] = [config["numerator"], config["denominator"]]
    else:
        assert data.fact_table_id is not None and data.aggregation is not None
        raw_operands = [
            {
                **config,
                "fact_table_id": str(data.fact_table_id),
                "aggregation": data.aggregation.value,
            }
        ]

    targets: list[tuple[_FactOperand, FactTable, DataSource, SqlDialect]] = []
    for raw in raw_operands:
        try:
            operand = _operand_from_config(raw)
        except (ScanError, ValueError) as exc:
            return _error_response(_trimmed_error(exc))
        # Project-scoped: 404 for a fact table in another project.
        fact_table = await get_fact_table(session, slug, operand.fact_table_id)
        if fact_table.data_source_id is None:
            return _error_response(
                f"Fact table {fact_table.display_name!r} has no data source bound; "
                "bind one before previewing."
            )
        # The credential the collector would open, fenced the way the SQL
        # preview fences its own (``_reject_foreign_data_source`` at collection).
        ds = await load_project_data_source(session, project_id, fact_table.data_source_id)
        try:
            dialect = dialect_for_db_type(ds.db_type)
            # Compile-time rejections (unknown named filter, unquotable value)
            # answered here, before a connection is opened.
            _resolve_fact_operand_query(fact_table, operand, dialect=dialect)
        except (ScanError, ValueError) as exc:
            return _error_response(_trimmed_error(exc))
        targets.append((operand, fact_table, ds, dialect))

    interval_code = data.interval.value
    time_from, time_to = fact_series_preview_window(interval_code, now=datetime.now(UTC))
    try:
        series = await asyncio.to_thread(
            _run_fact_series,
            targets,
            interval_code=interval_code,
            time_from=time_from,
            time_to=time_to,
        )
    except ScanError as exc:
        # tripl-authored: a measure or condition column the query no longer
        # returns, a truncated window.
        return _error_response(_trimmed_error(exc))
    except Exception as exc:  # noqa: BLE001 - warehouse failures are user-facing
        logger.exception("Fact metric series preview failed in project %s", project_id)
        return _error_response(_warehouse_error_message(exc))

    if is_ratio:
        values = evaluate_composition(
            MetricComposition.ratio, numerator=series[0], denominator=series[1]
        )
    else:
        values = dict(series[0])
    return _series_response(values)


def _event_metric_condition(
    event_id: uuid.UUID | None, event_type_id: uuid.UUID | None
) -> ColumnElement[bool] | None:
    """The scope ``_read_event_metric_series`` reads: per-event, else per-type rows."""
    if event_id is not None:
        return EventMetric.event_id == event_id
    if event_type_id is not None:
        return EventMetric.event_type_id == event_type_id
    return None


async def _read_event_series_window(
    session: AsyncSession,
    condition: ColumnElement[bool],
    *,
    scan_config_id: uuid.UUID,
    floor: datetime,
) -> dict[datetime, float]:
    """One grid's counts from ``floor`` on — ``_read_event_metric_series``, bounded.

    The collector reads a scope's whole retained history on every grid; a
    preview only shows ``PREVIEW_WINDOW_BUCKETS`` buckets of one grid, so it
    reads only those. Same condition, same bucket → count mapping.
    """
    rows = (
        await session.execute(
            select(EventMetric.bucket, EventMetric.count).where(
                condition,
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.bucket >= floor,
            )
        )
    ).all()
    return {to_utc(bucket): float(count) for bucket, count in rows}


class _PreloadedDataSourceSession:
    """Answers the one ``Session.get`` ``_collect_distinct_user_series`` makes.

    That collector takes a sync ``Session`` only to load the scan's data source.
    The request path has an async session and must not run a warehouse driver on
    the event loop, so the source is loaded (and project-fenced) up front and
    handed over through this stand-in; the collector itself runs unchanged in a
    worker thread.
    """

    def __init__(self, data_source: DataSource) -> None:
        self._data_source = data_source

    def get(self, model: type[object], ident: object) -> object | None:
        if model is DataSource and ident == self._data_source.id:
            return self._data_source
        return None


def _run_distinct_user_series(
    data_source: DataSource,
    *,
    scan_config: ScanConfig,
    user_id_column: str,
    time_from: datetime,
    time_to: datetime,
) -> dict[datetime, float]:
    from tripl.worker.tasks.metrics.metric_collect import _collect_distinct_user_series

    return _collect_distinct_user_series(
        cast("Session", _PreloadedDataSourceSession(data_source)),
        scan_config=scan_config,
        user_id_column=user_id_column,
        time_from=time_from,
        time_to=time_to,
    )


async def _preview_event_composition_series(
    session: AsyncSession, project_id: uuid.UUID, data: EventCompositionMetricDefinition
) -> MetricPreviewResponse:
    """Compose a draft from already-collected event counts on its newest grid.

    An ``event_composition`` metric is collected per source scan grid; the
    preview shows ONE: the grid whose numerator series reaches the most recent
    bucket (ties broken by id, so the choice is stable). Nothing runs against a
    warehouse except the distinct-user denominator of ``per_distinct_user``,
    which is the collector's own query over the same bounded window.
    """
    from tripl.services.metric_definition_service import (
        _verify_composition_refs,
        load_project_data_source,
    )
    from tripl.worker.analyzers.metric_composition import evaluate_composition
    from tripl.worker.tasks._errors import ScanError
    from tripl.worker.tasks.metrics.metric_collect import DEFAULT_USER_ID_COLUMN

    # The save path's own 422 for an event or event type outside the project:
    # without it a preview would read another project's counts by id.
    await _verify_composition_refs(session, project_id, data)
    numerator_condition = _event_metric_condition(
        data.numerator_event_id, data.numerator_event_type_id
    )
    if numerator_condition is None:  # pragma: no cover - the schema requires a numerator
        return _error_response("Pick an event to count.")

    heads = (
        await session.execute(
            select(EventMetric.scan_config_id, func.max(EventMetric.bucket))
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                numerator_condition,
                ScanConfig.project_id == project_id,
                ScanConfig.interval.is_not(None),
            )
            .group_by(EventMetric.scan_config_id)
        )
    ).all()
    candidates = [
        (to_utc(head), scan_config_id) for scan_config_id, head in heads if head is not None
    ]
    if not candidates:
        return _error_response(
            "No counts have been collected for this event yet. They appear after the "
            "next scan that tracks it."
        )
    head, scan_config_id = max(candidates, key=lambda item: (item[0], str(item[1])))
    scan_config = await session.get(ScanConfig, scan_config_id)
    if scan_config is None or scan_config.interval is None:  # pragma: no cover - joined above
        return _error_response("The scan these counts came from no longer exists.")
    interval_code = str(getattr(scan_config.interval, "value", scan_config.interval))
    delta = get_interval(interval_code).delta
    floor = head - delta * (PREVIEW_WINDOW_BUCKETS - 1)

    numerator = await _read_event_series_window(
        session, numerator_condition, scan_config_id=scan_config_id, floor=floor
    )
    composition = data.composition
    if composition is MetricComposition.single:
        return _series_response(evaluate_composition(composition, numerator=numerator))
    if composition is MetricComposition.ratio:
        denominator_condition = _event_metric_condition(
            data.denominator_event_id, data.denominator_event_type_id
        )
        denominator = (
            await _read_event_series_window(
                session, denominator_condition, scan_config_id=scan_config_id, floor=floor
            )
            if denominator_condition is not None
            else {}
        )
        return _series_response(
            evaluate_composition(composition, numerator=numerator, denominator=denominator)
        )

    # per_distinct_user: the warehouse distinct-user count over the numerator's
    # range, the bound ``_compose_grid_region`` puts on the same query.
    if not numerator:
        return _series_response({})
    data_source = await load_project_data_source(session, project_id, scan_config.data_source_id)
    try:
        distinct = await asyncio.to_thread(
            _run_distinct_user_series,
            data_source,
            scan_config=scan_config,
            user_id_column=data.user_id_column or DEFAULT_USER_ID_COLUMN,
            time_from=min(numerator),
            time_to=max(numerator) + delta,
        )
    except ScanError as exc:
        return _error_response(_trimmed_error(exc))
    except Exception as exc:  # noqa: BLE001 - warehouse failures are user-facing
        logger.exception("Distinct-user preview failed for scan config %s", scan_config.id)
        return _error_response(_warehouse_error_message(exc))
    return _series_response(
        evaluate_composition(composition, numerator=numerator, denominator=distinct)
    )


async def preview_metric_series(
    session: AsyncSession, slug: str, data: MetricSeriesPreviewRequest
) -> MetricPreviewResponse:
    """Dry-run a draft ``fact`` or ``event_composition`` metric's series (MT-9).

    The body is the definition a save would send, and the values come from the
    collector's own functions, so what the editor draws is what the first
    collection would store — minus persistence, breakdowns and the resume
    window: a fact draft covers the closed buckets of
    ``fact_series_preview_window`` (up to ``PREVIEW_WINDOW_BUCKETS``, capped at
    a month), an event composition the last
    ``PREVIEW_WINDOW_BUCKETS`` buckets of its newest source grid. Expected
    mistakes and warehouse failures come back as a 200 with ``error`` set, as
    the other previews do; an unknown project, fact table or data source is a
    404 and an event outside the project a 422, as on save.
    """
    project_id = await resolve_project_id(session, slug)
    if isinstance(data, FactMetricDefinition):
        return await _preview_fact_series(session, slug, project_id, data)
    return await _preview_event_composition_series(session, project_id, data)


async def get_saved_fact_metric_sql(
    session: AsyncSession,
    slug: str,
    metric_id: uuid.UUID,
) -> MetricGeneratedSqlResponse:
    """Compile the clicked metric's real primary dependency-batch statements.

    No adapter is connected and no warehouse statement is executed. The service
    expands the same fact-table dependency closure as Collect now, builds the
    worker's deduplicated conditional ``AggregateSpec`` registries, splits the
    same effective manual windows into replay chunks — bounded by
    ``compute_manual_collect_window`` and then widened to each metric's own resume
    point, exactly as the batch does (``effective_manual_window``) — then invokes
    the exact adapter SQL builders used by collection, down to the row LIMIT.
    """
    from tripl.core.adapters.multi_aggregate_sql import (
        compile_time_bucketed_multi_aggregate_sql,
    )
    from tripl.services.metric_definition_service import (
        _fact_collection_group,
        effective_manual_window,
        get_metric_definition,
    )
    from tripl.worker.tasks._errors import ScanError
    from tripl.worker.tasks.metrics._fact_conditions import (
        _operand_from_config,
        _resolve_batch_operand,
    )
    from tripl.worker.tasks.metrics.generation import _iter_window_chunks
    from tripl.worker.tasks.metrics.metric_collect import (
        _batch_chunk_interval_code,
        _SpecRegistry,
        metric_query_fetch_limit,
    )

    metric = await get_metric_definition(session, slug, metric_id)
    kind = metric.kind if isinstance(metric.kind, MetricKind) else MetricKind(metric.kind)
    if kind is not MetricKind.fact:
        raise HTTPException(
            status_code=400,
            detail="Generated batch SQL is available for fact metrics only",
        )

    dependency_group = await _fact_collection_group(session, metric)
    by_interval: dict[str, list[MetricDefinition]] = {}
    for definition in dependency_group:
        if definition.interval is None:
            raise HTTPException(
                status_code=422,
                detail=f"Metric {definition.name!r} in the collection batch has no interval",
            )
        by_interval.setdefault(str(definition.interval), []).append(definition)

    queries: list[MetricGeneratedSqlQuery] = []
    generated_sql_chars = 0
    generated_metric_id_references = 0
    saved_column_types: dict[uuid.UUID, dict[str, str]] = {}
    saved_fact_tables: dict[uuid.UUID, FactTable] = {}
    for interval_code, raw_definitions in by_interval.items():
        definitions = sorted(raw_definitions, key=lambda definition: str(definition.id))
        registries: dict[uuid.UUID, _SpecRegistry] = {}
        fact_tables: dict[uuid.UUID, FactTable] = {}
        data_sources: dict[uuid.UUID, DataSource] = {}
        metric_ids_by_fact: dict[uuid.UUID, set[uuid.UUID]] = {}

        for definition in definitions:
            config = dict(definition.config or {})
            composition = (
                definition.composition
                if isinstance(definition.composition, MetricComposition)
                else MetricComposition(definition.composition or MetricComposition.single.value)
            )
            raw_operands: list[Mapping[str, object]] = []
            if composition is MetricComposition.ratio:
                for role in ("numerator", "denominator"):
                    raw = config.get(role)
                    if not isinstance(raw, Mapping):
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Metric {definition.name!r} in the collection batch "
                                f"has no valid {role} operand"
                            ),
                        )
                    raw_operands.append(raw)
            elif definition.fact_table_id is not None and definition.aggregation is not None:
                aggregation = getattr(definition.aggregation, "value", definition.aggregation)
                raw_operands.append(
                    {
                        **config,
                        "fact_table_id": str(definition.fact_table_id),
                        "aggregation": str(aggregation),
                    }
                )
            else:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Metric {definition.name!r} in the collection batch has no valid operand"
                    ),
                )

            try:
                for raw in raw_operands:
                    operand = _operand_from_config(raw)
                    # Metrics land in one batch precisely BECAUSE they share fact
                    # tables, so the same table is reached once per metric here. Cache
                    # it like the data sources and column types below do
                    # (``get_fact_table`` costs two round trips: the slug lookup plus
                    # the table select).
                    fact_table = saved_fact_tables.get(operand.fact_table_id)
                    if fact_table is None:
                        # Deliberately NOT ``get_fact_table``: that raises
                        # ``HTTPException(404, "Fact table not found")``, which is
                        # neither ``ScanError`` nor ``ValueError`` and so walks
                        # straight past the handler below. The client asked about a
                        # metric that exists; a bare 404 on the whole request says
                        # the metric is missing, which is a different and wrong
                        # answer. A ratio operand's fact table id lives in opaque
                        # config JSON with no foreign key behind it, so a dangling
                        # id is reachable for rows written before
                        # ``fact_table_service`` started refusing the delete.
                        fact_table = await _load_project_fact_table(
                            session, slug, operand.fact_table_id
                        )
                        saved_fact_tables[operand.fact_table_id] = fact_table
                    _ensure_saved_fact_filter_input_budget(
                        fact_table,
                        operand_filter_sql=operand.filter_sql,
                        named_filter_count=len(operand.row_filters),
                        condition_count=len(operand.conditions),
                    )
                    if fact_table.data_source_id is None:
                        raise ValueError("Fact table has no data source bound")
                    data_source = data_sources.get(fact_table.data_source_id)
                    if data_source is None:
                        data_source = await session.scalar(
                            select(DataSource).where(DataSource.id == fact_table.data_source_id)
                        )
                        if data_source is None:
                            raise ValueError("Data source not found")
                        data_sources[fact_table.data_source_id] = data_source

                    column_types = saved_column_types.get(fact_table.id)
                    if column_types is None:
                        column_types = _saved_fact_column_types(fact_table, data_source)
                        saved_column_types[fact_table.id] = column_types
                    allowed_columns = set(column_types)
                    measure, filter_sql = _resolve_batch_operand(
                        operand,
                        fact_table=fact_table,
                        allowed_columns=allowed_columns,
                        dialect=dialect_for_db_type(data_source.db_type),
                    )
                    registry = registries.setdefault(fact_table.id, _SpecRegistry())
                    registry.register(
                        aggregation=operand.aggregation,
                        measure=measure,
                        filter_sql=filter_sql,
                    )
                    fact_tables[fact_table.id] = fact_table
                    metric_ids_by_fact.setdefault(fact_table.id, set()).add(definition.id)
            except (ScanError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Metric {definition.name!r} in the collection batch: {exc}",
                ) from exc

        # The window the batch will actually scan for THIS interval group, not the
        # bare manual window. ``_run_fact_metrics_batch`` derives its own
        # ``compute_manual_collect_window(interval_code)`` per interval group and
        # then widens it per metric to that metric's resume point, so a group
        # holding one lagging metric scans further back than the manual cap. The
        # time literals in the disclosed statement are part of the statement; a
        # window that is merely a lower bound makes this endpoint's promise false.
        time_from, time_to = await effective_manual_window(
            session, definitions=definitions, interval_code=interval_code
        )
        chunks = _iter_window_chunks(
            time_from,
            time_to,
            interval_delta=get_interval(interval_code).delta,
            chunk_interval_code=_batch_chunk_interval_code(definitions),
        )
        for chunk_from, chunk_to in chunks:
            for fact_table_id, registry in registries.items():
                _ensure_generated_sql_query_capacity(len(queries))
                metric_ids = metric_ids_by_fact[fact_table_id]
                generated_metric_id_references = _consume_generated_metric_id_references(
                    generated_metric_id_references,
                    len(metric_ids),
                )
                fact_table = fact_tables[fact_table_id]
                data_source_id = fact_table.data_source_id
                assert data_source_id is not None
                data_source = data_sources[data_source_id]
                column_types = saved_column_types[fact_table.id]
                _ensure_generated_sql_compile_budget(
                    current_chars=generated_sql_chars,
                    base_query=fact_table.sql,
                    filter_sqls=[spec.filter_sql for spec in registry.specs],
                )
                try:
                    _columns, sql = compile_time_bucketed_multi_aggregate_sql(
                        db_type=str(data_source.db_type),
                        base_query=fact_table.sql,
                        time_column=fact_table.timestamp_column,
                        interval=interval_code,
                        specs=registry.specs,
                        time_from=chunk_from,
                        time_to=chunk_to,
                        column_types=column_types,
                        # The collector's fetch limit, not the ceiling: every
                        # collection call site asks for one probe row above
                        # METRIC_QUERY_ROW_LIMIT so ``_reject_truncated_rows`` can
                        # tell a full window from a cut-short one. Passing the bare
                        # ceiling here disclosed ``LIMIT 100000`` for a statement
                        # that runs ``LIMIT 100001`` — a one-character difference,
                        # but this endpoint's whole contract is that the string can
                        # be pasted into a warehouse console and reproduce the run.
                        limit=metric_query_fetch_limit(),
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                generated_sql_chars = _consume_generated_sql_size(generated_sql_chars, sql)
                queries.append(
                    MetricGeneratedSqlQuery(
                        label=f"{fact_table.display_name} · {interval_code}",
                        fact_table_id=fact_table.id,
                        fact_table_name=fact_table.display_name,
                        interval=interval_code,
                        window_from=chunk_from,
                        window_to=chunk_to,
                        metric_ids=sorted(metric_ids, key=str),
                        sql=sql,
                    )
                )
    return MetricGeneratedSqlResponse(queries=queries, breakdown_queries_omitted=True)
