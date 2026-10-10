from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import delete, tuple_
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tripl.core.adapters.base import BaseAdapter
from tripl.core.analyzers.distribution_drift import TopShift, compute_psi
from tripl.core.analyzers.event_generator import (
    GenerationResult,
    _apply_name_format,
    _format_value,
    apply_event_group_rules,
    event_name_format_columns,
    json_name_format_keys,
    raw_values_from_row,
    render_default_event_name,
    truncate_event_name,
)
from tripl.core.bucketing import stored_bucket, to_utc
from tripl.core.json_string_columns import scan_source_query
from tripl.json_paths import (
    MAX_PROPERTY_FIELDS,
    build_json_value,
    decode_json_path_value,
    format_json_path_value,
    group_json_value_paths,
    is_property_field,
    split_property_field,
)
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import DistributionDriftBand
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import ShadowEventCandidate
from tripl.services._id_chunks import chunked
from tripl.worker.tasks.metrics._helpers import MAX_BREAKDOWN_VALUE_LENGTH
from tripl.worker.utils.scan_naming import group_column_index

logger = logging.getLogger(__name__)


def _build_event_name_from_row(
    data_row: Sequence[object],
    col_meta: dict[str, dict[str, object]],
    reg_index: dict[str, int],
    json_index: dict[str, int],
    n_reg: int,
    json_value_names: list[str],
    event_name_format: str | None,
    event_group_rules: Sequence[Mapping[str, object]] | None = None,
    *,
    event_type_column: str | None = None,
    time_column: str | None = None,
) -> str | None:
    """Build event name from a CH row using col_meta (same logic as generate_events).

    Every branch below has a counterpart in ``plan_events``, which mints the
    ``Event.source_name`` this name is then looked up by. The two must agree
    byte for byte or the row's volume is filed as an unplanned identity, so the
    parts that used to be spelled twice — the default name, the truncation and
    the group-rule value dict — are now imported from the planner rather than
    re-derived here.
    """
    kwargs: dict[str, str] = {}
    json_value_index = {
        name: n_reg + len(json_index) + idx for idx, name in enumerate(json_value_names)
    }
    # Group rules see the whole row, not just the columns with a field
    # definition: rule columns are reserved columns by construction, and a
    # reserved column never enters ``col_meta``.
    raw_values_by_field = raw_values_from_row(
        data_row,
        reg_index=reg_index,
        json_index=json_index,
        n_reg=n_reg,
        json_value_names=json_value_names,
        event_type_column=event_type_column,
        time_column=time_column,
    )

    for col_name, meta in col_meta.items():
        if meta.get("is_json"):
            j = json_index.get(col_name)
            if j is None:
                continue
            paths = data_row[n_reg + j]
            if paths:
                if isinstance(paths, (list, tuple)):
                    sorted_paths = sorted(str(p) for p in paths)
                else:
                    sorted_paths = [str(paths)]
                passthrough_paths = meta.get("json_passthrough_paths", [])
                if not isinstance(passthrough_paths, list):
                    passthrough_paths = []
                preserved_values = {
                    full_path: decode_json_path_value(data_row[json_value_index[full_path]])
                    for full_path in passthrough_paths
                    if full_path in json_value_index and full_path.startswith(f"{col_name}.")
                }
                value = build_json_value(
                    col_name,
                    sorted_paths,
                    preserved_values=preserved_values,
                )
            else:
                value = "{}"
        elif meta.get("is_low"):
            i = reg_index.get(col_name)
            if i is None:
                continue
            value = _format_value(data_row[i])
        else:
            # High-cardinality: use template
            template = meta.get("template")
            if not isinstance(template, str):
                continue
            value = template

        kwargs[col_name] = value
        if meta.get("is_json") and paths:
            for path in sorted_paths:
                full_path = f"{col_name}.{path}"
                if full_path in json_value_index:
                    rendered = format_json_path_value(data_row[json_value_index[full_path]])
                else:
                    rendered = f"${{{full_path}}}"
                # Only a name format can consume a per-path key. The default
                # name is one segment per COLUMN, which is how ``plan_events``
                # builds the ``source_name`` this has to match: appending a
                # ``col.path=`` segment here matched nothing.
                if event_name_format:
                    kwargs[full_path] = rendered

    if not kwargs:
        return None

    if event_name_format:
        # The event type column has no ``col_meta`` entry — it is skipped there
        # by design — but ``reserved_catalog_columns`` un-reserves it when the
        # format names it, so the format is entitled to it and the row carries
        # the value. The planner injects it the same way; this
        # must stay AFTER the ``not kwargs`` guard so the collector never mints
        # an identity the planner, which returns no events at all for an empty
        # ``col_meta``, did not plan.
        if event_type_column and event_type_column in event_name_format_columns(event_name_format):
            et_idx = reg_index.get(event_type_column)
            if et_idx is not None:
                kwargs.setdefault(event_type_column, _format_value(data_row[et_idx]))
        # A dotted placeholder whose path this row does not carry is an empty
        # segment, exactly like a NULL regular column.
        for key in json_name_format_keys(event_name_format, col_meta):
            kwargs.setdefault(key, "")
        # Second caller of ``_apply_name_format``. It raises ``NameFormatError``,
        # which ``user_facing_error`` surfaces verbatim, so this path needs no
        # try/except wrapper — do not add one.
        res = _apply_name_format(event_name_format, kwargs)
    else:
        res = render_default_event_name(kwargs.items())

    res = truncate_event_name(res)
    raw_values_by_field["__event_name"] = res
    raw_values_by_field.setdefault("event_name", res)
    group_match = apply_event_group_rules(res, raw_values_by_field, event_group_rules)
    res = group_match.event_name
    return res


def _normalize_breakdown_value(value: object) -> str:
    formatted = _format_value(value)
    if len(formatted) <= MAX_BREAKDOWN_VALUE_LENGTH:
        return formatted
    return formatted[:MAX_BREAKDOWN_VALUE_LENGTH]


