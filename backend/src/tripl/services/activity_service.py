from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import ColumnElement, Select, and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_METRIC,
    SCOPE_PROJECT_TOTAL,
)
from tripl.metric_grid import metric_grid_stmt, metric_grids
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.domain_enums import ScanInterval
from tripl.models.event import Event
from tripl.models.event_change import EVENT_CHANGE_SOURCE_SCAN, SCAN_AUTHOR_LABEL, EventChange
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob
from tripl.schemas.activity import ActivityItemResponse
from tripl.services.monitoring_utils import (
    LATEST_SCAN_STALE_INTERVALS,
    scan_interval_to_timedelta,
)
from tripl.services.project_links import project_org_slugs, qualify_project_path
from tripl.services.project_lookup import project_slug_clause, resolve_project_id

# The activity rail surfaces "recent" signals, not the full anomaly history.
# Without a window, weeks-old high-z anomalies stay ordered at the top of the
# feed on every page and read as live/streaming events. Bound the query to a
# recent window measured against wall-clock now so only fresh anomalies show.
#
# Measured on ``MetricAnomaly.bucket``, never ``created_at``:
# the detector deletes and re-inserts its trailing re-evaluation window every
# tick, so ``created_at`` is "last re-scored", not "happened". Keyed on it, a
# 26-day-old daily anomaly read "just now" on every collection and a replay
# re-dated three months of anomalies into the rail.
#
# The window is floored at ``LATEST_SCAN_STALE_INTERVALS`` buckets of the
# series' own grid, the same floor the freshness horizon uses. Ingestion
# settling withholds at least one trailing bucket from emission, so the newest
# anomaly a weekly series can produce STARTS more than 7 days ago: a bare
# ``bucket >= now - 7d`` would hide every weekly anomaly from the rail for good.
ANOMALY_RECENCY_WINDOW = timedelta(days=7)


def _recency_window(interval: str | None) -> timedelta:
    """How far back an anomaly bucket on ``interval``'s grid still counts as recent."""
    delta = scan_interval_to_timedelta(interval)
    if delta is None:
        return ANOMALY_RECENCY_WINDOW
    return max(ANOMALY_RECENCY_WINDOW, LATEST_SCAN_STALE_INTERVALS * delta)


def _scan_recency_clause(now: datetime) -> ColumnElement[bool]:
    """``bucket`` inside the recency window of its scan config's own grid.

    Expects ``ScanConfig`` joined. Grids whose floor never binds share the plain
    7-day cutoff; each coarser grid gets its own wider one.
    """
    wider: dict[timedelta, list[str]] = {}
    for interval in ScanInterval:
        window = _recency_window(interval.value)
        if window > ANOMALY_RECENCY_WINDOW:
            wider.setdefault(window, []).append(interval.value)
    return or_(
        MetricAnomaly.bucket >= now - ANOMALY_RECENCY_WINDOW,
        *(
            and_(ScanConfig.interval.in_(intervals), MetricAnomaly.bucket >= now - window)
            for window, intervals in wider.items()
        ),
    )


@dataclass(frozen=True)
class _ProjectScope:
    """Which projects one feed read covers: one slug, a member set, or both.

    ``project_ids`` of ``None`` is every project; the ``slug`` narrows further.
    """

    slug: str | None
    project_ids: set[uuid.UUID] | None

    def apply[S: Select[Any]](self, stmt: S) -> S:
        """Narrow a statement that already joins ``Project``."""
        if self.slug is not None:
            stmt = stmt.where(project_slug_clause(self.slug))
        if self.project_ids is not None:
            stmt = stmt.where(Project.id.in_(self.project_ids))
        return stmt


