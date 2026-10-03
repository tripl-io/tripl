from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy import func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_METRIC,
    SCOPE_PROJECT_TOTAL,
)
from tripl.core.intervals import get_interval
from tripl.metric_grid import metric_grid_stmt, metric_grids
from tripl.metric_monitoring import monitored_metric_criteria
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import (
    AlertInboxStatus,
    MetricBreakdownAnomalyKind,
    SignalVerdict,
)
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.scan_config import ScanConfig
from tripl.schemas.event_metric import (
    BreakdownTimelinePoint,
    BreakdownTimelineResponse,
    DistributionDriftPoint,
    DistributionDriftsResponse,
    DistributionDriftTopMover,
    MetricSignalResponse,
    SeasonalityCell,
    SeasonalityHeatmapResponse,
    SignalSeriesResponse,
    SignalSeriesScope,
    SignalVerdictCountsResponse,
    TopMoverItem,
)
from tripl.services import (
    alerting_service,
    anomaly_attribution_service,
    signal_triage_service,
)
from tripl.services.metrics_service import (
    _get_anomaly_rows,
    _get_metric_rows,
    _get_project_recent_signal_window,
    _get_project_recent_signal_windows,
    _resolve_event,
    _resolve_event_type,
    _resolve_project,
    _resolve_scope_scan_config_id,
    _signal_from_anomaly,
)
from tripl.services.monitoring_utils import (
    classify_signal_state,
    latest_bucket_by_scan,
    scan_interval_to_timedelta,
    scan_liveness_cutoff,
)
from tripl.services.signal_triage_service import SignalKey
from tripl.worker.analyzers.metric_value_kind import is_count_shaped


async def _get_latest_anomaly_rows_multi(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope_types: list[str],
    event_ids: list[uuid.UUID] | None = None,
) -> list[MetricAnomaly]:
    """Latest MetricAnomaly row per (scan_config_id, scope_type, scope_ref),
    across all requested scope_types in a single round-trip."""
    if not scope_types:
        return []
    latest_subquery = (
        select(
            MetricAnomaly.scan_config_id.label("scan_config_id"),
            MetricAnomaly.scope_type.label("scope_type"),
            MetricAnomaly.scope_ref.label("scope_ref"),
            func.max(MetricAnomaly.bucket).label("bucket"),
        )
        .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .where(
            ScanConfig.project_id == project_id,
            MetricAnomaly.scope_type.in_(scope_types),
            # Expected by a planned event (F18): drawn on the chart, never open.
            MetricAnomaly.planned_event_id.is_(None),
        )
        .group_by(
            MetricAnomaly.scan_config_id,
            MetricAnomaly.scope_type,
            MetricAnomaly.scope_ref,
        )
        .subquery()
    )
    query = (
        select(MetricAnomaly)
        .join(
            latest_subquery,
            (MetricAnomaly.scan_config_id == latest_subquery.c.scan_config_id)
            & (MetricAnomaly.scope_type == latest_subquery.c.scope_type)
            & (MetricAnomaly.scope_ref == latest_subquery.c.scope_ref)
            & (MetricAnomaly.bucket == latest_subquery.c.bucket),
        )
        .order_by(MetricAnomaly.bucket.desc())
    )
    if SCOPE_EVENT in scope_types and event_ids is not None:
        # Keep other scope_types unfiltered; restrict only the EVENT-scope rows.
        query = query.where(
            (MetricAnomaly.scope_type != SCOPE_EVENT) | MetricAnomaly.event_id.in_(event_ids)
        )
    result = await session.execute(query)
    return list(result.scalars().all())


async def _get_latest_metric_buckets_multi(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope_types: list[str],
    event_ids: list[uuid.UUID] | None = None,
) -> dict[tuple[uuid.UUID, str, str], datetime]:
    """Single-round-trip UNION of the scope-specific metric-bucket queries.

    ``scope_ref`` stays as a native UUID in the SQL layer (all three branches
    project a UUID column), and we stringify once in Python — this matches
    ``MetricAnomaly.scope_ref`` which is also ``str(uuid)`` (hyphenated). A
    SQL-level ``CAST(uuid AS VARCHAR)`` would diverge between dialects
    (SQLite returns 32-char hex, Postgres returns hyphenated) and break
    key-matching against MetricAnomaly on SQLite tests.
    """
    subs: list[Any] = []
    if SCOPE_PROJECT_TOTAL in scope_types:
        subs.append(
            select(
                EventMetric.scan_config_id.label("scan_config_id"),
                literal(SCOPE_PROJECT_TOTAL).label("scope_type"),
                EventMetric.scan_config_id.label("scope_ref_uuid"),
                func.max(EventMetric.bucket).label("bucket"),
            )
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                ScanConfig.project_id == project_id,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id.is_not(None),
            )
            .group_by(EventMetric.scan_config_id)
        )
    if SCOPE_EVENT_TYPE in scope_types:
        subs.append(
            select(
                EventMetric.scan_config_id.label("scan_config_id"),
                literal(SCOPE_EVENT_TYPE).label("scope_type"),
                EventMetric.event_type_id.label("scope_ref_uuid"),
                func.max(EventMetric.bucket).label("bucket"),
            )
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                ScanConfig.project_id == project_id,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id.is_not(None),
            )
            .group_by(EventMetric.scan_config_id, EventMetric.event_type_id)
        )
    if SCOPE_EVENT in scope_types:
        event_sub = (
            select(
                EventMetric.scan_config_id.label("scan_config_id"),
                literal(SCOPE_EVENT).label("scope_type"),
                EventMetric.event_id.label("scope_ref_uuid"),
                func.max(EventMetric.bucket).label("bucket"),
            )
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                ScanConfig.project_id == project_id,
                EventMetric.event_id.is_not(None),
            )
            .group_by(EventMetric.scan_config_id, EventMetric.event_id)
        )
        if event_ids:
            event_sub = event_sub.where(EventMetric.event_id.in_(event_ids))
        subs.append(event_sub)

    if not subs:
        return {}

    combined = union_all(*subs).subquery()
    rows = (
        await session.execute(
            select(
                combined.c.scan_config_id,
                combined.c.scope_type,
                combined.c.scope_ref_uuid,
                combined.c.bucket,
            )
        )
    ).all()
    return {
        (scan_config_id, scope_type, str(scope_ref_uuid)): bucket
        for scan_config_id, scope_type, scope_ref_uuid, bucket in rows
        if scope_ref_uuid is not None
    }