def _is_supported_metric_breakdown_column(
    config: ScanConfig,
    *,
    column: str,
    regular_cols: list[str],
    json_cols: Sequence[str] = (),
) -> bool:
    """Whether ``column`` can be collected as a breakdown of this scan's rows.

    A scalar column must be one of the scan's regular columns. A property entry
    (``<json_column>.<path>``, F23 #306) must name a path of one of its nested
    columns, whose value the adapter extracts per row; an entry that breaks the
    path grammar (only a row written before the save-time check could hold one)
    is unsupported rather than an error.
    """
    if column in (config.event_type_column, config.time_column):
        return False
    try:
        prop = split_property_field(column)
    except ValueError:
        return False
    if prop is None:
        return column in regular_cols
    return prop[0] in json_cols


def _is_supported_configured_breakdown_column(
    config: ScanConfig,
    *,
    column: str,
    regular_cols: list[str],
    json_cols: Sequence[str] = (),
) -> bool:
    """The test for a column a USER listed in ``metric_breakdown_columns``.

    One exclusion on top of the base test: ``app_version_column``. Versions are
    already collected on their own path (``_collect_app_version_breakdown_rows``)
    into the same table with the same row shape, so collecting them here as well
    writes the SAME (scope, bucket, column, value, is_other) key twice inside one
    multi-row ON CONFLICT DO UPDATE — a cardinality violation on Postgres — and
    adds a generic ``('app_version', 'Other', True)`` row the version path never
    writes and never deletes, which then double-counts in the version series.

    ``platform_column`` is deliberately NOT excluded. It is added scan-wide below
    and deduped through ``scan_breakdown_column_set``, so an event listing it
    takes the scan-wide branch and can only ever produce one key — and the demo
    project ships an event that lists it.

    The base test stays as it is: ``_collect_app_version_breakdown_rows`` guards
    itself with it, so tightening that one instead would silently delete every
    app-version series.
    """
    return (
        _is_supported_metric_breakdown_column(
            config,
            column=column,
            regular_cols=regular_cols,
            json_cols=json_cols,
        )
        and column != config.app_version_column
    )


def _is_supported_distribution_drift_field(
    config: ScanConfig,
    *,
    field_name: str,
    regular_cols: list[str],
    json_cols: Sequence[str] = (),
) -> bool:
    return _is_supported_metric_breakdown_column(
        config,
        column=field_name,
        regular_cols=regular_cols,
        json_cols=json_cols,
    )


def _within_property_cap(field: str, selected: Collection[str]) -> bool:
    """Whether one more field keeps a query within ``MAX_PROPERTY_FIELDS`` properties.

    The save schema holds each list to the cap; this holds the collection
    query to it too, because the breakdown query unions the scan's list with
    every event's, and each property parses the JSON document per row. A
    scalar column, or a property already selected, is always within it.
    """
    if not is_property_field(field) or field in selected:
        return True
    return sum(1 for item in selected if is_property_field(item)) < MAX_PROPERTY_FIELDS


def _serialize_distribution_top_movers(top_movers: list[TopShift]) -> list[dict[str, object]]:
    return [
        {
            "value": shift.value,
            "baseline_share": shift.baseline_share,
            "current_share": shift.current_share,
            "contribution": shift.contribution,
        }
        for shift in top_movers
    ]


def _get_scan_json_value_path_map(config: ScanConfig) -> dict[str, list[str]]:
    return group_json_value_paths(config.json_value_paths)


# PostgreSQL/psycopg caps a single statement at 65535 bind parameters. A
# multi-row INSERT contributes one parameter per column per row, so large
# upserts must be split into batches that stay under that ceiling. We use a
# margin below 65535 to be safe across drivers.
_MAX_BIND_PARAMS = 60000


def _chunk_rows(rows: list[dict[str, object]]) -> Iterator[list[dict[str, object]]]:
    """Yield row batches sized so ``rows_per_batch * columns <= _MAX_BIND_PARAMS``."""
    columns = max(1, len(rows[0]))
    yield from chunked(rows, max(1, _MAX_BIND_PARAMS // columns))


def _chunk_keys[KeyT: tuple[object, ...]](keys: Sequence[KeyT]) -> Iterator[list[KeyT]]:
    """Yield key batches sized so ``keys_per_batch * width + 1 <= _MAX_BIND_PARAMS``.

    Same ceiling and same reason as ``_chunk_rows``: an expanded tuple ``IN``
    contributes one bind parameter per element per key, and a replay chunk can
    carry up to ``metrics_row_limit`` keys. The ``+ 1`` is the ``scan_config_id``
    bind every one of these deletes carries alongside the ``IN``. Empty input
    yields nothing, so callers that split their key list by scope can hand each
    half over unguarded.
    """
    if not keys:
        return
    width = max(1, len(keys[0]))
    for batch in chunked(keys, max(1, (_MAX_BIND_PARAMS - 1) // width)):
        yield list(batch)


def _upsert_event_metrics_rows(
    session: Session,
    *,
    rows: list[dict[str, object]],
    constraint: str,
) -> None:
    if not rows:
        return

    is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
    for chunk in _chunk_rows(rows):
        if is_sqlite:
            sqlite_stmt = sqlite_insert(EventMetric).values(chunk)
            sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=["scan_config_id", "event_id", "bucket"]
                if constraint == "uq_event_metric_config_event_bucket"
                else ["scan_config_id", "event_type_id", "bucket"],
                set_={"count": sqlite_stmt.excluded.count},
            )
            session.execute(sqlite_stmt)
            continue

        pg_stmt = pg_insert(EventMetric).values(chunk)
        pg_stmt = pg_stmt.on_conflict_do_update(
            constraint=constraint,
            set_={"count": pg_stmt.excluded.count},
        )
        session.execute(pg_stmt)


def _upsert_event_metric_breakdown_rows(
    session: Session,
    *,
    rows: list[dict[str, object]],
    constraint: str,
) -> None:
    if not rows:
        return

    is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
    pg_constraint = (
        "uq_event_metric_breakdown_config_event_bucket_value"
        if constraint == "event"
        else "uq_event_metric_breakdown_config_type_bucket_value"
    )
    for chunk in _chunk_rows(rows):
        if is_sqlite:
            sqlite_stmt = sqlite_insert(EventMetricBreakdown).values(chunk)
            sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=[
                    "scan_config_id",
                    "event_id" if constraint == "event" else "event_type_id",
                    "bucket",
                    "breakdown_column",
                    "breakdown_value",
                    "is_other",
                ],
                set_={
                    "count": sqlite_stmt.excluded.count,
                    "is_other": sqlite_stmt.excluded.is_other,
                },
            )
            session.execute(sqlite_stmt)
            continue

        pg_stmt = pg_insert(EventMetricBreakdown).values(chunk)
        pg_stmt = pg_stmt.on_conflict_do_update(
            constraint=pg_constraint,
            set_={"count": pg_stmt.excluded.count, "is_other": pg_stmt.excluded.is_other},
        )
        session.execute(pg_stmt)


def _upsert_coverage_rows(
    session: Session,
    *,
    rows: list[dict[str, object]],
) -> None:
    if not rows:
        return

    is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
    for chunk in _chunk_rows(rows):
        if is_sqlite:
            sqlite_stmt = sqlite_insert(CoverageMetric).values(chunk)
            sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=["scan_config_id", "bucket"],
                set_={
                    "total_count": sqlite_stmt.excluded.total_count,
                    "matched_count": sqlite_stmt.excluded.matched_count,
                },
            )
            session.execute(sqlite_stmt)
            continue

        pg_stmt = pg_insert(CoverageMetric).values(chunk)
        pg_stmt = pg_stmt.on_conflict_do_update(
            constraint="uq_coverage_metric_config_bucket",
            set_={
                "total_count": pg_stmt.excluded.total_count,
                "matched_count": pg_stmt.excluded.matched_count,
            },
        )
        session.execute(pg_stmt)


def _delete_coverage_metrics_window(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    time_from: datetime,
    time_to: datetime,
) -> int:
    result = session.execute(
        delete(CoverageMetric).where(
            CoverageMetric.scan_config_id == scan_config_id,
            CoverageMetric.bucket >= time_from,
            CoverageMetric.bucket < time_to,
        )
    )
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)