def _utc_sort_key(value: datetime) -> datetime:
    """SQLite hands naive timestamps back; compare every item as a UTC instant."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def list_activity(
    session: AsyncSession,
    *,
    visible_project_ids: set[uuid.UUID] | None,
    slug: str | None = None,
    limit: int = 20,
) -> list[ActivityItemResponse]:
    """The activity rail, limited to the projects the caller may see.

    ``visible_project_ids`` is ``project_access.member_project_ids`` for the
    caller: a set limits every source query to those projects (``None``, every
    project on the instance, is kept for scripts; no request passes it). It is
    required, with no default, so a new caller cannot silently fall back to the
    whole instance. A non-member never
    sees a project exist, so a slug outside the set is "Project not found",
    exactly as for a slug that does not exist.
    """
    if slug is not None:
        project_id = await resolve_project_id(session, slug)
        if visible_project_ids is not None and project_id not in visible_project_ids:
            raise HTTPException(status_code=404, detail="Project not found")
    if visible_project_ids is not None and not visible_project_ids:
        return []

    scope = _ProjectScope(slug=slug, project_ids=visible_project_ids)
    items: list[ActivityItemResponse] = []
    items.extend(await _anomaly_items(session, scope=scope, limit=limit))
    items.extend(await _scan_job_items(session, scope=scope, limit=limit))
    items.extend(await _alert_delivery_items(session, scope=scope, limit=limit))
    items.extend(await _event_items(session, scope=scope, limit=limit))
    items.extend(await _auto_transition_items(session, scope=scope, limit=limit))

    page = sorted(items, key=lambda item: _utc_sort_key(item.occurred_at), reverse=True)[:limit]
    return await _org_qualified(session, page)


async def _org_qualified(
    session: AsyncSession, items: list[ActivityItemResponse]
) -> list[ActivityItemResponse]:
    """The items with ``target_path`` naming each project's organization (F20 PR8).

    The source queries build ``/p/{slug}/...``; a project slug is unique only
    inside its organization, so the path is completed here, per item, from the
    project's own organization — one query for the page rather than a join in
    each of the six sources.
    """
    org_slugs = await project_org_slugs(session, {item.project_id for item in items})
    return [
        item.model_copy(
            update={
                "target_path": qualify_project_path(org_slugs[item.project_id], item.target_path)
            }
        )
        if item.target_path is not None and item.project_id in org_slugs
        else item
        for item in items
    ]


async def _anomaly_items(
    session: AsyncSession,
    *,
    scope: _ProjectScope,
    limit: int,
) -> list[ActivityItemResponse]:
    now = datetime.now(UTC)
    stmt = (
        select(
            MetricAnomaly.id,
            MetricAnomaly.scan_config_id,
            MetricAnomaly.scope_type,
            MetricAnomaly.event_id,
            MetricAnomaly.event_type_id,
            MetricAnomaly.bucket,
            MetricAnomaly.actual_count,
            MetricAnomaly.expected_count,
            MetricAnomaly.z_score,
            MetricAnomaly.direction,
            Project.id.label("project_id"),
            Project.slug,
            Project.name.label("project_name"),
            ScanConfig.name.label("scan_name"),
            Event.name.label("event_name"),
            EventType.display_name.label("event_type_name"),
        )
        .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .join(Project, Project.id == ScanConfig.project_id)
        .outerjoin(Event, Event.id == MetricAnomaly.event_id)
        .outerjoin(EventType, EventType.id == MetricAnomaly.event_type_id)
        .where(_scan_recency_clause(now))
        .order_by(desc(MetricAnomaly.bucket), desc(MetricAnomaly.id))
        .limit(limit)
    )
    stmt = scope.apply(stmt)

    rows = (await session.execute(stmt)).all()

    # One incident is one rail item: a project-total spike/drop trips its child
    # event_type/event scopes on the same scan, bucket and direction. Surface
    # only the parent project_total row and suppress the co-firing children so a
    # single incident does not stack into many rail entries. This is layered on
    # top of Wave 1's wall-clock recency filter (kept above), not a replacement.
    parent_incidents = {
        (row.scan_config_id, row.bucket, str(row.direction))
        for row in rows
        if row.scope_type == SCOPE_PROJECT_TOTAL
    }

    items: list[ActivityItemResponse] = []
    for row in rows:
        if (
            row.scope_type in (SCOPE_EVENT_TYPE, SCOPE_EVENT)
            and (row.scan_config_id, row.bucket, str(row.direction)) in parent_incidents
        ):
            continue
        scope_name = _scope_name(
            row.scope_type,
            event_name=row.event_name,
            event_type_name=row.event_type_name,
            project_name=row.project_name,
        )
        expected = round(float(row.expected_count))
        z_score = float(row.z_score)
        direction = str(row.direction)
        items.append(
            ActivityItemResponse(
                id=f"anomaly:{row.id}",
                project_id=row.project_id,
                project_slug=row.slug,
                project_name=row.project_name,
                type="anomaly",
                severity="high" if abs(z_score) >= 4 else "medium",
                title=f"{direction.capitalize()} on {scope_name}",
                detail=(
                    f"{int(row.actual_count):,} actual vs {expected:,} expected · z={z_score:.1f}"
                ),
                occurred_at=row.bucket,
                target_path=_monitoring_path(
                    row.slug,
                    row.scope_type,
                    scan_config_id=row.scan_config_id,
                    event_id=row.event_id,
                    event_type_id=row.event_type_id,
                ),
            )
        )
    items.extend(await _metric_anomaly_items(session, scope=scope, limit=limit, now=now))
    return items


async def _metric_anomaly_items(
    session: AsyncSession,
    *,
    scope: _ProjectScope,
    limit: int,
    now: datetime,
) -> list[ActivityItemResponse]:
    """Catalog-metric anomalies, which the scan-config query above cannot reach.

    A ``metric``-scope row carries a NULL ``scan_config_id`` and is keyed by
    ``scope_ref = str(metric_definition_id)`` (``models.metric_anomaly``), so
    the inner join through ScanConfig dropped every one of them and a spiking
    catalog metric never reached the rail.
    Project is reached through ``MetricDefinition.project_id`` instead. The
    metric ids are resolved first and matched on ``scope_ref`` in Python-side
    string form, the way ``metrics_insights_service`` loads metric scopes,
    because ``scope_ref`` is a string and a SQL cast of a UUID column does not
    render the same text on SQLite and PostgreSQL.

    Each metric's recency window is floored on its own resolved grid
    (``metric_grid``), so a weekly metric's newest emittable anomaly, which
    starts more than 7 days back, still reaches the rail.
    """
    metric_stmt = select(
        MetricDefinition.id,
        MetricDefinition.display_name,
        Project.id.label("project_id"),
        Project.slug,
        Project.name.label("project_name"),
    ).join(Project, Project.id == MetricDefinition.project_id)
    metric_stmt = scope.apply(metric_stmt)
    metrics = {str(row.id): row for row in (await session.execute(metric_stmt)).all()}
    if not metrics:
        return []
    grids = metric_grids(
        (
            await session.execute(
                metric_grid_stmt(MetricDefinition.id.in_([row.id for row in metrics.values()]))
            )
        ).all()
    )
    refs_by_window: dict[timedelta, list[str]] = {}
    for scope_ref, metric in metrics.items():
        grid = grids.get(metric.id)
        window = _recency_window(grid.interval if grid is not None else None)
        refs_by_window.setdefault(window, []).append(scope_ref)

    rows = (
        await session.execute(
            select(
                MetricAnomaly.id,
                MetricAnomaly.scope_ref,
                MetricAnomaly.bucket,
                MetricAnomaly.actual_count,
                MetricAnomaly.expected_count,
                MetricAnomaly.z_score,
                MetricAnomaly.direction,
            )
            .where(
                MetricAnomaly.scope_type == SCOPE_METRIC,
                or_(
                    *(
                        and_(
                            MetricAnomaly.scope_ref.in_(scope_refs),
                            MetricAnomaly.bucket >= now - window,
                        )
                        for window, scope_refs in refs_by_window.items()
                    )
                ),
            )
            .order_by(desc(MetricAnomaly.bucket), desc(MetricAnomaly.id))
            .limit(limit)
        )
    ).all()

    items: list[ActivityItemResponse] = []
    for row in rows:
        metric = metrics[row.scope_ref]
        z_score = float(row.z_score)
        direction = str(row.direction)
        items.append(
            ActivityItemResponse(
                id=f"anomaly:{row.id}",
                project_id=metric.project_id,
                project_slug=metric.slug,
                project_name=metric.project_name,
                type="anomaly",
                severity="high" if abs(z_score) >= 4 else "medium",
                title=f"{direction.capitalize()} on {metric.display_name}",
                # Catalog metrics carry fractional values (ratios, averages), so
                # int() would print a collapsed 0.04 ratio as "0 actual vs 0
                # expected".
                detail=(
                    f"{_format_metric_value(row.actual_count)} actual vs "
                    f"{_format_metric_value(row.expected_count)} expected · z={z_score:.1f}"
                ),
                occurred_at=row.bucket,
                target_path=f"/p/{metric.slug}/monitoring/metric/{metric.id}",
            )
        )
    return items


def _format_metric_value(value: float) -> str:
    """Whole numbers with thousands separators; small fractions to 3 significant digits."""
    number = float(value)
    if number.is_integer() or abs(number) >= 100:
        return f"{round(number):,}"
    return f"{number:.3g}"


async def _scan_job_items(
    session: AsyncSession,
    *,
    scope: _ProjectScope,
    limit: int,
) -> list[ActivityItemResponse]:
    occurred_at = func.coalesce(ScanJob.completed_at, ScanJob.started_at, ScanJob.updated_at)
    stmt = (
        select(
            ScanJob.id,
            ScanJob.status,
            ScanJob.result_summary,
            ScanJob.error_message,
            occurred_at.label("occurred_at"),
            Project.id.label("project_id"),
            Project.slug,
            Project.name.label("project_name"),
            ScanConfig.name.label("scan_name"),
        )
        .join(ScanConfig, ScanConfig.id == ScanJob.scan_config_id)
        .join(Project, Project.id == ScanConfig.project_id)
        .order_by(desc(occurred_at), desc(ScanJob.id))
        .limit(limit)
    )
    stmt = scope.apply(stmt)

    rows = (await session.execute(stmt)).all()
    items: list[ActivityItemResponse] = []
    for row in rows:
        status = str(row.status)
        items.append(
            ActivityItemResponse(
                id=f"scan-job:{row.id}",
                project_id=row.project_id,
                project_slug=row.slug,
                project_name=row.project_name,
                type="scan",
                severity=_scan_job_severity(status),
                title=f"Scan {status}: {row.scan_name}",
                detail=_scan_job_detail(status, row.result_summary, row.error_message),
                occurred_at=row.occurred_at,
                target_path=f"/p/{row.slug}/scans",
            )
        )
    return items


async def _alert_delivery_items(
    session: AsyncSession,
    *,
    scope: _ProjectScope,
    limit: int,
) -> list[ActivityItemResponse]:
    occurred_at = func.coalesce(AlertDelivery.sent_at, AlertDelivery.updated_at)
    stmt = (
        select(
            AlertDelivery.id,
            AlertDelivery.status,
            AlertDelivery.channel,
            AlertDelivery.matched_count,
            AlertDelivery.error_message,
            occurred_at.label("occurred_at"),
            Project.id.label("project_id"),
            Project.slug,
            Project.name.label("project_name"),
            AlertDestination.name.label("destination_name"),
            AlertRule.name.label("rule_name"),
        )
        .join(Project, Project.id == AlertDelivery.project_id)
        .join(AlertDestination, AlertDestination.id == AlertDelivery.destination_id)
        .join(AlertRule, AlertRule.id == AlertDelivery.rule_id)
        .order_by(desc(occurred_at), desc(AlertDelivery.id))
        .limit(limit)
    )
    stmt = scope.apply(stmt)

    rows = (await session.execute(stmt)).all()
    items: list[ActivityItemResponse] = []
    for row in rows:
        status = str(row.status)
        items.append(
            ActivityItemResponse(
                id=f"alert-delivery:{row.id}",
                project_id=row.project_id,
                project_slug=row.slug,
                project_name=row.project_name,
                type="alert",
                severity=_alert_delivery_severity(status),
                title=f"Alert {status}: {row.rule_name}",
                detail=_alert_delivery_detail(
                    status=status,
                    channel=row.channel,
                    matched_count=row.matched_count,
                    destination_name=row.destination_name,
                    error_message=row.error_message,
                ),
                occurred_at=row.occurred_at,
                target_path=f"/p/{row.slug}/alerting",
            )
        )
    return items


async def _event_items(
    session: AsyncSession,
    *,
    scope: _ProjectScope,
    limit: int,
) -> list[ActivityItemResponse]:
    """Recent catalog events, restricted to each project's MAIN branch.

    Event is branch-scoped and ``deep_copy_plan_to_branch`` clones every plan
    entity with fresh ids, so joining on ``project_id`` alone put one copy of
    each event into the feed per open working branch. Those rows
    were emitted as ``/p/<slug>/monitoring/event/<id>``, which the detail page
    resolves against main — so a branch-local id produced a hard 404 rather than
    a wrong-but-working link, and which copy took the slot was a coin flip: rows
    seeded in one transaction share an identical ``server_default=now()``
    ``updated_at``, leaving the ``desc(updated_at), desc(id)`` tiebreak nothing
    to order by.

    Same defect class as ``metrics_service.get_overview_kpi_series``,
    scoped the same way. The predicate is a join on
    ``branch_id`` rather than a per-project subquery because this feed also runs
    unscoped (no ``slug``) across every visible project at once.

    ``PlanBranch.project_id == Event.project_id`` is part of the join, not
    decoration: ``events.branch_id`` is only an FK to ``plan_branches.id``, with
    nothing at the schema level tying the two to the same project, so
    ``kind == main`` alone would also accept ANOTHER project's main branch. No
    write path can produce that today — the API validates the override in
    ``resolve_branch_id`` and the column default derives the branch from the
    row's own ``project_id`` — but the pair is what "this project's main branch"
    actually means, and it is free here since Project is already joined.
    """
    stmt = (
        select(
            Event.id,
            Event.name,
            Event.status,
            Event.created_at,
            Event.updated_at,
            Project.id.label("project_id"),
            Project.slug,
            Project.name.label("project_name"),
            EventType.display_name.label("event_type_name"),
        )
        .join(Project, Project.id == Event.project_id)
        .join(EventType, EventType.id == Event.event_type_id)
        .join(
            PlanBranch,
            (PlanBranch.id == Event.branch_id) & (PlanBranch.project_id == Event.project_id),
        )
        .where(PlanBranch.kind == BranchKind.main.value)
        .order_by(desc(Event.updated_at), desc(Event.id))
        .limit(limit)
    )
    stmt = scope.apply(stmt)

    rows = (await session.execute(stmt)).all()
    items: list[ActivityItemResponse] = []
    for row in rows:
        title, detail, severity = _event_copy(
            name=row.name,
            event_type_name=row.event_type_name,
            status=str(row.status),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        items.append(
            ActivityItemResponse(
                id=f"event:{row.id}",
                project_id=row.project_id,
                project_slug=row.slug,
                project_name=row.project_name,
                type="event",
                severity=severity,
                title=title,
                detail=detail,
                occurred_at=row.updated_at,
                target_path=f"/p/{row.slug}/monitoring/event/{row.id}",
            )
        )
    return items


async def _auto_transition_items(
    session: AsyncSession,
    *,
    scope: _ProjectScope,
    limit: int,
) -> list[ActivityItemResponse]:
    """Status transitions the metrics worker made from data (#258), attributed to the scan.

    Auto-live writes an ``EventChange`` with ``source='scan'`` and leaves the event's
    ``updated_at`` alone, so the transition reaches the rail through this row
    — "Seen in data: <event> is live", by ``tripl (scan)`` — instead of an
    anonymous "Event implemented" re-announcement from ``_event_items``. Main
    branch only, joined the way ``_event_items`` joins it.

    Selected on ``source``, never on ``user_id IS NULL``: ``user_id`` is
    ``ON DELETE SET NULL``, so a person's status edit reads NULL there once the
    person is deleted, and would otherwise surface as "Seen in data".
    """
    stmt = (
        select(
            EventChange.id.label("change_id"),
            EventChange.new_value,
            EventChange.created_at.label("occurred_at"),
            Event.id.label("event_id"),
            Event.name,
            Project.id.label("project_id"),
            Project.slug,
            Project.name.label("project_name"),
            EventType.display_name.label("event_type_name"),
        )
        .join(Event, Event.id == EventChange.event_id)
        .join(Project, Project.id == Event.project_id)
        .join(EventType, EventType.id == Event.event_type_id)
        .join(
            PlanBranch,
            (PlanBranch.id == Event.branch_id) & (PlanBranch.project_id == Event.project_id),
        )
        .where(
            PlanBranch.kind == BranchKind.main.value,
            EventChange.source == EVENT_CHANGE_SOURCE_SCAN,
            EventChange.field == "status",
            EventChange.new_value.in_(("live", "implemented")),
        )
        .order_by(desc(EventChange.created_at), desc(EventChange.id))
        .limit(limit)
    )
    stmt = scope.apply(stmt)
    rows = (await session.execute(stmt)).all()
    return [
        ActivityItemResponse(
            id=f"event-status:{row.change_id}",
            project_id=row.project_id,
            project_slug=row.slug,
            project_name=row.project_name,
            type="event",
            severity="low",
            title=f"Seen in data: {row.name} is {row.new_value}",
            detail=f"{SCAN_AUTHOR_LABEL} · {row.event_type_name}",
            occurred_at=row.occurred_at,
            target_path=f"/p/{row.slug}/monitoring/event/{row.event_id}",
        )
        for row in rows
    ]


def _scope_name(
    scope_type: str,
    *,
    event_name: str | None,
    event_type_name: str | None,
    project_name: str,
) -> str:
    if scope_type == SCOPE_EVENT:
        return event_name or "event"
    if scope_type == SCOPE_EVENT_TYPE:
        return event_type_name or "event type"
    if scope_type == SCOPE_PROJECT_TOTAL:
        return f"{project_name} total"
    return "metric"


def _monitoring_path(
    slug: str,
    scope_type: str,
    *,
    scan_config_id: uuid.UUID,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
) -> str | None:
    """Route an anomaly to its monitoring page, or nowhere.

    Built from the FK columns, never from ``scope_ref``.
    ``scope_ref`` is an unconstrained ``String(64)`` the detector writes for
    dedupe keying, whereas ``event_id`` / ``event_type_id`` are real foreign
    keys declared ``ondelete=SET NULL`` — so deleting an event nulls the FK but
    leaves its uuid sitting in ``scope_ref``, and routing off the latter emitted
    a link to a row that no longer exists. A NULL FK means the target is gone:
    return no path and let the rail render the row without a link.

    No branch predicate is needed here, unlike ``_event_items``. The detector
    only ever names rows the scan pipeline wrote, and those are on main by
    construction: the worker's INSERT omits ``branch_id``, so the column default
    ``plan_branch.default_branch_id`` resolves the project's main branch, and
    ``event_generator`` narrows its dedup lookup to that branch as well. A
    branch-local id therefore cannot reach an anomaly the way it reached the
    event feed.
    """
    if scope_type == SCOPE_PROJECT_TOTAL:
        return f"/p/{slug}/monitoring/project-total/{scan_config_id}"
    if scope_type == SCOPE_EVENT_TYPE:
        return f"/p/{slug}/monitoring/event-type/{event_type_id}" if event_type_id else None
    if scope_type == SCOPE_EVENT:
        return f"/p/{slug}/monitoring/event/{event_id}" if event_id else None
    return None


def _scan_job_severity(status: str) -> str:
    if status == "failed":
        return "high"
    if status in {"running", "pending"}:
        return "medium"
    return "low"


# Metric-point (time-series ROW) scopes a completed run writes. Summed rather
# than surfaced individually, and kept in sync with the frontend
# ``summarizeScanChanges`` (scanUtils.ts) so the activity rail and the Scans page
# agree on what a run produced.
_METRIC_POINT_KEYS = (
    "event_metrics",
    "type_metrics",
    "breakdown_event_metrics",
    "breakdown_type_metrics",
)
# What a run read, in preference order, with the noun each counter needs. A
# metrics collection reports warehouse rows (``query_rows_scanned``); the
# catalog analyzer reports the rows of its ``GROUP BY ALL`` breakdown
# (``scan_rows_processed``), which are distinct column combinations, not
# warehouse rows. Calling both "rows scanned" put one catalog run on screen as
# "153 combos" on its scan page and "153 rows scanned" here (#247).
_SCANNED_KEYS = (
    ("query_rows_scanned", "row scanned", "rows scanned"),
    ("scan_rows_processed", "column combination", "column combinations"),
)


def _as_positive_count(value: object) -> int:
    """Coerce a JSON result-summary value to a positive int, else 0.

    ``bool`` is rejected explicitly: it is an ``int`` subclass in Python, and a
    stray boolean flag must never be counted as a quantity.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    return 0


def _count_label(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _metric_points_written(summary: dict[str, object]) -> int:
    return sum(_as_positive_count(summary.get(key)) for key in _METRIC_POINT_KEYS)


def _scanned_label(summary: dict[str, object]) -> str | None:
    for key, singular, plural in _SCANNED_KEYS:
        count = _as_positive_count(summary.get(key))
        if count:
            return _count_label(count, singular, plural)
    return None


def _scan_job_detail(
    status: str,
    result_summary: dict[str, object] | None,
    error_message: str | None,
) -> str:
    if status == "failed" and error_message:
        return error_message

    summary = result_summary or {}
    # Lead with what the run actually produced. A completed scan on an
    # established catalog routinely discovers 0 new events yet still writes
    # metric points and scans rows; surfacing a bare "0 events created" made a
    # healthy run read as a no-op / failure. Only non-zero deltas
    # are shown, mirroring the frontend Scans-page summary.
    parts: list[str] = []
    events_created = _as_positive_count(summary.get("events_created"))
    if events_created:
        parts.append(_count_label(events_created, "new event", "new events"))
    metric_points = _metric_points_written(summary)
    if metric_points:
        parts.append(_count_label(metric_points, "metric point", "metric points"))
    signals_added = _as_positive_count(summary.get("signals_added"))
    if signals_added:
        # "new signal(s)", not "signal(s)": this is the run's signals_added
        # DELTA, and a bare "1 signal" read as the project's open-signal total —
        # irreconcilable with the Anomalies headline. Matches the
        # events_created branch above, which already qualifies its delta.
        parts.append(_count_label(signals_added, "new signal", "new signals"))
    alerts_queued = _as_positive_count(summary.get("alerts_queued"))
    if alerts_queued:
        parts.append(_count_label(alerts_queued, "alert queued", "alerts queued"))

    # A completed run that created nothing new is normal, not a failure: say so
    # explicitly rather than falling back to a vague status line.
    if not parts and status == "completed" and "events_created" in summary:
        parts.append("no new events discovered")

    scanned = _scanned_label(summary)
    if scanned:
        parts.append(scanned)

    if parts:
        return " · ".join(parts)
    # The activity rail is a web-UI surface, so it says *run*, never the API/CLI
    # spelling "job". Neither fallback may name a run TYPE it cannot
    # know either: a completed metrics collection always carries "events_created"
    # (collect_metrics writes it unconditionally), so the completed branch is
    # never a metrics collection — it is reached by an event-groups apply, or by
    # a run whose summary is missing. The old "Metrics collection job updated"
    # mislabelled both.
    if status == "completed":
        return "Run finished with no counts to report"
    return "Run status changed"


def _alert_delivery_severity(status: str) -> str:
    if status == "failed":
        return "high"
    if status == "pending":
        return "medium"
    return "low"


def _alert_delivery_detail(
    *,
    status: str,
    channel: str,
    matched_count: int,
    destination_name: str,
    error_message: str | None,
) -> str:
    if status == "failed" and error_message:
        return error_message
    matched = (
        f"{matched_count} matched signal"
        if matched_count == 1
        else f"{matched_count} matched signals"
    )
    return f"{matched} · {channel} · {destination_name}"


def _event_copy(
    *,
    name: str,
    event_type_name: str,
    status: str,
    created_at: datetime,
    updated_at: datetime,
) -> tuple[str, str, str]:
    if status == "archived":
        return f"Event archived: {name}", event_type_name, "low"
    if status == "in_review":
        return f"Event needs review: {name}", event_type_name, "medium"
    if status in ("implemented", "live"):
        return f"Event implemented: {name}", event_type_name, "low"
    if updated_at != created_at:
        return f"Event updated: {name}", event_type_name, "low"
    return f"Event added: {name}", event_type_name, "low"