async def _get_active_metric_signals(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    recent_window: timedelta | None = None,
) -> list[MetricSignalResponse]:
    """Open ``metric``-scope signals for a project's MONITORED catalog metrics.

    Monitored is ``active`` AND ``anomaly_detection_enabled``
    (``tripl.metric_monitoring``), the same predicate detection scores on.

    Catalog metric anomalies carry a NULL ``scan_config_id`` and are keyed by
    ``scope_ref = str(metric_definition_id)``, so they are loaded on their own
    (the event-scope multi-query joins ScanConfig on scan_config_id and would
    drop them). Each metric's newest anomaly is classified against its latest
    stored value bucket and surfaces while that classification is open — either
    state, ``latest_scan`` or ``recent``. (It said "only latest_scan" for a
    while; ingestion settling makes that branch unreachable for a scope that is
    still emitting, so it would have described an empty set.)

    ``recent_window`` is the project's configured freshness window; callers that
    already hold it pass it in so this does not re-query, and omitting it keeps
    the default 24h window.
    """
    # The grid rides along because each metric is scored on its OWN grid, so its
    # freshness window has to be measured on that grid too — including the one an
    # ``event_composition`` metric inherits from its source scan, which its own
    # (NULL) ``interval`` column does not carry.
    grids = metric_grids(
        (
            await session.execute(
                metric_grid_stmt(
                    MetricDefinition.project_id == project_id,
                    *monitored_metric_criteria(),
                )
            )
        ).all()
    )
    if not grids:
        return []
    metric_ids = list(grids)
    interval_by_ref = {str(metric_id): grid.interval for metric_id, grid in grids.items()}

    scope_refs = [str(metric_id) for metric_id in metric_ids]
    latest_value_buckets: dict[str, datetime] = {
        str(metric_definition_id): bucket
        for metric_definition_id, bucket in (
            await session.execute(
                select(MetricValue.metric_definition_id, func.max(MetricValue.bucket))
                .where(MetricValue.metric_definition_id.in_(metric_ids))
                .group_by(MetricValue.metric_definition_id)
            )
        ).all()
    }

    latest_anomalies: dict[str, MetricAnomaly] = {}
    for anomaly in (
        await session.execute(
            select(MetricAnomaly)
            .where(
                MetricAnomaly.scope_type == SCOPE_METRIC,
                MetricAnomaly.scope_ref.in_(scope_refs),
                MetricAnomaly.planned_event_id.is_(None),
            )
            .order_by(MetricAnomaly.bucket.desc())
        )
    ).scalars():
        latest_anomalies.setdefault(anomaly.scope_ref, anomaly)

    if recent_window is None:
        recent_window = await _get_project_recent_signal_window(session, project_id)

    signals: list[MetricSignalResponse] = []
    for scope_ref, anomaly in latest_anomalies.items():
        state = classify_signal_state(
            anomaly_bucket=anomaly.bucket,
            latest_metric_bucket=latest_value_buckets.get(scope_ref),
            interval=scan_interval_to_timedelta(interval_by_ref.get(scope_ref)),
            recent_window=recent_window,
        )
        if state is not None:
            signals.append(_signal_from_anomaly(anomaly, state=state))
    return signals


# Magnitude gate shared with the AnomaliesPage default "Significant" filter
# (frontend AnomaliesPage.MAGNITUDE_PRESETS -> minRelEffect 0.5). The sidebar /
# Overview badge (``monitoring_signal_count``) counts a signal only when its
# relative effect clears this bar, so the badge equals the page's default
# (significant) open-signal count across every scope (tripl-yfsj.1).
SIGNIFICANT_MIN_REL_EFFECT = 0.5

# The magnitude a move off a zero baseline is reported as, and the ceiling every
# other magnitude is clamped to. It has to be FINITE: this number is shipped on
# ``MetricSignalResponse.relative_effect``, and Pydantic serializes ``inf`` to
# JSON null, so an unbounded value reached the client as "not computed" and the
# client re-derived a count-shaped estimate that lands below the gate. The badge,
# which gates in-process where ``inf`` still compares, counted such a signal while
# every page it links to hid it (tripl-l33u.3). Far above the highest magnitude
# preset ("Major", 1.0) so it cannot be mistaken for a measured ratio.
MAX_RELATIVE_EFFECT = 1e6


def relative_effect(actual: float, expected: float, *, count_shaped: bool = True) -> float:
    """How big a signal is, relative to what was expected.

    For a COUNT the denominator is floored at 1: dividing by a sub-unit
    expectation would let a scope with essentially no traffic outrank a real
    move on a busy one, which is why the floor is there and why it stays.

    A fractional series is not a count, and the floor is a category error on it.
    A conversion ratio expected at 0.12 and observed at 0.04 is a two-thirds
    collapse; divided by 1 it scores 0.08 and never clears the 0.5 gate, so the
    metric anomalies the detector deliberately admits (``detect.py`` drops the
    zero-fill and min-expected gates for fractional metrics) were invisible at
    the default magnitude level (tripl-yf8c). Shape is decided the one way the
    codebase already decides it, ``metric_value_kind.is_count_shaped``.

    Measured when this was written: of 191 open signals on production, all were
    event scope and none were affected — the fault was latent, waiting for the
    first percent-unit metric to move.

    Every branch is clamped to ``MAX_RELATIVE_EFFECT`` because the result travels
    over JSON, where a non-finite float becomes null (see that constant).
    """
    if count_shaped:
        return min(abs(actual - expected) / max(expected, 1.0), MAX_RELATIVE_EFFECT)
    if expected == 0:
        # No baseline to be relative to. Anything non-zero is unbounded change;
        # nothing from nothing is no change.
        return MAX_RELATIVE_EFFECT if actual != 0 else 0.0
    # A near-zero (but non-zero) fractional baseline overflows to inf on its own.
    return min(abs(actual - expected) / abs(expected), MAX_RELATIVE_EFFECT)


def is_significant_signal(actual: float, expected: float, *, count_shaped: bool = True) -> bool:
    """Whether a signal clears the shared "Significant" magnitude threshold."""
    return (
        relative_effect(actual, expected, count_shaped=count_shaped) >= SIGNIFICANT_MIN_REL_EFFECT
    )