def _upsert_shadow_event_candidates(
    session: Session,
    *,
    rows: list[dict[str, object]],
) -> None:
    """Insert-or-refresh shadow candidates.

    On conflict only the observation columns move: ``observed_count`` and
    ``sample_properties`` are the latest window's, ``last_seen_at`` never
    rewinds. ``status`` and the resolution columns are user-owned and left
    untouched so an accepted or dismissed candidate is not resurrected by the
    collector.
    """
    if not rows:
        return

    is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
    for chunk in _chunk_rows(rows):
        if is_sqlite:
            sqlite_stmt = sqlite_insert(ShadowEventCandidate).values(chunk)
            sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                index_elements=["scan_config_id", "event_name"],
                set_={
                    "observed_count": sqlite_stmt.excluded.observed_count,
                    "event_type_id": sqlite_stmt.excluded.event_type_id,
                    "sample_properties": sqlite_stmt.excluded.sample_properties,
                    "last_seen_at": sa_func.max(
                        ShadowEventCandidate.last_seen_at,
                        sqlite_stmt.excluded.last_seen_at,
                    ),
                },
            )
            session.execute(sqlite_stmt)
            continue

        pg_stmt = pg_insert(ShadowEventCandidate).values(chunk)
        pg_stmt = pg_stmt.on_conflict_do_update(
            constraint="uq_shadow_candidate_config_name",
            set_={
                "observed_count": pg_stmt.excluded.observed_count,
                "event_type_id": pg_stmt.excluded.event_type_id,
                "sample_properties": pg_stmt.excluded.sample_properties,
                "last_seen_at": sa_func.greatest(
                    ShadowEventCandidate.last_seen_at,
                    pg_stmt.excluded.last_seen_at,
                ),
            },
        )
        session.execute(pg_stmt)


def _delete_event_metrics_window(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    time_from: datetime,
    time_to: datetime,
) -> int:
    result = session.execute(
        delete(EventMetric).where(
            EventMetric.scan_config_id == scan_config_id,
            EventMetric.bucket >= time_from,
            EventMetric.bucket < time_to,
        )
    )
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)


def _delete_event_metrics_rows(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    keys: Sequence[tuple[uuid.UUID, datetime]],
) -> int:
    if not keys:
        return 0

    deleted = 0
    for batch in _chunk_keys(keys):
        result = session.execute(
            delete(EventMetric).where(
                EventMetric.scan_config_id == scan_config_id,
                tuple_(EventMetric.event_id, EventMetric.bucket).in_(batch),
            )
        )
        rowcount = getattr(result, "rowcount", 0)
        deleted += int(rowcount or 0)
    return deleted


def _delete_event_type_metrics_rows(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    keys: Sequence[tuple[uuid.UUID, datetime]],
) -> int:
    if not keys:
        return 0

    deleted = 0
    for batch in _chunk_keys(keys):
        result = session.execute(
            delete(EventMetric).where(
                EventMetric.scan_config_id == scan_config_id,
                tuple_(EventMetric.event_type_id, EventMetric.bucket).in_(batch),
            )
        )
        rowcount = getattr(result, "rowcount", 0)
        deleted += int(rowcount or 0)
    return deleted


def _delete_event_metric_breakdowns_window(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    time_from: datetime,
    time_to: datetime,
) -> int:
    result = session.execute(
        delete(EventMetricBreakdown).where(
            EventMetricBreakdown.scan_config_id == scan_config_id,
            EventMetricBreakdown.bucket >= time_from,
            EventMetricBreakdown.bucket < time_to,
        )
    )
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)


def _delete_event_metric_breakdowns_column_window(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    breakdown_column: str,
    time_from: datetime,
    time_to: datetime,
) -> int:
    """Delete every breakdown row for one column within a window.

    Used by replay to clear app-version rows before re-inserting the complete
    per-version set for the chunk. App-version breakdowns store every version
    verbatim (retention / "Other" rollup is a read-time concern), so a
    column-scoped window delete is the correct idempotent replacement for the
    per-key delete — it also sweeps obsolete ``is_other=True`` rows left behind
    by the old per-chunk retention path.
    """
    result = session.execute(
        delete(EventMetricBreakdown).where(
            EventMetricBreakdown.scan_config_id == scan_config_id,
            EventMetricBreakdown.breakdown_column == breakdown_column,
            EventMetricBreakdown.bucket >= time_from,
            EventMetricBreakdown.bucket < time_to,
        )
    )
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)


def _delete_event_metric_breakdown_rows(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    keys: Sequence[tuple[uuid.UUID, datetime, str]],
    constraint: str,
) -> int:
    """Delete the produced breakdown rows for a replayed chunk, per (scope, bucket, column).

    The key deliberately stops at the column. Whether a value is stored under
    its own label or folded into ``("Other", is_other=True)`` is a property of
    the query window the top-N was ranked over, not of the data, and a replay
    chunk is generally wider than the chunk that first collected the window. A
    value-scoped key therefore left the previous label behind next to the new
    one and the read side summed both, so the breakdown total exceeded the
    event's own metric. Clearing every row of a (scope, bucket, column) the
    chunk re-derived makes the fold idempotent while still keeping rows for
    scopes the chunk produced nothing for.
    """
    if not keys:
        return 0

    metric_key = (
        tuple_(
            EventMetricBreakdown.event_id,
            EventMetricBreakdown.bucket,
            EventMetricBreakdown.breakdown_column,
        )
        if constraint == "event"
        else tuple_(
            EventMetricBreakdown.event_type_id,
            EventMetricBreakdown.bucket,
            EventMetricBreakdown.breakdown_column,
        )
    )
    deleted = 0
    for batch in _chunk_keys(keys):
        result = session.execute(
            delete(EventMetricBreakdown).where(
                EventMetricBreakdown.scan_config_id == scan_config_id,
                metric_key.in_(batch),
            )
        )
        rowcount = getattr(result, "rowcount", 0)
        deleted += int(rowcount or 0)
    return deleted


def _delete_distribution_drifts_window(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    time_from: datetime,
    time_to: datetime,
) -> int:
    result = session.execute(
        delete(DistributionDrift).where(
            DistributionDrift.scan_config_id == scan_config_id,
            DistributionDrift.bucket >= time_from,
            DistributionDrift.bucket < time_to,
        )
    )
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)


def _delete_distribution_drifts_rows(
    session: Session,
    *,
    scan_config_id: uuid.UUID,
    keys: Sequence[tuple[uuid.UUID | None, datetime, str]],
) -> int:
    """Delete the produced drift rows for a replayed chunk.

    ``event_type_id`` is NULL on the scan-wide row the producer emits for every
    (field, bucket). A row-value ``IN`` whose component is NULL compares as
    unknown rather than true, so those keys matched nothing and every replay of
    an already-collected window appended another duplicate scan-wide row. The
    scan-wide keys therefore need an explicit ``IS NULL`` predicate instead of
    being folded into the tuple.
    """
    if not keys:
        return 0

    scoped_keys = [key for key in keys if key[0] is not None]
    scan_wide_keys = [
        (bucket, field_name)
        for (event_type_id, bucket, field_name) in keys
        if event_type_id is None
    ]

    deleted = 0
    for batch in _chunk_keys(scoped_keys):
        result = session.execute(
            delete(DistributionDrift).where(
                DistributionDrift.scan_config_id == scan_config_id,
                tuple_(
                    DistributionDrift.event_type_id,
                    DistributionDrift.bucket,
                    DistributionDrift.field_name,
                ).in_(batch),
            )
        )
        rowcount = getattr(result, "rowcount", 0)
        deleted += int(rowcount or 0)
    for scan_wide_batch in _chunk_keys(scan_wide_keys):
        result = session.execute(
            delete(DistributionDrift).where(
                DistributionDrift.scan_config_id == scan_config_id,
                DistributionDrift.event_type_id.is_(None),
                tuple_(
                    DistributionDrift.bucket,
                    DistributionDrift.field_name,
                ).in_(scan_wide_batch),
            )
        )
        rowcount = getattr(result, "rowcount", 0)
        deleted += int(rowcount or 0)
    return deleted