async def _count_active_metric_signals_by_project(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Batched per-project count of open ``metric``-scope signals.

    The size of :func:`_active_metric_signals_by_project` per project; callers
    that also need to drop triaged (hidden) signals read the keys instead.
    """
    return {
        project_id: len(keys)
        for project_id, keys in (
            await _active_metric_signals_by_project(session, project_ids)
        ).items()
    }


async def _active_metric_signals_by_project(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[tuple[str, datetime]]]:
    """Batched per-project ``(scope_ref, bucket)`` of open, significant ``metric``-scope signals.

    A single-project :func:`_get_active_metric_signals` call issues three queries;
    the ProjectsPage / sidebar badge needs these counts for N projects at once, so
    this loads the same three datasets once for all projects and classifies in
    Python (O(1) queries in N). Catalog-metric anomalies carry a NULL
    ``scan_config_id`` and are keyed by ``scope_ref = str(metric_definition_id)``,
    so project ownership and the latest stored value bucket are resolved on the
    hyphenated ``str(uuid)`` keys the anomalies store — a SQL-level cast would
    diverge SQLite vs Postgres (see ``_get_latest_metric_buckets_multi``).
    Classification reuses ``classify_signal_state`` exactly as
    :func:`_get_active_metric_signals` does — each metric measured on its own
    grid (``tripl.metric_grid``) and each project's own configured freshness
    window applied. That parity was claimed here long before it was true: this
    query did not even select an interval, so every catalog metric was judged
    against a bare 24h window and a DAILY metric read OPEN on the Anomalies page
    and ZERO on the badge (tripl-l429.17).
    """
    if not project_ids:
        return {}
    grids = metric_grids(
        (
            await session.execute(
                metric_grid_stmt(
                    MetricDefinition.project_id.in_(project_ids),
                    *monitored_metric_criteria(),
                )
            )
        ).all()
    )
    if not grids:
        return {}
    grid_by_scope_ref = {str(metric_id): grid for metric_id, grid in grids.items()}
    metric_ids = list(grids)
    scope_refs = list(grid_by_scope_ref)

    latest_value_buckets: dict[str, datetime] = {
        str(metric_definition_id): bucket
        for metric_definition_id, bucket in (
            await session.execute(
                select(MetricValue.metric_definition_id, func.max(MetricValue.bucket))
                .where(MetricValue.metric_definition_id.in_(metric_ids))
                .group_by(MetricValue.metric_definition_id)
            )
        ).all()
    }

    # This counter sees ONLY metric scopes, which is the population where the
    # magnitude floor is a category error — a ratio expected at 0.12 is not a
    # count (tripl-yf8c). One batched lookup, same rule the detector uses.
    fractional_refs = {
        str(metric.id)
        for metric in (
            await session.execute(
                select(MetricDefinition).where(MetricDefinition.id.in_(metric_ids))
            )
        ).scalars()
        if not is_count_shaped(metric)
    }

    latest_anomalies: dict[str, MetricAnomaly] = {}
    for anomaly in (
        await session.execute(
            select(MetricAnomaly)
            .where(
                MetricAnomaly.scope_type == SCOPE_METRIC,
                MetricAnomaly.scope_ref.in_(scope_refs),
                MetricAnomaly.planned_event_id.is_(None),
            )
            .order_by(MetricAnomaly.bucket.desc())
        )
    ).scalars():
        latest_anomalies.setdefault(anomaly.scope_ref, anomaly)

    # One query for every project's configured freshness window, so the loop below
    # stays free of per-signal (and per-project) lookups.
    recent_windows = await _get_project_recent_signal_windows(session, project_ids)

    now = datetime.now(UTC)
    keys: dict[uuid.UUID, list[tuple[str, datetime]]] = defaultdict(list)
    for scope_ref, anomaly in latest_anomalies.items():
        grid = grid_by_scope_ref.get(scope_ref)
        project_id = grid.project_id if grid is not None else None
        state = classify_signal_state(
            anomaly_bucket=anomaly.bucket,
            latest_metric_bucket=latest_value_buckets.get(scope_ref),
            now=now,
            interval=scan_interval_to_timedelta(grid.interval if grid is not None else None),
            recent_window=recent_windows.get(project_id) if project_id is not None else None,
        )
        if (
            state is not None
            and project_id is not None
            and is_significant_signal(
                anomaly.actual_count,
                anomaly.expected_count,
                count_shaped=scope_ref not in fractional_refs,
            )
        ):
            keys[project_id].append((scope_ref, anomaly.bucket))
    return dict(keys)


IncidentKey = tuple[uuid.UUID | None, datetime, str]


class _IncidentSignal(Protocol):
    """The signal shape the incident-dedup rule reads (duck-typed).

    Satisfied by both ``MetricSignalResponse`` (the AnomaliesPage list) and
    ``MetricAnomaly``/``ProjectLatestSignal`` (the sidebar count), so one rule
    can serve both call sites. Members are read-only ``@property`` so they are
    covariant — the concrete types use StrEnum ``scope_type``/``direction`` and a
    non-optional ``scan_config_id``, all subtypes of the shapes below.
    """

    @property
    def scope_type(self) -> str: ...
    @property
    def scan_config_id(self) -> uuid.UUID | None: ...
    @property
    def bucket(self) -> datetime: ...
    @property
    def direction(self) -> str: ...


def incident_parent_keys(signals: Iterable[_IncidentSignal]) -> set[IncidentKey]:
    """The ``(scan_config_id, bucket, direction)`` keys a project_total scope owns.

    A real spike/drop on the project total trips its child ``event_type``/``event``
    scopes on the SAME scan, bucket and direction — one incident, many rows. These
    keys mark that incident so the child rows can be folded into the single
    project_total signal. Shared by the AnomaliesPage list
    (``_deduplicate_into_incidents``) and the sidebar badge count
    (``project_service._populate_monitoring_signals``) so they cannot drift.
    """
    return {
        (signal.scan_config_id, signal.bucket, signal.direction)
        for signal in signals
        if signal.scope_type == SCOPE_PROJECT_TOTAL
    }


def is_incident_child(signal: _IncidentSignal, parent_keys: set[IncidentKey]) -> bool:
    """True when ``signal`` is a child scope collapsed under a project_total parent.

    A ``project_total`` row is never its own child. A non-project_total row is a
    child only when a project_total signal shares its
    ``(scan_config_id, bucket, direction)``. ``metric``-scope signals carry a NULL
    ``scan_config_id`` and never match a parent, so they always pass through.
    """
    if signal.scope_type == SCOPE_PROJECT_TOTAL:
        return False
    return (signal.scan_config_id, signal.bucket, signal.direction) in parent_keys


def _deduplicate_into_incidents(
    signals: list[MetricSignalResponse],
) -> list[MetricSignalResponse]:
    """Collapse one incident's cross-scope fan-out into a single active signal.

    Keep the parent ``project_total`` row and suppress the child ``event_type``/
    ``event`` rows that share its ``(scan_config_id, bucket, direction)``. The
    per-scope anomaly rows stay in the DB for drill-down; they are just not
    surfaced here as separate active signals. The dedup rule lives in
    ``incident_parent_keys``/``is_incident_child`` so the sidebar badge count
    (``project_service._populate_monitoring_signals``) applies exactly the same rule.
    """
    parent_keys = incident_parent_keys(signals)
    if not parent_keys:
        return signals
    return [signal for signal in signals if not is_incident_child(signal, parent_keys)]


def _flag_incident_children(
    signals: list[MetricSignalResponse],
) -> list[MetricSignalResponse]:
    """Tag (rather than drop) each incident's child scopes.

    Same incident rule as ``_deduplicate_into_incidents`` — a child
    ``event_type``/``event`` row sharing a ``project_total``'s
    ``(scan_config_id, bucket, direction)`` — but the expanded AnomaliesPage
    keeps the child rows and marks them ``incident_child=True`` so the UI can
    render the full spike/drop breakdown while still signposting the rollup.
    """
    parent_keys = incident_parent_keys(signals)
    if not parent_keys:
        return signals
    return [
        signal.model_copy(update={"incident_child": True})
        if is_incident_child(signal, parent_keys)
        else signal
        for signal in signals
    ]


async def _attach_derived_fields(
    session: AsyncSession,
    signals: list[MetricSignalResponse],
) -> list[MetricSignalResponse]:
    """Resolve each signal's display name into the response that carries it.

    Three batched queries for the whole page — never one per signal. Same
    resolution the alert pipeline does for its candidates
    (``alerting_service._build_scope_name_map``): event -> ``Event.name``,
    event_type -> ``EventType.display_name``, metric -> the catalog metric's
    ``display_name``. ``project_total`` has no lookup (the scope IS the project)
    and stays NULL, as does any scope whose entity has been deleted — the FKs
    are ``ondelete=SET NULL``, so a NULL ``event_id`` next to a populated
    ``scope_ref`` means the event is gone, and ``scope_ref`` is the one thing a
    client must not print in its place (tripl-y4wt).
    """
    event_ids = {
        signal.event_id
        for signal in signals
        if signal.scope_type == SCOPE_EVENT and signal.event_id is not None
    }
    event_type_ids = {
        signal.event_type_id
        for signal in signals
        if signal.scope_type == SCOPE_EVENT_TYPE and signal.event_type_id is not None
    }
    # ``scope_ref`` is an unconstrained string while MetricDefinition.id is a
    # UUID, so parse defensively and let anything unparseable resolve to NULL.
    metric_ids: set[uuid.UUID] = set()
    for signal in signals:
        if signal.scope_type != SCOPE_METRIC:
            continue
        try:
            metric_ids.add(uuid.UUID(signal.scope_ref))
        except ValueError:
            continue

    event_names: dict[uuid.UUID, str] = {}
    if event_ids:
        rows = await session.execute(select(Event.id, Event.name).where(Event.id.in_(event_ids)))
        event_names = {row_id: name for row_id, name in rows.all()}
    event_type_names: dict[uuid.UUID, str] = {}
    if event_type_ids:
        rows = await session.execute(
            select(EventType.id, EventType.display_name).where(EventType.id.in_(event_type_ids))
        )
        event_type_names = {row_id: name for row_id, name in rows.all()}
    metric_names: dict[uuid.UUID, str] = {}
    metric_units: dict[uuid.UUID, str | None] = {}
    # Shape, not just name: the magnitude below divides a fractional series by
    # its real baseline and a count by a floored one (tripl-yf8c).
    fractional_metric_ids: set[uuid.UUID] = set()
    if metric_ids:
        metrics = (
            await session.execute(
                select(MetricDefinition).where(MetricDefinition.id.in_(metric_ids))
            )
        ).scalars()
        for metric in metrics:
            metric_names[metric.id] = metric.display_name
            metric_units[metric.id] = metric.unit
            if not is_count_shaped(metric):
                fractional_metric_ids.add(metric.id)

    def resolve(signal: MetricSignalResponse) -> str | None:
        if signal.scope_type == SCOPE_EVENT:
            return event_names.get(signal.event_id) if signal.event_id else None
        if signal.scope_type == SCOPE_EVENT_TYPE:
            return event_type_names.get(signal.event_type_id) if signal.event_type_id else None
        if signal.scope_type == SCOPE_METRIC:
            try:
                return metric_names.get(uuid.UUID(signal.scope_ref))
            except ValueError:
                return None
        return None

    def count_shaped(signal: MetricSignalResponse) -> bool:
        """Only a catalog metric can be fractional; every other scope is a count."""
        if signal.scope_type != SCOPE_METRIC:
            return True
        try:
            return uuid.UUID(signal.scope_ref) not in fractional_metric_ids
        except ValueError:
            return True

    def unit_of(signal: MetricSignalResponse) -> str | None:
        """Only a catalog metric carries a unit; every other scope counts events."""
        if signal.scope_type != SCOPE_METRIC:
            return None
        try:
            return metric_units.get(uuid.UUID(signal.scope_ref))
        except ValueError:
            return None

    return [
        signal.model_copy(
            update={
                "scope_name": resolve(signal),
                "unit": unit_of(signal),
                "relative_effect": relative_effect(
                    signal.actual_count,
                    signal.expected_count,
                    count_shaped=count_shaped(signal),
                ),
            }
        )
        for signal in signals
    ]


async def get_active_signals(
    session: AsyncSession,
    slug: str,
    event_ids: list[uuid.UUID] | None = None,
    *,
    expanded: bool = False,
) -> list[MetricSignalResponse]:
    # Cache only the unfiltered variants (the "all signals for project" fetches:
    # collapsed for top-bar/overview/events, expanded for the AnomaliesPage).
    # Filtered variants have too many permutations — pass through.
    cacheable = not event_ids
    project = await _resolve_project(session, slug)
    cache_key = (
        cache.key_signals_all_expanded(project.id)
        if expanded
        else cache.key_signals_all(project.id)
    )
    if cacheable:
        cached = await cache.get_json(cache_key)
        if cached is not None:
            cached_signals = [MetricSignalResponse.model_validate(item) for item in cached]
            return await _with_live_state(session, project.id, cached_signals, expanded=expanded)

    scope_types = [SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE]
    # Expanded (AnomaliesPage) pulls every per-event scope even without an
    # event-id filter; the collapsed callers stay on total + event_type only.
    if event_ids or expanded:
        scope_types.append(SCOPE_EVENT)

    # Two round-trips (anomalies + metrics) for all scope types combined,
    # instead of 2×per-scope (4–6 RTTs) in the old loop.
    latest_anomalies = await _get_latest_anomaly_rows_multi(
        session, project_id=project.id, scope_types=scope_types, event_ids=event_ids
    )
    latest_metrics = await _get_latest_metric_buckets_multi(
        session, project_id=project.id, scope_types=scope_types, event_ids=event_ids
    )

    # A long-interval (daily/weekly) scan needs its freshness horizon widened to
    # max(24h, 3*interval); without the interval, classify_signal_state falls back
    # to the default 24h window and prematurely closes a still-open daily signal.
    # Load the intervals for this batch's scan_configs in one query.
    scan_config_ids = {
        anomaly.scan_config_id for anomaly in latest_anomalies if anomaly.scan_config_id is not None
    }
    interval_map: dict[uuid.UUID, str | None] = {}
    if scan_config_ids:
        interval_map = {
            scan_config_id: interval
            for scan_config_id, interval in (
                await session.execute(
                    select(ScanConfig.id, ScanConfig.interval).where(
                        ScanConfig.id.in_(scan_config_ids)
                    )
                )
            ).all()
        }

    # One lookup for the project's configured open-signal window, reused by every
    # anomaly below and by the catalog-metric pass — these loops run over hundreds
    # of rows, so it must never be resolved per signal.
    recent_window = await _get_project_recent_signal_window(session, project.id)

    # Scan liveness, derived from the rows already loaded above (no extra query):
    # an outage anchor is only still open while its scan is still collecting
    # SOMETHING. Same rule as the sidebar badge, via the shared helper.
    scan_latest_buckets = latest_bucket_by_scan(
        (scan_config_id, bucket)
        for (scan_config_id, _scope_type, _scope_ref), bucket in latest_metrics.items()
    )

    now = datetime.now(UTC)
    signals: list[MetricSignalResponse] = []
    for anomaly in latest_anomalies:
        # Event-scope rows always carry a scan_config_id (the multi-query joins
        # ScanConfig); the NULL-config ``metric`` scope is handled separately.
        if anomaly.scan_config_id is None:
            continue
        key = (anomaly.scan_config_id, anomaly.scope_type, anomaly.scope_ref)
        latest_metric_bucket = latest_metrics.get(key)
        state = classify_signal_state(
            anomaly_bucket=anomaly.bucket,
            latest_metric_bucket=latest_metric_bucket,
            now=now,
            interval=scan_interval_to_timedelta(interval_map.get(anomaly.scan_config_id)),
            recent_window=recent_window,
            # An outage announced once and never re-emitted is re-checked against
            # the current series rather than its own age (tripl-l429.15) — but
            # only while the anchor had volume to lose, or a scope expected to
            # emit nothing would sit in this list, and in the "N of M open"
            # denominator it feeds, forever (tripl-wkwv.4).
            anomaly_actual_count=anomaly.actual_count,
            anomaly_expected_count=anomaly.expected_count,
            scan_latest_bucket=scan_latest_buckets.get(anomaly.scan_config_id),
        )
        if state is not None:
            signals.append(_signal_from_anomaly(anomaly, state=state))

    # Catalog metric signals are project-global (NULL scan_config_id) and so are
    # loaded separately from the event-scope multi-query above.
    signals.extend(
        await _get_active_metric_signals(
            session,
            project_id=project.id,
            recent_window=recent_window,
        )
    )

    # One incident is one active signal. The collapsed list drops the child
    # event_type/event rows a project-total spike/drop trips on the same bucket;
    # the expanded list keeps them (tagged ``incident_child``) so the
    # AnomaliesPage can show the full breakdown.
    signals = _flag_incident_children(signals) if expanded else _deduplicate_into_incidents(signals)

    # After the collapse, so the name lookups only cover rows that will be
    # returned, and before the cache write, so a cache hit is labelled too.
    signals = await _attach_derived_fields(session, signals)

    signals.sort(key=lambda signal: signal.bucket, reverse=True)
    if cacheable:
        await cache.set_json(
            cache_key,
            [signal.model_dump(mode="json") for signal in signals],
            ttl_seconds=30,
        )
    return await _with_live_state(session, project.id, signals, expanded=expanded)


def _is_significant(signal: MetricSignalResponse) -> bool:
    """The badge's magnitude gate, on an already-listed signal.

    Reads the ``relative_effect`` the list computed (shape-aware: a fractional
    metric is not floored like a count); falls back to the count-shaped rule
    the badge applies to scan scopes when it is missing.
    """
    if signal.relative_effect is not None:
        return signal.relative_effect >= SIGNIFICANT_MIN_REL_EFFECT
    return is_significant_signal(signal.actual_count, signal.expected_count)


def _needs_verdict(signal: MetricSignalResponse) -> bool:
    return signal.verdict is None and not signal.hidden and _is_significant(signal)


def needing_verdict(signals: list[MetricSignalResponse]) -> list[MetricSignalResponse]:
    """The signals still waiting for a verdict: none set, none read off an
    incident, not hidden by a mute, and significant by the same magnitude gate
    as the sidebar badge (``is_significant_signal``), so the filter lists
    exactly what the badge counts (F01, #254). The Anomalies page's default
    filter (``?needs_verdict=true``)."""
    return [signal for signal in signals if _needs_verdict(signal)]


async def get_signal_verdict_counts(
    session: AsyncSession, slug: str
) -> SignalVerdictCountsResponse:
    """Verdict tallies over the project's open signals (the expanded list).

    The population is the Anomalies page's, hidden signals included, so an
    ``expected`` signal (which is hidden) still counts as expected; a signal
    hidden only by a mute has no verdict and is in no bucket. ``needs_verdict``
    applies the badge's magnitude gate too, so it equals the sidebar badge.
    """
    signals = await get_active_signals(session, slug, expanded=True)
    counts: dict[str, int] = {verdict.value: 0 for verdict in SignalVerdict}
    needs = 0
    for signal in signals:
        if signal.verdict is not None:
            counts[signal.verdict.verdict.value] += 1
        elif _needs_verdict(signal):
            needs += 1
    return SignalVerdictCountsResponse(
        needs_verdict=needs,
        expected=counts[SignalVerdict.expected.value],
        tracking_bug=counts[SignalVerdict.tracking_bug.value],
        false_positive=counts[SignalVerdict.false_positive.value],
        real_issue=counts[SignalVerdict.real_issue.value],
    )


async def _with_live_state(
    session: AsyncSession,
    project_id: uuid.UUID,
    signals: list[MetricSignalResponse],
    *,
    expanded: bool,
) -> list[MetricSignalResponse]:
    """The per-fetch state layered over the (cacheable) signal list.

    Incident refs (one lookup for the list), then triage (MO-4 / JR-5) and
    verdicts (F01, #254) on both lists: the collapsed list drops muted /
    expected signals outright, because the top bar, Overview and Events count
    what it returns; the expanded list keeps them flagged ``hidden`` so the
    Anomalies page can offer "Show hidden (n)". ``incident_id`` /
    ``incident_status`` stay an expanded-list field; ``incident`` is on both.
    """
    if not signals:
        return signals
    refs = await alerting_service.incident_refs_for_signals(
        session,
        project_id,
        (
            (signal.scan_config_id, signal.scope_type, signal.scope_ref, signal.bucket)
            for signal in signals
        ),
    )
    if expanded:
        signals = _with_incident_refs(signals, refs)
    triaged = await signal_triage_service.apply_triage(
        session,
        project_id,
        signals,
        drop_hidden=not expanded,
        refs=refs,
    )
    # "Why did it change?" (F02, #255), after the drop so only returned rows
    # are looked up.
    attributed = await anomaly_attribution_service.attach_attributions(session, triaged)
    # Owners (F07, #260) on the expanded list only: the Anomalies page shows
    # them, while the collapsed list feeds counts that never read them.
    if expanded:
        return await anomaly_attribution_service.attach_owners(session, project_id, attributed)
    return attributed


def _with_incident_refs(
    signals: list[MetricSignalResponse],
    refs: Mapping[SignalKey, alerting_service.SignalIncidentRef],
) -> list[MetricSignalResponse]:
    """Copies of ``signals`` naming the inbox incident each was routed into (JR-6).

    Applied after the cache read and after the cache write, never before: an
    incident's status moves with triage, which invalidates nothing here, so a
    cached status would lag the inbox for the cache's lifetime.
    """
    if not refs:
        return signals
    out: list[MetricSignalResponse] = []
    for signal in signals:
        ref = refs.get(
            (
                signal.scan_config_id,
                str(signal.scope_type),
                signal.scope_ref,
                _as_utc(signal.bucket),
            )
        )
        out.append(
            signal
            if ref is None
            else signal.model_copy(
                update={
                    "incident_id": ref.correlation_group_id,
                    "incident_status": AlertInboxStatus(ref.status),
                }
            )
        )
    return out


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def get_top_movers(
    session: AsyncSession,
    slug: str,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    bucket: datetime,
    limit: int = 10,
) -> list[TopMoverItem]:
    """Return the breakdown rows that "moved the most" for a given anomaly.

    Picks MetricBreakdownAnomaly rows on the same scope and bucket as the
    provided anomaly key, ordered by |z_score| descending. Used by the UI
    to render the "why did it move" panel on MonitoringDetailPage.
    """
    project = await _resolve_project(session, slug)
    scan_config = (
        await session.execute(
            select(ScanConfig).where(
                ScanConfig.id == scan_config_id,
                ScanConfig.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if scan_config is None:
        raise HTTPException(404, "Scan config not found")

    query = select(MetricBreakdownAnomaly).where(
        MetricBreakdownAnomaly.scan_config_id == scan_config_id,
        MetricBreakdownAnomaly.scope_type == scope_type,
        MetricBreakdownAnomaly.scope_ref == scope_ref,
        MetricBreakdownAnomaly.bucket == bucket,
        MetricBreakdownAnomaly.kind == MetricBreakdownAnomalyKind.volume,
    )
    if scan_config.app_version_column:
        # Version rollout curves are observational and use the dedicated
        # release-regression detector. Do not let legacy version markers appear
        # as causal movers for an otherwise valid aggregate alert.
        query = query.where(
            MetricBreakdownAnomaly.breakdown_column != scan_config.app_version_column
        )
    rows = (
        (
            await session.execute(
                query.order_by(func.abs(MetricBreakdownAnomaly.z_score).desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return [
        TopMoverItem(
            breakdown_column=row.breakdown_column,
            breakdown_value=row.breakdown_value,
            is_other=row.is_other,
            actual_count=row.actual_count,
            expected_count=row.expected_count,
            stddev=row.stddev,
            z_score=row.z_score,
            direction=row.direction,
        )
        for row in rows
    ]


async def get_seasonality_heatmap(
    session: AsyncSession,
    slug: str,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    time_from: datetime | None,
    time_to: datetime | None,
) -> SeasonalityHeatmapResponse:
    """Aggregate counts and anomaly hits by (weekday, hour).

    Aggregation runs in Python so the same code path works on Postgres and
    SQLite — the windowed datasets are bounded (max ~90d × 24h × buckets)
    and Python's datetime.weekday/.hour are timezone-aware.
    """
    project = await _resolve_project(session, slug)
    scan_config = (
        await session.execute(
            select(ScanConfig).where(
                ScanConfig.id == scan_config_id,
                ScanConfig.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if scan_config is None:
        raise HTTPException(404, "Scan config not found")

    # ``scope_ref`` arrives as free text (the route types it ``FreeTextFilter``,
    # which only strips NUL bytes). For these two scopes ``_scope_metric_filters``
    # feeds it straight to ``uuid.UUID()``, and an unparseable one raised a bare
    # ValueError that the catch-all handler turned into a 500 — where the sibling
    # insight endpoints (breakdown-timeline, distribution drifts) answer 422.
    if scope_type in (SCOPE_EVENT, SCOPE_EVENT_TYPE):
        _parse_scope_uuid(scope_ref, label="scope_ref")

    metric_rows = await _get_metric_rows(
        session,
        scope=scope_type,
        scan_config_id=scan_config_id,
        scope_ref=scope_ref,
        time_from=time_from,
        time_to=time_to,
    )
    anomalies = await _get_anomaly_rows(
        session,
        scan_config_id=scan_config_id,
        scope=scope_type,
        scope_ref=scope_ref,
        time_from=time_from,
        time_to=time_to,
    )

    counts: dict[tuple[int, int], int] = {}
    anomaly_counts: dict[tuple[int, int], int] = {}
    total = 0
    for bucket, count in metric_rows:
        slot = (bucket.weekday(), bucket.hour)
        counts[slot] = counts.get(slot, 0) + int(count)
        total += int(count)
    for anomaly in anomalies:
        slot = (anomaly.bucket.weekday(), anomaly.bucket.hour)
        anomaly_counts[slot] = anomaly_counts.get(slot, 0) + 1

    cells: list[SeasonalityCell] = []
    max_count = 0
    for weekday in range(7):
        for hour in range(24):
            slot_key = (weekday, hour)
            count = counts.get(slot_key, 0)
            max_count = max(max_count, count)
            cells.append(
                SeasonalityCell(
                    weekday=weekday,
                    hour=hour,
                    count=count,
                    anomaly_count=anomaly_counts.get(slot_key, 0),
                )
            )

    # Say which interval produced these bins. Below an hour of resolution every
    # bucket floors into hour 0, so the 7x24 grid is 23/24 structurally empty and
    # a reader takes it for missing data rather than a coarser scan
    # (tripl-jfm3.128). A 6h scan is not hourly either: its buckets land on
    # 00/06/12/18, leaving 20 of 24 columns structurally empty, so only an
    # interval of one hour or finer resolves an hour (tripl-0zpq.199).
    interval_code = scan_config.interval or ""
    try:
        hourly_resolution = get_interval(interval_code).delta <= timedelta(hours=1)
    except ValueError:
        # Unset or unknown is a configuration problem, not a reason to fail a
        # read-only chart: assume the finer rendering and let the grid speak.
        hourly_resolution = True

    return SeasonalityHeatmapResponse(
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        cells=cells,
        max_count=max_count,
        total_count=total,
        interval=interval_code,
        hourly_resolution=hourly_resolution,
    )


async def get_breakdown_timeline(
    session: AsyncSession,
    slug: str,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ref: str,
    breakdown_column: str,
    breakdown_value: str,
    is_other: bool,
    time_from: datetime | None,
    time_to: datetime | None,
) -> BreakdownTimelineResponse:
    """Per-bucket counts for one breakdown_value over the requested window.

    Powers the top-mover drill-down: clicking a breakdown row in the panel
    opens its own timeline so users can see whether the move is a single
    bucket spike or a sustained shift.
    """
    project = await _resolve_project(session, slug)
    scan_config = (
        await session.execute(
            select(ScanConfig).where(
                ScanConfig.id == scan_config_id,
                ScanConfig.project_id == project.id,
            )
        )
    ).scalar_one_or_none()
    if scan_config is None:
        raise HTTPException(404, "Scan config not found")

    if scope_type == SCOPE_PROJECT_TOTAL:
        # Sum across event_types so the row matches the project-total chart.
        query = (
            select(EventMetricBreakdown.bucket, func.sum(EventMetricBreakdown.count))
            .where(
                EventMetricBreakdown.scan_config_id == scan_config_id,
                EventMetricBreakdown.event_id.is_(None),
                EventMetricBreakdown.event_type_id.is_not(None),
                EventMetricBreakdown.breakdown_column == breakdown_column,
                EventMetricBreakdown.breakdown_value == breakdown_value,
                EventMetricBreakdown.is_other.is_(is_other),
            )
            .group_by(EventMetricBreakdown.bucket)
            .order_by(EventMetricBreakdown.bucket)
        )
    elif scope_type == SCOPE_EVENT_TYPE:
        query = (
            select(EventMetricBreakdown.bucket, EventMetricBreakdown.count)
            .where(
                EventMetricBreakdown.scan_config_id == scan_config_id,
                EventMetricBreakdown.event_id.is_(None),
                EventMetricBreakdown.event_type_id
                == _parse_scope_uuid(scope_ref, label="scope_ref"),
                EventMetricBreakdown.breakdown_column == breakdown_column,
                EventMetricBreakdown.breakdown_value == breakdown_value,
                EventMetricBreakdown.is_other.is_(is_other),
            )
            .order_by(EventMetricBreakdown.bucket)
        )
    else:
        query = (
            select(EventMetricBreakdown.bucket, EventMetricBreakdown.count)
            .where(
                EventMetricBreakdown.scan_config_id == scan_config_id,
                EventMetricBreakdown.event_id == _parse_scope_uuid(scope_ref, label="scope_ref"),
                EventMetricBreakdown.breakdown_column == breakdown_column,
                EventMetricBreakdown.breakdown_value == breakdown_value,
                EventMetricBreakdown.is_other.is_(is_other),
            )
            .order_by(EventMetricBreakdown.bucket)
        )

    if time_from is not None:
        query = query.where(EventMetricBreakdown.bucket >= time_from)
    if time_to is not None:
        query = query.where(EventMetricBreakdown.bucket < time_to)

    rows = (await session.execute(query)).all()
    data = [BreakdownTimelinePoint(bucket=bucket, count=int(count)) for bucket, count in rows]

    return BreakdownTimelineResponse(
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        breakdown_column=breakdown_column,
        breakdown_value=breakdown_value,
        is_other=is_other,
        interval=scan_config.interval,
        data=data,
    )


# The sparkline window around a signal's flagged bucket (MO-19): the run-up,
# the flagged bucket, and a few after it to show whether the move held.
SIGNAL_SERIES_BUCKETS_BEFORE = 19
SIGNAL_SERIES_BUCKETS_AFTER = 4

_SignalSeriesGroupKey = tuple[uuid.UUID, str]


def _signal_series_bounds(
    *,
    interval: str,
    recent_window: timedelta | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """``[oldest, newest]`` flagged bucket a sparkline is drawn for on this grid.

    ``bucket`` comes from the request body, and a group's query spans the union
    of its windows — so without a bound, two scopes flagged in 1970 and 2100
    would read a scan's whole ``event_metrics`` history in one request. An open
    signal's bucket lies within the freshness horizon of its scan's newest
    bucket, which itself lies within that horizon of now while the scan is
    alive; twice the horizon is the oldest bucket an open signal can carry, with
    room for a collector that is running late. Nothing open is flagged beyond
    the bucket now falls in.
    """
    delta = get_interval(interval).delta
    liveness_cutoff = scan_liveness_cutoff(interval=interval, recent_window=recent_window, now=now)
    oldest = liveness_cutoff - (now - liveness_cutoff)
    return oldest, now + delta


def _signal_series_window(bucket: datetime, delta: timedelta) -> tuple[datetime, datetime]:
    """``[start, end)`` of one signal's sparkline, on the scan's own grid."""
    anchor = _as_utc(bucket)
    return (
        anchor - SIGNAL_SERIES_BUCKETS_BEFORE * delta,
        anchor + (SIGNAL_SERIES_BUCKETS_AFTER + 1) * delta,
    )


def _signal_series_points(
    counts: dict[datetime, int],
    *,
    start: datetime,
    end: datetime,
    delta: timedelta,
) -> list[BreakdownTimelinePoint]:
    """The stored buckets inside ``[start, end)``, interior gaps zero-filled.

    Filled only between the first and last stored bucket: before the first the
    scope may simply not have been collected yet, and after the last the bucket
    may not have closed, so a zero there would be invented rather than observed.
    """
    inside = sorted(bucket for bucket in counts if start <= bucket < end)
    if not inside:
        return []
    points: list[BreakdownTimelinePoint] = []
    cursor = inside[0]
    while cursor <= inside[-1]:
        points.append(BreakdownTimelinePoint(bucket=cursor, count=counts.get(cursor, 0)))
        cursor += delta
    return points


async def _signal_series_counts(
    session: AsyncSession,
    *,
    scan_config_id: uuid.UUID,
    scope_type: str,
    scope_ids: set[uuid.UUID],
    time_from: datetime,
    time_to: datetime,
) -> dict[str, dict[datetime, int]]:
    """``scope_ref -> {bucket: count}`` for one scan and scope type, one query.

    Bounded by the union of the group's windows, and ``get_signal_series`` only
    admits buckets inside ``_signal_series_bounds`` — so the span is at most
    twice the freshness horizon plus one sparkline, never the history.
    """
    in_window = (EventMetric.bucket >= time_from, EventMetric.bucket < time_to)
    counts: dict[str, dict[datetime, int]] = defaultdict(dict)
    if scope_type == SCOPE_PROJECT_TOTAL:
        rows = await session.execute(
            select(EventMetric.bucket, func.sum(EventMetric.count))
            .where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id.is_not(None),
                *in_window,
            )
            .group_by(EventMetric.bucket)
        )
        for bucket, count in rows.tuples().all():
            counts[str(scan_config_id)][_as_utc(bucket)] = int(count or 0)
        return counts
    if scope_type == SCOPE_EVENT_TYPE:
        scope_rows = await session.execute(
            select(EventMetric.event_type_id, EventMetric.bucket, EventMetric.count).where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_id.is_(None),
                EventMetric.event_type_id.in_(scope_ids),
                *in_window,
            )
        )
    else:
        scope_rows = await session.execute(
            select(EventMetric.event_id, EventMetric.bucket, EventMetric.count).where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_id.in_(scope_ids),
                *in_window,
            )
        )
    for scope_id, bucket, count in scope_rows.tuples().all():
        counts[str(scope_id)][_as_utc(bucket)] = int(count)
    return counts


async def get_signal_series(
    session: AsyncSession,
    slug: str,
    scopes: list[SignalSeriesScope],
) -> list[SignalSeriesResponse]:
    """Recent series for many open signals at once — the Anomalies row sparklines.

    Kept OUT of ``get_active_signals`` and its 30 s cache on purpose: that list
    is shared by the bell, Overview and the Events page, none of which draws a
    sparkline, and 24 points per signal would multiply every one of their
    payloads. One query per (scan, scope type) instead of one per signal.

    Scopes this cannot draw are omitted rather than failed: catalog ``metric``
    signals (no scan; their series live in ``metric_values``), a scan outside
    the project or without an interval, a malformed ``scope_ref``, and a
    ``bucket`` no open signal can carry (``_signal_series_bounds``).
    """
    if not scopes:
        return []
    project = await _resolve_project(session, slug)
    config_ids = {scope.scan_config_id for scope in scopes}
    intervals: dict[uuid.UUID, str] = {
        config_id: str(interval)
        for config_id, interval in (
            await session.execute(
                select(ScanConfig.id, ScanConfig.interval).where(
                    ScanConfig.id.in_(config_ids),
                    ScanConfig.project_id == project.id,
                    ScanConfig.interval.is_not(None),
                )
            )
        )
        .tuples()
        .all()
    }

    recent_window = await _get_project_recent_signal_window(session, project.id)
    now = datetime.now(UTC)
    bounds = {
        config_id: _signal_series_bounds(interval=interval, recent_window=recent_window, now=now)
        for config_id, interval in intervals.items()
    }

    drawable: list[tuple[SignalSeriesScope, timedelta, datetime, datetime]] = []
    group_ids: dict[_SignalSeriesGroupKey, set[uuid.UUID]] = defaultdict(set)
    group_span: dict[_SignalSeriesGroupKey, tuple[datetime, datetime]] = {}
    for scope in scopes:
        interval = intervals.get(scope.scan_config_id)
        scope_type = str(scope.scope_type)
        if interval is None or scope_type not in (
            SCOPE_PROJECT_TOTAL,
            SCOPE_EVENT_TYPE,
            SCOPE_EVENT,
        ):
            continue
        try:
            scope_id = uuid.UUID(scope.scope_ref)
        except ValueError:
            continue
        if scope_type == SCOPE_PROJECT_TOTAL and scope_id != scope.scan_config_id:
            continue
        oldest, newest = bounds[scope.scan_config_id]
        if not oldest <= _as_utc(scope.bucket) <= newest:
            continue
        delta = get_interval(interval).delta
        start, end = _signal_series_window(scope.bucket, delta)
        key = (scope.scan_config_id, scope_type)
        group_ids[key].add(scope_id)
        lo, hi = group_span.get(key, (start, end))
        group_span[key] = (min(lo, start), max(hi, end))
        drawable.append((scope, delta, start, end))

    counts_by_group: dict[_SignalSeriesGroupKey, dict[str, dict[datetime, int]]] = {}
    for key, (time_from, time_to) in group_span.items():
        counts_by_group[key] = await _signal_series_counts(
            session,
            scan_config_id=key[0],
            scope_type=key[1],
            scope_ids=group_ids[key],
            time_from=time_from,
            time_to=time_to,
        )

    return [
        SignalSeriesResponse(
            scan_config_id=scope.scan_config_id,
            scope_type=scope.scope_type,
            scope_ref=scope.scope_ref,
            bucket=scope.bucket,
            interval=intervals[scope.scan_config_id],
            data=_signal_series_points(
                counts_by_group[(scope.scan_config_id, str(scope.scope_type))].get(
                    str(uuid.UUID(scope.scope_ref)), {}
                ),
                start=start,
                end=end,
                delta=delta,
            ),
        )
        for scope, delta, start, end in drawable
    ]


def _parse_scope_uuid(scope_ref: str, *, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(scope_ref)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{label} must be a UUID") from exc


def _mover_float(value: object) -> float:
    if isinstance(value, (int, float, str)):
        return float(value)
    return 0.0


def _distribution_top_movers_from_row(row: DistributionDrift) -> list[DistributionDriftTopMover]:
    top_movers: list[DistributionDriftTopMover] = []
    for mover in row.top_movers or []:
        top_movers.append(
            DistributionDriftTopMover(
                value=str(mover.get("value", "")),
                baseline_share=_mover_float(mover.get("baseline_share")),
                current_share=_mover_float(mover.get("current_share")),
                contribution=_mover_float(mover.get("contribution")),
            )
        )
    return top_movers


async def get_distribution_drifts(
    session: AsyncSession,
    slug: str,
    *,
    scope_type: str,
    scope_ref: str,
    scan_config_id: uuid.UUID | None,
    time_from: datetime | None,
    time_to: datetime | None,
) -> DistributionDriftsResponse:
    project = await _resolve_project(session, slug)
    query = (
        select(DistributionDrift)
        .join(ScanConfig, ScanConfig.id == DistributionDrift.scan_config_id)
        .where(ScanConfig.project_id == project.id)
    )

    resolved_scan_config_id = scan_config_id
    event_type_id: uuid.UUID | None = None
    if scope_type == SCOPE_PROJECT_TOTAL:
        if resolved_scan_config_id is None:
            resolved_scan_config_id = _parse_scope_uuid(scope_ref, label="scope_ref")
        query = query.where(
            DistributionDrift.scan_config_id == resolved_scan_config_id,
            DistributionDrift.event_type_id.is_(None),
        )
    elif scope_type == SCOPE_EVENT_TYPE:
        event_type_id = _parse_scope_uuid(scope_ref, label="scope_ref")
        await _resolve_event_type(session, project.id, event_type_id)
        resolved_scan_config_id = resolved_scan_config_id or await _resolve_scope_scan_config_id(
            session,
            project.id,
            event_type_id=event_type_id,
        )
        query = query.where(DistributionDrift.event_type_id == event_type_id)
        if resolved_scan_config_id is not None:
            query = query.where(DistributionDrift.scan_config_id == resolved_scan_config_id)
    elif scope_type == SCOPE_EVENT:
        event_id = _parse_scope_uuid(scope_ref, label="scope_ref")
        event = await _resolve_event(session, project.id, event_id)
        event_type_id = event.event_type_id
        resolved_scan_config_id = resolved_scan_config_id or await _resolve_scope_scan_config_id(
            session,
            project.id,
            event_id=event.id,
        )
        query = query.where(DistributionDrift.event_type_id == event_type_id)
        if resolved_scan_config_id is not None:
            query = query.where(DistributionDrift.scan_config_id == resolved_scan_config_id)
    else:
        raise HTTPException(status_code=422, detail="Unsupported distribution drift scope_type")

    if resolved_scan_config_id is not None:
        scan_config = (
            await session.execute(
                select(ScanConfig.id).where(
                    ScanConfig.id == resolved_scan_config_id,
                    ScanConfig.project_id == project.id,
                )
            )
        ).scalar_one_or_none()
        if scan_config is None:
            raise HTTPException(404, "Scan config not found")
    if time_from is not None:
        query = query.where(DistributionDrift.bucket >= time_from)
    if time_to is not None:
        query = query.where(DistributionDrift.bucket < time_to)

    rows = (
        (
            await session.execute(
                query.order_by(DistributionDrift.bucket, DistributionDrift.field_name)
            )
        )
        .scalars()
        .all()
    )
    points = [
        DistributionDriftPoint(
            id=row.id,
            scan_config_id=row.scan_config_id,
            event_type_id=row.event_type_id,
            field_name=row.field_name,
            bucket=row.bucket,
            psi=row.psi,
            band=row.band,
            baseline_total=row.baseline_total,
            current_total=row.current_total,
            top_movers=_distribution_top_movers_from_row(row),
        )
        for row in rows
    ]
    fields = sorted({point.field_name for point in points})
    return DistributionDriftsResponse(
        scope=scope_type,
        scan_config_id=resolved_scan_config_id,
        event_type_id=event_type_id,
        fields=fields,
        data=points,
    )