def _collect_metric_breakdown_rows(
    *,
    adapter: BaseAdapter,
    config: ScanConfig,
    interval_code: str,
    regular_cols: list[str],
    json_cols: list[str],
    json_value_path_map: dict[str, list[str]],
    time_from: datetime,
    time_to: datetime,
    query_row_limit: int,
    reg_index: dict[str, int],
    json_index: dict[str, int],
    n_reg: int,
    gen_results: dict[str, GenerationResult],
    single_result: GenerationResult | None,
    et_by_name: dict[str, EventType],
) -> tuple[list[dict[str, object]], list[dict[str, object]], bool]:
    event_agg: dict[tuple[uuid.UUID, uuid.UUID, datetime, str, str, bool], int] = {}
    type_agg: dict[tuple[uuid.UUID, uuid.UUID, datetime, str, str, bool], int] = {}
    et_col_idx = group_column_index(config, reg_index)

    breakdown_columns: list[str] = []
    scan_breakdown_column_set: set[str] = set()
    event_breakdown_columns_by_event_id: dict[uuid.UUID, set[str]] = {}
    seen_breakdown_columns: set[str] = set()
    unsupported_breakdown_columns: set[str] = set()

    def add_supported_column(
        configured_column: str, *, source: str, allow_reserved_roles: bool = False
    ) -> bool:
        # ``allow_reserved_roles`` is for the columns this function adds itself
        # because the scan designates them (the platform column), not for the
        # ones a user typed into a metric_breakdown_columns list.
        is_supported = (
            _is_supported_metric_breakdown_column
            if allow_reserved_roles
            else _is_supported_configured_breakdown_column
        )
        if is_supported(
            config,
            column=configured_column,
            regular_cols=regular_cols,
            json_cols=json_cols,
        ) and _within_property_cap(configured_column, seen_breakdown_columns):
            if configured_column not in seen_breakdown_columns:
                breakdown_columns.append(configured_column)
                seen_breakdown_columns.add(configured_column)
            return True
        if configured_column not in unsupported_breakdown_columns:
            logger.warning(
                "Skipping unsupported metric breakdown column %r for scan %s (%s)",
                configured_column,
                config.id,
                source,
            )
            unsupported_breakdown_columns.add(configured_column)
        return False

    for configured_column in config.metric_breakdown_columns or []:
        if configured_column in scan_breakdown_column_set:
            continue
        if add_supported_column(configured_column, source="scan_config"):
            scan_breakdown_column_set.add(configured_column)

    # The designated platform column is collected as a scan-level breakdown so
    # platform values land in EventMetricBreakdown — powering the per-event
    # platform presence matrix and per-platform volume anomalies — without the
    # user also listing it in metric_breakdown_columns. Deduped against the set
    # above so it is never queried twice.
    if (
        config.platform_column
        and config.platform_column not in scan_breakdown_column_set
        and add_supported_column(
            config.platform_column,
            source="platform_column",
            allow_reserved_roles=True,
        )
    ):
        scan_breakdown_column_set.add(config.platform_column)

    generation_results: list[GenerationResult] = []
    if single_result is not None:
        generation_results.append(single_result)
    generation_results.extend(gen_results.values())
    for generation_result in generation_results:
        for event in generation_result.events_by_name.values():
            event_columns: set[str] = set()
            for configured_column in event.metric_breakdown_columns or []:
                if add_supported_column(configured_column, source=f"event:{event.id}"):
                    event_columns.add(configured_column)
            if event_columns:
                event_breakdown_columns_by_event_id[event.id] = event_columns

    if not breakdown_columns:
        return [], [], False

    (
        _col_names,
        breakdown_json_value_names,
        rows,
    ) = adapter.get_time_bucketed_breakdown_counts_multi(
        scan_source_query(adapter, config),
        config.time_column or "",
        interval_code,
        breakdown_columns,
        regular_cols,
        json_cols,
        json_value_path_map,
        time_from,
        time_to,
        values_limit=config.metric_breakdown_values_limit,
        limit=query_row_limit + 1,
    )
    truncated = len(rows) > query_row_limit
    rows = rows[:query_row_limit]
    logger.info(
        "Got %s bucketed breakdown rows for %s from ClickHouse",
        len(rows),
        ", ".join(breakdown_columns),
    )

    for row in rows:
        bucket = stored_bucket(row[0])
        breakdown_column = str(row[1])
        breakdown_value = _normalize_breakdown_value(row[2])
        is_other = bool(row[3])
        data_row = row[4:]
        cnt = int(cast(int | str | float, row[-1]))
        col_meta: dict[str, dict[str, object]]
        events_by_name: dict[str, Event]
        event_type_id: uuid.UUID | None

        if et_col_idx is not None:
            et_name = str(data_row[et_col_idx])
            event_type = et_by_name.get(et_name)
            if event_type is None:
                continue
            event_type_id = event_type.id
            gen_result: GenerationResult | None = gen_results.get(et_name)
            if gen_result is None:
                continue
            col_meta = gen_result.col_meta
            events_by_name = gen_result.events_by_name
        else:
            event_type_id = config.event_type_id
            if single_result is None:
                continue
            col_meta = single_result.col_meta
            events_by_name = single_result.events_by_name

        event_name = _build_event_name_from_row(
            data_row,
            col_meta,
            reg_index,
            json_index,
            n_reg,
            breakdown_json_value_names,
            config.event_name_format,
            config.event_group_rules,
            event_type_column=config.event_type_column,
            time_column=config.time_column,
        )

        scan_wide_column = breakdown_column in scan_breakdown_column_set

        if event_name:
            ev = events_by_name.get(event_name)
            event_columns = (
                event_breakdown_columns_by_event_id.get(ev.id, set())
                if isinstance(ev, Event)
                else set()
            )
            if isinstance(ev, Event) and (scan_wide_column or breakdown_column in event_columns):
                key = (
                    config.id,
                    ev.id,
                    bucket,
                    breakdown_column,
                    breakdown_value,
                    is_other,
                )
                event_agg[key] = event_agg.get(key, 0) + cnt

        if event_type_id and scan_wide_column:
            key = (
                config.id,
                event_type_id,
                bucket,
                breakdown_column,
                breakdown_value,
                is_other,
            )
            type_agg[key] = type_agg.get(key, 0) + cnt

    event_rows: list[dict[str, object]] = [
        {
            "id": uuid.uuid4(),
            "scan_config_id": sc_id,
            "event_id": ev_id,
            "event_type_id": None,
            "bucket": bucket,
            "breakdown_column": column,
            "breakdown_value": value,
            "is_other": is_other,
            "count": total,
        }
        for (sc_id, ev_id, bucket, column, value, is_other), total in event_agg.items()
    ]
    type_rows: list[dict[str, object]] = [
        {
            "id": uuid.uuid4(),
            "scan_config_id": sc_id,
            "event_id": None,
            "event_type_id": et_id,
            "bucket": bucket,
            "breakdown_column": column,
            "breakdown_value": value,
            "is_other": is_other,
            "count": total,
        }
        for (sc_id, et_id, bucket, column, value, is_other), total in type_agg.items()
    ]
    return event_rows, type_rows, truncated


def _collect_app_version_breakdown_rows(
    *,
    config: ScanConfig,
    regular_cols: list[str],
    rows: list[tuple[object, ...]],
    json_value_names: list[str],
    reg_index: dict[str, int],
    json_index: dict[str, int],
    n_reg: int,
    gen_results: dict[str, GenerationResult],
    single_result: GenerationResult | None,
    et_by_name: dict[str, EventType],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Derive app-version breakdowns from the primary bucketed metric rows.

    ``get_time_bucketed_counts`` already groups by every regular column,
    including ``app_version_column`` when configured. A separate warehouse
    GROUPING SETS query for one version dimension would therefore rescan the
    same source rows at the same granularity.
    """
    version_column = config.app_version_column
    if not version_column:
        return [], []
    if not _is_supported_metric_breakdown_column(
        config,
        column=version_column,
        regular_cols=regular_cols,
    ):
        logger.warning(
            "Skipping unsupported app_version_column %r for scan %s",
            version_column,
            config.id,
        )
        return [], []

    version_idx = reg_index.get(version_column)
    if version_idx is None:
        logger.warning(
            "Skipping app_version_column %r for scan %s: column missing from metric rows",
            version_column,
            config.id,
        )
        return [], []

    et_col_idx = group_column_index(config, reg_index)
    event_counts: dict[tuple[uuid.UUID, uuid.UUID, datetime, str], int] = {}
    type_counts: dict[tuple[uuid.UUID, uuid.UUID, datetime, str], int] = {}

    for row in rows:
        bucket = stored_bucket(row[0])
        data_row = row[1:]
        if version_idx >= len(data_row) - 1:
            continue
        version_value = _normalize_breakdown_value(data_row[version_idx])
        cnt = int(cast(int | str | float, row[-1]))
        col_meta: dict[str, dict[str, object]]
        events_by_name: dict[str, Event]
        event_type_id: uuid.UUID | None

        if et_col_idx is not None:
            et_name = str(data_row[et_col_idx])
            event_type = et_by_name.get(et_name)
            if event_type is None:
                continue
            event_type_id = event_type.id
            gen_result: GenerationResult | None = gen_results.get(et_name)
            if gen_result is None:
                continue
            col_meta = gen_result.col_meta
            events_by_name = gen_result.events_by_name
        else:
            event_type_id = config.event_type_id
            if single_result is None:
                continue
            col_meta = single_result.col_meta
            events_by_name = single_result.events_by_name

        event_name = _build_event_name_from_row(
            data_row,
            col_meta,
            reg_index,
            json_index,
            n_reg,
            json_value_names,
            config.event_name_format,
            config.event_group_rules,
            event_type_column=config.event_type_column,
            time_column=config.time_column,
        )

        if event_name:
            ev = events_by_name.get(event_name)
            if isinstance(ev, Event):
                event_key = (config.id, ev.id, bucket, version_value)
                event_counts[event_key] = event_counts.get(event_key, 0) + cnt

        if event_type_id:
            type_key = (config.id, event_type_id, bucket, version_value)
            type_counts[type_key] = type_counts.get(type_key, 0) + cnt

    event_rows, type_rows = _build_app_version_breakdown_rows(
        version_column=version_column,
        event_counts=event_counts,
        type_counts=type_counts,
    )
    return event_rows, type_rows


def _build_app_version_breakdown_rows(
    *,
    version_column: str,
    event_counts: dict[tuple[uuid.UUID, uuid.UUID, datetime, str], int],
    type_counts: dict[tuple[uuid.UUID, uuid.UUID, datetime, str], int],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Persist one ``is_other=False`` breakdown row per (scope, bucket, version).

    Every distinct version is stored verbatim — no "Other" rollup here. SemVer
    latest-N retention and the "Other" rollup are applied at READ time
    (``metrics_service.get_app_version_series``) over the full requested window.
    Collapsing per chunk at write time made a version flip between its own
    series and "Other" across chunk boundaries (e.g. a transient newer release
    bumping a high-volume older one out of the kept set), producing
    chunk-size-dependent timelines — most visible on replay (many sub-windows)
    versus regular collection (a single small window).
    """
    event_rows: list[dict[str, object]] = [
        {
            "id": uuid.uuid4(),
            "scan_config_id": sc_id,
            "event_id": ev_id,
            "event_type_id": None,
            "bucket": bucket,
            "breakdown_column": version_column,
            "breakdown_value": version,
            "is_other": False,
            "count": total,
        }
        for (sc_id, ev_id, bucket, version), total in event_counts.items()
    ]
    type_rows: list[dict[str, object]] = [
        {
            "id": uuid.uuid4(),
            "scan_config_id": sc_id,
            "event_id": None,
            "event_type_id": et_id,
            "bucket": bucket,
            "breakdown_column": version_column,
            "breakdown_value": version,
            "is_other": False,
            "count": total,
        }
        for (sc_id, et_id, bucket, version), total in type_counts.items()
    ]
    return event_rows, type_rows


def _collect_distribution_drift_rows(
    *,
    adapter: BaseAdapter,
    config: ScanConfig,
    interval_code: str,
    interval_delta: timedelta,
    regular_cols: list[str],
    json_cols: list[str],
    json_value_path_map: dict[str, list[str]],
    time_from: datetime,
    time_to: datetime,
    query_row_limit: int,
    reg_index: dict[str, int],
    et_by_name: dict[str, EventType],
) -> tuple[list[dict[str, object]], int, bool]:
    distribution_fields: list[str] = []
    seen_fields: set[str] = set()
    for configured_field in config.distribution_drift_fields or []:
        if configured_field in seen_fields:
            continue
        seen_fields.add(configured_field)
        if _is_supported_distribution_drift_field(
            config,
            field_name=configured_field,
            regular_cols=regular_cols,
            json_cols=json_cols,
        ) and _within_property_cap(configured_field, distribution_fields):
            distribution_fields.append(configured_field)
            continue
        logger.warning(
            "Skipping unsupported distribution drift field %r for scan %s",
            configured_field,
            config.id,
        )

    if not distribution_fields:
        return [], 0, False

    baseline_window_buckets = max(int(config.baseline_window_buckets or 1), 1)
    min_history_buckets = max(int(config.min_history_buckets or 1), 1)
    history_from = time_from - interval_delta * baseline_window_buckets

    _col_names, _json_value_names, rows = adapter.get_time_bucketed_breakdown_counts_multi(
        scan_source_query(adapter, config),
        config.time_column or "",
        interval_code,
        distribution_fields,
        regular_cols,
        json_cols,
        json_value_path_map,
        history_from,
        time_to,
        values_limit=None,
        limit=query_row_limit + 1,
    )
    truncated = len(rows) > query_row_limit
    rows = rows[:query_row_limit]
    logger.info(
        "Got %s bucketed distribution drift rows for %s from warehouse",
        len(rows),
        ", ".join(distribution_fields),
    )

    # Buckets below are ``stored_bucket`` values, aware UTC, so
    # the window they are compared against is stamped the same way. Only the
    # comparison bounds: the adapter above gets the window exactly as the caller
    # passed it, like every other collector query.
    window_from = to_utc(time_from)
    window_to = to_utc(time_to)

    # Pre-grouped by scope and then by bucket, NOT flat. The analysis below needs
    # one bucket's values and its handful of predecessors at a time; against a
    # flat dict that is a full rescan per (scope, bucket) pair, and the fetch is
    # bounded only by metrics_row_limit (100k rows, two scopes each), so a chunk
    # spent minutes of pure-Python dict iteration inside one Celery task.
    # ``grouped[scope].keys()`` is exactly the set of buckets
    # the scope has data for, so no parallel bucket index is needed.
    grouped: dict[tuple[uuid.UUID | None, str], dict[datetime, dict[str, int]]] = {}
    et_col_idx = group_column_index(config, reg_index)

    def add_count(
        *,
        event_type_id: uuid.UUID | None,
        field_name: str,
        bucket: datetime,
        value: str,
        count: int,
    ) -> None:
        by_bucket = grouped.setdefault((event_type_id, field_name), {})
        bucket_counts = by_bucket.setdefault(bucket, {})
        bucket_counts[value] = bucket_counts.get(value, 0) + count

    for row in rows:
        bucket = stored_bucket(row[0])
        field_name = str(row[1])
        field_value = _normalize_breakdown_value(row[2])
        data_row = row[4:]
        count = int(cast(int | str | float, row[-1]))

        event_type_id: uuid.UUID | None = None
        if et_col_idx is not None:
            event_type_name = str(data_row[et_col_idx])
            event_type = et_by_name.get(event_type_name)
            if event_type is not None:
                event_type_id = event_type.id
        else:
            event_type_id = config.event_type_id

        add_count(
            event_type_id=None,
            field_name=field_name,
            bucket=bucket,
            value=field_value,
            count=count,
        )
        if event_type_id is not None:
            add_count(
                event_type_id=event_type_id,
                field_name=field_name,
                bucket=bucket,
                value=field_value,
                count=count,
            )

    output_rows: list[dict[str, object]] = []
    significant_count = 0
    scopes = sorted(grouped, key=lambda item: (str(item[0] or ""), item[1]))
    for event_type_id, field_name in scopes:
        by_bucket = grouped[(event_type_id, field_name)]
        ordered_buckets = sorted(by_bucket)
        for index, bucket in enumerate(ordered_buckets):
            if bucket < window_from or bucket >= window_to:
                continue

            baseline_from = bucket - interval_delta * baseline_window_buckets
            baseline_counts: dict[str, int] = {}
            # ``min_history_buckets`` counts DISTINCT populated buckets, not rows
            # and not values: one dense bucket must not satisfy a seven-bucket
            # history requirement. Every key of ``by_bucket`` holds at least one
            # value, so counting the keys walked is the same number the old set
            # of seen buckets carried.
            baseline_bucket_count = 0
            # ``ordered_buckets`` ascends, so the predecessors of ``bucket`` are
            # exactly ``ordered_buckets[:index]`` — the old condition was
            # ``baseline_from <= row_bucket < bucket``, half-open on both ends.
            # Walking them backwards lets the window end the scan instead of the
            # dict ending it.
            for prior_bucket in reversed(ordered_buckets[:index]):
                if prior_bucket < baseline_from:
                    break
                baseline_bucket_count += 1
                for value, count in by_bucket[prior_bucket].items():
                    baseline_counts[value] = baseline_counts.get(value, 0) + count

            # A live reference into ``grouped``, not a copy: ``compute_psi`` takes
            # a Mapping and only reads it. Keep it that way.
            current_counts = by_bucket[bucket]

            if baseline_bucket_count < min_history_buckets or not current_counts:
                continue

            result = compute_psi(baseline_counts, current_counts)
            top_movers = _serialize_distribution_top_movers(result.top_movers)
            if result.band == DistributionDriftBand.significant.value:
                significant_count += 1
            output_rows.append(
                {
                    "id": uuid.uuid4(),
                    "scan_config_id": config.id,
                    "event_type_id": event_type_id,
                    "field_name": field_name,
                    "bucket": bucket,
                    "psi": result.psi,
                    "band": result.band,
                    "baseline_total": result.baseline_total,
                    "current_total": result.current_total,
                    "top_movers": top_movers,
                }
            )

    return output_rows, significant_count, truncated


def _drop_non_finite_values(rows: list[dict[str, object]], *, kind: str) -> list[dict[str, object]]:
    """Drop rows whose ``value`` is NaN or ±infinity, logging the count.

    A user SELECT can hand back either — ``0.0/0.0``, ``log(0)``, an overflowing
    ``sum`` — and ``float()`` accepts both. Postgres stores them in a ``double
    precision`` column verbatim; SQLite rewrites NaN to NULL and keeps inf. Once
    stored they poison every consumer downstream: a mean, a stddev, an anomaly
    band and a JSON response all go NaN, and the anomaly detector's thresholds
    stop comparing true.

    DROPPED rather than clamped or zeroed. The surrounding window-delete has
    already cleared the bucket, so it reads as ABSENT — the same outcome as a
    divide-by-zero bucket, which is honest: the warehouse did not answer with a
    number. Writing 0 would draw a dive that never happened.
    """
    finite = [row for row in rows if _is_finite_value(row.get("value"))]
    dropped = len(rows) - len(finite)
    if dropped:
        logger.warning("Dropped %d non-finite %s row(s) before upsert", dropped, kind)
    return finite


def _is_finite_value(value: object) -> bool:
    """Whether this row survives the non-finite filter — NOT "is a valid float".

    False for exactly one thing: a number that is NaN or ±infinity. A ``str``,
    a ``None`` or a ``bool`` is passed through as True even though the Float
    column cannot carry it, because rejecting it here would swallow a type
    error the DB reports precisely (see the body comment). Naming this
    "is a real number the Float column can carry" would invert that at the call
    site, which reads ``if _is_finite_value(row.get("value"))``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # Not a number at all: leave it to the DB, which owns that error.
        return True
    return math.isfinite(value)


def _upsert_metric_values_rows(
    session: Session,
    *,
    rows: list[dict[str, object]],
) -> int:
    """Insert-or-refresh MetricValue rows keyed by (definition, config, bucket).

    Uses the same dialect-aware ON CONFLICT DO UPDATE pattern as the event
    helpers, but generalized over the Float ``value`` column. On conflict only
    ``value`` moves so re-collecting a window overwrites stale values without
    creating duplicates. Non-finite values are dropped first — see
    :func:`_drop_non_finite_values`.

    Returns how many rows were actually written, which is ``len(rows)`` MINUS
    whatever that filter removed. Callers accumulate this instead of the length
    of the list they passed in: a collection whose warehouse answered one bucket
    with ``0.0/0.0`` would otherwise report a value it did not store.
    """
    rows = _drop_non_finite_values(rows, kind="metric value")
    if not rows:
        return 0

    is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
    for chunk in _chunk_rows(rows):
        # Catalog rows use NULL scan_config_id. PostgreSQL (and SQLite) UNIQUE
        # constraints treat NULLs as distinct, so they target the dedicated
        # partial unique index; event-composition rows keep the original full
        # constraint. Splitting also handles a defensive mixed input batch.
        groups = (
            [row for row in chunk if row.get("scan_config_id") is None],
            [row for row in chunk if row.get("scan_config_id") is not None],
        )
        for group in groups:
            if not group:
                continue
            is_catalog = group[0].get("scan_config_id") is None
            if is_sqlite:
                sqlite_stmt = sqlite_insert(MetricValue).values(group)
                if is_catalog:
                    sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                        index_elements=["metric_definition_id", "bucket"],
                        index_where=MetricValue.scan_config_id.is_(None),
                        set_={"value": sqlite_stmt.excluded.value},
                    )
                else:
                    sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                        index_elements=["metric_definition_id", "scan_config_id", "bucket"],
                        set_={"value": sqlite_stmt.excluded.value},
                    )
                session.execute(sqlite_stmt)
                continue

            pg_stmt = pg_insert(MetricValue).values(group)
            if is_catalog:
                pg_stmt = pg_stmt.on_conflict_do_update(
                    index_elements=["metric_definition_id", "bucket"],
                    index_where=MetricValue.scan_config_id.is_(None),
                    set_={"value": pg_stmt.excluded.value},
                )
            else:
                pg_stmt = pg_stmt.on_conflict_do_update(
                    constraint="uq_metric_value_def_config_bucket",
                    set_={"value": pg_stmt.excluded.value},
                )
            session.execute(pg_stmt)
    return len(rows)


def _upsert_metric_value_breakdown_rows(
    session: Session,
    *,
    rows: list[dict[str, object]],
) -> int:
    """Insert-or-refresh MetricValueBreakdown rows.

    Keyed by (definition, config, bucket, breakdown_column, breakdown_value,
    is_other). On conflict ``value`` and ``is_other`` move, mirroring the
    event-breakdown upsert generalized over the Float ``value`` column.
    Non-finite values are dropped first — see :func:`_drop_non_finite_values`.

    Returns the number of rows actually written, for the same reason as
    :func:`_upsert_metric_values_rows`.
    """
    rows = _drop_non_finite_values(rows, kind="metric breakdown")
    if not rows:
        return 0

    is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
    for chunk in _chunk_rows(rows):
        groups = (
            [row for row in chunk if row.get("scan_config_id") is None],
            [row for row in chunk if row.get("scan_config_id") is not None],
        )
        for group in groups:
            if not group:
                continue
            is_catalog = group[0].get("scan_config_id") is None
            if is_sqlite:
                sqlite_stmt = sqlite_insert(MetricValueBreakdown).values(group)
                if is_catalog:
                    sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                        index_elements=[
                            "metric_definition_id",
                            "bucket",
                            "breakdown_column",
                            "breakdown_value",
                            "is_other",
                        ],
                        index_where=MetricValueBreakdown.scan_config_id.is_(None),
                        set_={
                            "value": sqlite_stmt.excluded.value,
                            "is_other": sqlite_stmt.excluded.is_other,
                        },
                    )
                else:
                    sqlite_stmt = sqlite_stmt.on_conflict_do_update(
                        index_elements=[
                            "metric_definition_id",
                            "scan_config_id",
                            "bucket",
                            "breakdown_column",
                            "breakdown_value",
                            "is_other",
                        ],
                        set_={
                            "value": sqlite_stmt.excluded.value,
                            "is_other": sqlite_stmt.excluded.is_other,
                        },
                    )
                session.execute(sqlite_stmt)
                continue

            pg_stmt = pg_insert(MetricValueBreakdown).values(group)
            if is_catalog:
                pg_stmt = pg_stmt.on_conflict_do_update(
                    index_elements=[
                        "metric_definition_id",
                        "bucket",
                        "breakdown_column",
                        "breakdown_value",
                        "is_other",
                    ],
                    index_where=MetricValueBreakdown.scan_config_id.is_(None),
                    set_={
                        "value": pg_stmt.excluded.value,
                        "is_other": pg_stmt.excluded.is_other,
                    },
                )
            else:
                pg_stmt = pg_stmt.on_conflict_do_update(
                    constraint="uq_metric_value_breakdown_def_config_bucket_value",
                    set_={
                        "value": pg_stmt.excluded.value,
                        "is_other": pg_stmt.excluded.is_other,
                    },
                )
            session.execute(pg_stmt)
    return len(rows)


def _delete_metric_values_window(
    session: Session,
    *,
    metric_definition_id: uuid.UUID,
    time_from: datetime,
    time_to: datetime,
    scan_config_id: uuid.UUID | None = None,
) -> int:
    """Delete MetricValue rows for one definition within a half-open window.

    When ``scan_config_id`` is provided the delete is further scoped to that
    source grid (used by ``event_composition`` recollection); otherwise every
    matching definition row in the window is removed.
    """
    stmt = delete(MetricValue).where(
        MetricValue.metric_definition_id == metric_definition_id,
        MetricValue.bucket >= time_from,
        MetricValue.bucket < time_to,
    )
    if scan_config_id is not None:
        stmt = stmt.where(MetricValue.scan_config_id == scan_config_id)
    result = session.execute(stmt)
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)


def _delete_metric_value_breakdowns_window(
    session: Session,
    *,
    metric_definition_id: uuid.UUID,
    time_from: datetime,
    time_to: datetime,
    scan_config_id: uuid.UUID | None = None,
) -> int:
    """Delete MetricValueBreakdown rows for one definition within a window."""
    stmt = delete(MetricValueBreakdown).where(
        MetricValueBreakdown.metric_definition_id == metric_definition_id,
        MetricValueBreakdown.bucket >= time_from,
        MetricValueBreakdown.bucket < time_to,
    )
    if scan_config_id is not None:
        stmt = stmt.where(MetricValueBreakdown.scan_config_id == scan_config_id)
    result = session.execute(stmt)
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount or 0)
