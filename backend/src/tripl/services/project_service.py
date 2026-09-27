import math
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import case, delete, func, literal, or_, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_METRIC,
    SCOPE_PROJECT_TOTAL,
)
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import MetricScopeType, ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.schemas.project import (
    ProjectCreate,
    ProjectCreateResponse,
    ProjectLatestScanJob,
    ProjectLatestSignal,
    ProjectResponse,
    ProjectSummary,
    ProjectUpdate,
)
from tripl.services import alerting_service, plan_branch_service, signal_triage_service
from tripl.services._monitor_state_intervals import load_monitor_state_intervals
from tripl.services.metrics_insights_service import (
    _active_metric_signals_by_project,
    is_significant_signal,
)
from tripl.services.metrics_service import _get_project_recent_signal_windows
from tripl.services.monitoring_utils import (
    classify_signal_state,
    latest_bucket_by_scan,
    scan_interval_to_timedelta,
    summarize_monitor_states,
)
from tripl.services.project_lookup import owning_org_id, resolve_project


async def _get_project_summaries(
    session: AsyncSession,
    project_ids: list[uuid.UUID],
    *,
    branch_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, ProjectSummary]:
    """Per-project counters for the sidebar badges and the Overview.

    ``branch_id`` scopes the PLAN counters (event types, events, variables) to
    one working branch instead of main, so a sidebar badge read while a branch
    is open agrees with the list on the page beside it (SH-11). It is only ever
    passed for a single project whose ownership the ``?branch=`` dependency
    already checked; every non-plan counter ignores it.
    """
    summaries = {project_id: ProjectSummary() for project_id in project_ids}
    if not project_ids:
        return summaries

    # Plan-entity counters (event types, events, variables) must describe the
    # LIVE plan only — the same rows the list endpoints return after
    # ``resolve_branch_id(None)`` resolves to the main branch. These models are
    # branch-scoped: working branches deep-copy every plan entity into the same
    # tables under a different ``branch_id``, so counting by ``project_id``
    # alone multiplies every counter by (1 + open branches). Non-plan models
    # (ScanConfig, ScanJob, AlertDestination, AlertRule, MetricAnomaly,
    # EventMetric) carry no ``branch_id`` and stay unfiltered. Batched: one
    # IN-subquery over all projects' main branches, no per-project fan-out.
    plan_branch_ids = (
        select(PlanBranch.id).where(
            PlanBranch.project_id.in_(project_ids),
            PlanBranch.kind == BranchKind.main.value,
        )
        if branch_id is None
        else select(PlanBranch.id).where(
            PlanBranch.project_id.in_(project_ids),
            PlanBranch.id == branch_id,
        )
    )

    event_type_rows = await session.execute(
        select(EventType.project_id, func.count(EventType.id))
        .where(
            EventType.project_id.in_(project_ids),
            EventType.branch_id.in_(plan_branch_ids),
        )
        .group_by(EventType.project_id)
    )
    for project_id, event_type_count in event_type_rows.all():
        summaries[project_id].event_type_count = int(event_type_count or 0)

    event_rows = await session.execute(
        select(
            Event.project_id,
            func.count(Event.id),
            func.sum(case((Event.status != "archived", 1), else_=0)),
            func.sum(
                case(
                    (Event.status.in_(["implemented", "live"]), 1),
                    else_=0,
                )
            ),
            func.sum(case((Event.status == "in_review", 1), else_=0)),
            func.sum(case((Event.status == "archived", 1), else_=0)),
        )
        .where(
            Event.project_id.in_(project_ids),
            Event.branch_id.in_(plan_branch_ids),
        )
        .group_by(Event.project_id)
    )
    for (
        project_id,
        event_count,
        active_event_count,
        implemented_event_count,
        review_pending_event_count,
        archived_event_count,
    ) in event_rows.all():
        summary = summaries[project_id]
        summary.event_count = int(event_count or 0)
        summary.active_event_count = int(active_event_count or 0)
        summary.implemented_event_count = int(implemented_event_count or 0)
        summary.review_pending_event_count = int(review_pending_event_count or 0)
        summary.archived_event_count = int(archived_event_count or 0)

    variable_rows = await session.execute(
        select(Variable.project_id, func.count(Variable.id))
        .where(
            Variable.project_id.in_(project_ids),
            Variable.branch_id.in_(plan_branch_ids),
        )
        .group_by(Variable.project_id)
    )
    for project_id, variable_count in variable_rows.all():
        summaries[project_id].variable_count = int(variable_count or 0)

    scan_rows = await session.execute(
        select(ScanConfig.project_id, func.count(ScanConfig.id))
        .where(ScanConfig.project_id.in_(project_ids))
        .group_by(ScanConfig.project_id)
    )
    for project_id, scan_count in scan_rows.all():
        summaries[project_id].scan_count = int(scan_count or 0)

    metric_rows = await session.execute(
        select(MetricDefinition.project_id, func.count(MetricDefinition.id))
        .where(MetricDefinition.project_id.in_(project_ids))
        .group_by(MetricDefinition.project_id)
    )
    for project_id, metric_count in metric_rows.all():
        summaries[project_id].metric_count = int(metric_count or 0)

    # Destinations AND their enabled rules in one pass. A destination with no
    # enabled rule routes nothing, so callers that ask "is alerting wired up?"
    # (the onboarding checklist) need both numbers — tripl-jfm3.81. The LEFT
    # JOIN keeps rule-less destinations in the destination count, and
    # COUNT(DISTINCT ...) stops the join fan-out inflating it; the CASE yields
    # NULL for disabled rows, which COUNT skips.
    alert_rows = await session.execute(
        select(
            AlertDestination.project_id,
            func.count(func.distinct(AlertDestination.id)),
            func.count(func.distinct(case((AlertRule.enabled.is_(True), AlertRule.id)))),
        )
        .select_from(AlertDestination)
        .outerjoin(AlertRule, AlertRule.destination_id == AlertDestination.id)
        .where(AlertDestination.project_id.in_(project_ids))
        .group_by(AlertDestination.project_id)
    )
    for project_id, alert_destination_count, alert_rule_count in alert_rows.all():
        summary = summaries[project_id]
        summary.alert_destination_count = int(alert_destination_count or 0)
        summary.alert_rule_count = int(alert_rule_count or 0)

    await _populate_latest_scan_jobs(session, summaries)
    await _populate_failing_scan_configs(session, summaries)
    await _populate_monitoring_signals(session, summaries)
    await _populate_firing_monitor_counts(session, summaries)
    await _populate_open_incident_counts(session, summaries)
    await _populate_failing_alert_destinations(session, summaries)

    return summaries


async def _populate_failing_alert_destinations(
    session: AsyncSession,
    summaries: dict[uuid.UUID, ProjectSummary],
) -> None:
    """Count, per project, the ENABLED destinations whose latest delivery failed.

    The per-destination twin of ``_populate_failing_scan_configs`` (MO-15): a
    channel that fails every send stays counted even when another destination
    delivered more recently, and one whose newest delivery went through (a
    retry included — it updates the same row) drops out. Disabled destinations
    send nothing, so their history is not a live failure.
    """
    if not summaries:
        return

    # One correlated LIMIT-1 lookup per enabled destination, which walks
    # ``ix_alert_delivery_destination_created`` backwards and stops at the first
    # row. Ranking every delivery with ``row_number()`` instead read the table's
    # whole unbounded history on every summary read.
    latest_status = (
        select(AlertDelivery.status)
        .where(AlertDelivery.destination_id == AlertDestination.id)
        .order_by(AlertDelivery.created_at.desc(), AlertDelivery.id.desc())
        .limit(1)
        .correlate(AlertDestination)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(AlertDestination.project_id, func.count())
        .where(
            AlertDestination.project_id.in_(list(summaries)),
            AlertDestination.enabled.is_(True),
            latest_status == AlertDeliveryStatus.failed.value,
        )
        .group_by(AlertDestination.project_id)
    )
    for project_id, failing_count in rows.all():
        summaries[project_id].failing_alert_destination_count = int(failing_count or 0)


async def _populate_open_incident_counts(
    session: AsyncSession,
    summaries: dict[uuid.UUID, ProjectSummary],
) -> None:
    """Stamp each summary with the inbox's OWN count of open incidents.

    The sidebar badge next to "Alerting" showed the destination count, so it read
    "Alerting 1" beside a page listing 52 open incidents (tripl-oxkt.16). The fix
    is only worth anything if the number agrees with the page, so the counting is
    not done here at all: ``alerting_service.count_open_incidents`` owns it, next
    to the ``list_alert_inbox`` whose window, cap and lapsed-mute rule it has to
    match. A copy of any of those rules on this side is exactly how the badge and
    the page drift apart, and this module has no business reaching into the
    inbox's private helpers or its models to keep one.
    """
    if not summaries:
        return

    counts = await alerting_service.count_open_incidents(session, list(summaries))
    for project_id, count in counts.items():
        summaries[project_id].open_incident_count = count


async def _populate_failing_scan_configs(
    session: AsyncSession,
    summaries: dict[uuid.UUID, ProjectSummary],
) -> None:
    """Count, per project, the scan configs whose *latest* run failed.

    ``_populate_latest_scan_jobs`` ranks jobs ``partition_by`` project, so it only
    ever sees the single newest job across the whole project. A config that fails
    every run then disappears from the "failed jobs" surface the moment a
    *different* config records a newer success. This rollup ranks jobs
    ``partition_by`` scan_config instead, keeping each config's most recent job,
    and counts the configs whose top job is ``failed``. A project with any such
    config is surfaced as having failed jobs even when its newest job overall
    succeeded.
    """
    if not summaries:
        return

    ranked_jobs = (
        select(
            ScanConfig.project_id.label("project_id"),
            ScanJob.status.label("status"),
            func.row_number()
            .over(
                partition_by=ScanJob.scan_config_id,
                order_by=(ScanJob.created_at.desc(), ScanJob.id.desc()),
            )
            .label("row_number"),
        )
        .join(ScanConfig, ScanConfig.id == ScanJob.scan_config_id)
        .where(ScanConfig.project_id.in_(list(summaries)))
        .subquery()
    )

    rows = await session.execute(
        select(ranked_jobs.c.project_id, func.count())
        .where(
            ranked_jobs.c.row_number == 1,
            ranked_jobs.c.status == ScanJobStatus.failed.value,
        )
        .group_by(ranked_jobs.c.project_id)
    )
    for project_id, failing_count in rows.all():
        summaries[project_id].failing_scan_config_count = int(failing_count or 0)


async def _populate_firing_monitor_counts(
    session: AsyncSession,
    summaries: dict[uuid.UUID, ProjectSummary],
) -> None:
    """Count monitors (alert rules) currently in a FIRING state per project.

    Mirrors ``_alerting_monitors.get_monitors_summary``'s ``firing_count``: each
    rule's per-scope ``AlertRuleState`` rows are rolled up via
    ``summarize_monitor_states``; a rule counts as firing when at least one active
    scope has a recent anomaly. Kept in lockstep so this agrees with
    ``MonitorsSummaryResponse.firing_count`` for the same project.
    """
    if not summaries:
        return

    rule_rows = (
        await session.execute(
            select(AlertDestination.project_id, AlertRule.id)
            .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
            .where(AlertDestination.project_id.in_(list(summaries)))
        )
    ).all()
    if not rule_rows:
        return

    rule_ids = [rule_id for _project_id, rule_id in rule_rows]
    states_by_rule: dict[uuid.UUID, list[AlertRuleState]] = defaultdict(list)
    states = (
        (await session.execute(select(AlertRuleState).where(AlertRuleState.rule_id.in_(rule_ids))))
        .scalars()
        .all()
    )
    for state in states:
        states_by_rule[state.rule_id].append(state)

    # Same per-grid horizon as the Monitors screen and dispatch (tripl-0zpq.162).
    interval_of = await load_monitor_state_intervals(session, states)
    now = datetime.now(UTC)
    for project_id, rule_id in rule_rows:
        rollup = summarize_monitor_states(
            states_by_rule.get(rule_id, []), now=now, interval_of=interval_of
        )
        if rollup.status == "firing":
            summaries[project_id].firing_monitor_count += 1


async def _populate_latest_scan_jobs(
    session: AsyncSession,
    summaries: dict[uuid.UUID, ProjectSummary],
) -> None:
    if not summaries:
        return

    ranked_jobs = (
        select(
            ScanConfig.project_id.label("project_id"),
            ScanJob.id.label("job_id"),
            ScanJob.scan_config_id.label("scan_config_id"),
            ScanConfig.name.label("scan_name"),
            ScanJob.status.label("status"),
            ScanJob.started_at.label("started_at"),
            ScanJob.completed_at.label("completed_at"),
            ScanJob.result_summary.label("result_summary"),
            ScanJob.error_message.label("error_message"),
            ScanJob.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=ScanConfig.project_id,
                order_by=(ScanJob.created_at.desc(), ScanJob.id.desc()),
            )
            .label("row_number"),
        )
        .join(ScanConfig, ScanConfig.id == ScanJob.scan_config_id)
        .where(ScanConfig.project_id.in_(list(summaries)))
        .subquery()
    )

    rows = await session.execute(
        select(
            ranked_jobs.c.project_id,
            ranked_jobs.c.job_id,
            ranked_jobs.c.scan_config_id,
            ranked_jobs.c.scan_name,
            ranked_jobs.c.status,
            ranked_jobs.c.started_at,
            ranked_jobs.c.completed_at,
            ranked_jobs.c.result_summary,
            ranked_jobs.c.error_message,
            ranked_jobs.c.created_at,
        ).where(ranked_jobs.c.row_number == 1)
    )
    for row in rows.all():
        summaries[row.project_id].latest_scan_job = ProjectLatestScanJob(
            id=row.job_id,
            scan_config_id=row.scan_config_id,
            scan_name=row.scan_name,
            status=row.status,
            started_at=row.started_at,
            completed_at=row.completed_at,
            result_summary=row.result_summary,
            error_message=row.error_message,
            created_at=row.created_at,
        )


async def _load_scope_names(
    session: AsyncSession,
    anomalies: list[MetricAnomaly],
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    event_ids = {anomaly.event_id for anomaly in anomalies if anomaly.event_id is not None}
    event_type_ids = {
        anomaly.event_type_id for anomaly in anomalies if anomaly.event_type_id is not None
    }

    event_names: dict[uuid.UUID, str] = {}
    if event_ids:
        event_rows = await session.execute(
            select(Event.id, Event.name).where(Event.id.in_(event_ids))
        )
        event_names = {event_id: name for event_id, name in event_rows.all()}

    event_type_names: dict[uuid.UUID, str] = {}
    if event_type_ids:
        event_type_rows = await session.execute(
            select(EventType.id, EventType.display_name).where(EventType.id.in_(event_type_ids))
        )
        event_type_names = {
            event_type_id: display_name for event_type_id, display_name in event_type_rows.all()
        }

    return event_names, event_type_names


def _resolve_scope_name(
    anomaly: MetricAnomaly,
    *,
    event_names: dict[uuid.UUID, str],
    event_type_names: dict[uuid.UUID, str],
) -> str:
    if anomaly.scope_type == SCOPE_PROJECT_TOTAL:
        return "Project total"
    if anomaly.event_type_id is not None:
        return event_type_names.get(anomaly.event_type_id, "Event type")
    if anomaly.event_id is not None:
        return event_names.get(anomaly.event_id, "Event")
    return anomaly.scope_ref


async def _populate_monitoring_signals(
    session: AsyncSession,
    summaries: dict[uuid.UUID, ProjectSummary],
) -> None:
    if not summaries:
        return

    project_ids = list(summaries)

    # Catalog-metric anomalies carry a NULL scan_config_id and are keyed by
    # metric_definition_id, so the ScanConfig-joined query below silently drops
    # them. Fold them into the count here by reusing the exact open-signal logic
    # the AnomaliesPage uses (metrics_insights_service._count_active_metric_signals_by_project,
    # the batched sibling of _get_active_metric_signals, which classifies each
    # metric's newest anomaly against its latest stored value bucket ON THAT
    # METRIC'S OWN GRID), so the sidebar / ProjectsPage badge agrees with the
    # AnomaliesPage list. The grid half of that claim was aspirational until
    # tripl-l429.17: the batched sibling passed no interval at all, so a daily
    # catalog metric read open on the page and zero here. Batched to
    # O(1) queries so listing N projects does not fan out to N per-project scans.
    # These signals have no scan_config_id and so cannot populate ``latest_signal``
    # (a ProjectLatestSignal requires one); they contribute to
    # ``monitoring_signal_count`` only. This runs before the ``anomaly_rows``
    # early-return so a project with only metric-scope anomalies is still counted.
    #
    # Both halves drop signals a triage verdict hides (muted scope or marked
    # expected, MO-4 / JR-5), the same predicate the expanded list flags
    # ``hidden``, and every signal that has a verdict — its own, or its
    # incident's status (F01, #254) — the Anomalies page's default
    # "Needs verdict" view, so the badge keeps counting what the page shows.
    metric_signals = await _active_metric_signals_by_project(session, project_ids)
    metric_hidden = await signal_triage_service.uncounted_signal_keys(
        session,
        {
            project_id: [
                signal_triage_service.signal_key(None, SCOPE_METRIC, scope_ref, bucket)
                for scope_ref, bucket in keys
            ]
            for project_id, keys in metric_signals.items()
        },
    )
    for project_id, keys in metric_signals.items():
        hidden = metric_hidden.get(project_id, set())
        summaries[project_id].monitoring_signal_count += sum(
            1
            for scope_ref, bucket in keys
            if signal_triage_service.signal_key(None, SCOPE_METRIC, scope_ref, bucket) not in hidden
        )

    latest_anomaly_keys = (
        select(
            ScanConfig.project_id.label("project_id"),
            MetricAnomaly.scan_config_id.label("scan_config_id"),
            MetricAnomaly.scope_type.label("scope_type"),
            MetricAnomaly.scope_ref.label("scope_ref"),
            func.max(MetricAnomaly.bucket).label("bucket"),
        )
        .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
        .where(
            ScanConfig.project_id.in_(project_ids),
            # The AnomaliesPage now lists EVERY open scope as a flat, magnitude-
            # filtered list (tripl-w0ay), so the badge must count the same
            # population — project_total + event_type + per-event — and gate on
            # magnitude below rather than excluding the per-event scope. The
            # "Significant" threshold (not scope exclusion) is what now keeps
            # trivial per-event wobble out of the badge (tripl-yfsj.1, supersedes
            # tripl-posm).
            MetricAnomaly.scope_type.in_([SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE, SCOPE_EVENT]),
        )
        .group_by(
            ScanConfig.project_id,
            MetricAnomaly.scan_config_id,
            MetricAnomaly.scope_type,
            MetricAnomaly.scope_ref,
        )
        .subquery()
    )

    anomaly_rows = (
        await session.execute(
            select(ScanConfig.project_id, ScanConfig.name, ScanConfig.interval, MetricAnomaly)
            .join(ScanConfig, ScanConfig.id == MetricAnomaly.scan_config_id)
            .join(
                latest_anomaly_keys,
                (ScanConfig.project_id == latest_anomaly_keys.c.project_id)
                & (MetricAnomaly.scan_config_id == latest_anomaly_keys.c.scan_config_id)
                & (MetricAnomaly.scope_type == latest_anomaly_keys.c.scope_type)
                & (MetricAnomaly.scope_ref == latest_anomaly_keys.c.scope_ref)
                & (MetricAnomaly.bucket == latest_anomaly_keys.c.bucket),
            )
            .order_by(ScanConfig.project_id, MetricAnomaly.bucket.desc())
        )
    ).all()
    if not anomaly_rows:
        return

    anomalies = [anomaly for _project_id, _scan_name, _interval, anomaly in anomaly_rows]
    event_names, event_type_names = await _load_scope_names(session, anomalies)

    project_total_metrics = (
        select(
            ScanConfig.project_id.label("project_id"),
            EventMetric.scan_config_id.label("scan_config_id"),
            literal(SCOPE_PROJECT_TOTAL).label("scope_type"),
            EventMetric.scan_config_id.label("scope_ref_uuid"),
            func.max(EventMetric.bucket).label("latest_metric_bucket"),
        )
        .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
        .where(
            ScanConfig.project_id.in_(project_ids),
            EventMetric.event_id.is_(None),
            EventMetric.event_type_id.is_not(None),
        )
        .group_by(ScanConfig.project_id, EventMetric.scan_config_id)
    )
    event_type_metrics = (
        select(
            ScanConfig.project_id.label("project_id"),
            EventMetric.scan_config_id.label("scan_config_id"),
            literal(SCOPE_EVENT_TYPE).label("scope_type"),
            EventMetric.event_type_id.label("scope_ref_uuid"),
            func.max(EventMetric.bucket).label("latest_metric_bucket"),
        )
        .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
        .where(
            ScanConfig.project_id.in_(project_ids),
            EventMetric.event_id.is_(None),
            EventMetric.event_type_id.is_not(None),
        )
        .group_by(ScanConfig.project_id, EventMetric.scan_config_id, EventMetric.event_type_id)
    )
    # Per-event branch: event-scope anomalies now flow through ``latest_anomaly_keys``
    # (tripl-yfsj.1), so classify_signal_state needs each event's latest metric
    # bucket to judge freshness — keyed by event_id, mirroring get_active_signals'
    # ``_get_latest_metric_buckets_multi``.
    event_metrics = (
        select(
            ScanConfig.project_id.label("project_id"),
            EventMetric.scan_config_id.label("scan_config_id"),
            literal(SCOPE_EVENT).label("scope_type"),
            EventMetric.event_id.label("scope_ref_uuid"),
            func.max(EventMetric.bucket).label("latest_metric_bucket"),
        )
        .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
        .where(
            ScanConfig.project_id.in_(project_ids),
            EventMetric.event_id.is_not(None),
        )
        .group_by(ScanConfig.project_id, EventMetric.scan_config_id, EventMetric.event_id)
    )
    latest_metric_union = union_all(
        project_total_metrics,
        event_type_metrics,
        event_metrics,
    ).subquery()
    latest_metric_rows = await session.execute(
        select(
            latest_metric_union.c.project_id,
            latest_metric_union.c.scan_config_id,
            latest_metric_union.c.scope_type,
            latest_metric_union.c.scope_ref_uuid,
            latest_metric_union.c.latest_metric_bucket,
        )
    )
    latest_metric_buckets = {
        (project_id, scan_config_id, scope_type, str(scope_ref_uuid)): latest_metric_bucket
        for (
            project_id,
            scan_config_id,
            scope_type,
            scope_ref_uuid,
            latest_metric_bucket,
        ) in latest_metric_rows.all()
    }

    # Scan liveness, off the rows already loaded: an outage anchor stays open only
    # while its scan is still collecting SOMETHING. Keyed on the scan config id
    # itself, never on the stringified scope_ref. Same helper as the AnomaliesPage.
    scan_latest_buckets = latest_bucket_by_scan(
        (scan_config_id, bucket)
        for (_project_id, scan_config_id, _scope_type, _scope_ref), bucket in (
            latest_metric_buckets.items()
        )
    )

    now = datetime.now(UTC)
    # Same per-project open-signal window the metric-scope half above and the
    # AnomaliesPage already honour; without it the two halves of this badge
    # would classify against different horizons.
    recent_windows = await _get_project_recent_signal_windows(session, project_ids)
    open_rows: list[tuple[uuid.UUID, str, str, MetricAnomaly]] = []
    for project_id, scan_name, scan_interval, anomaly in anomaly_rows:
        latest_metric_bucket = latest_metric_buckets.get(
            (project_id, anomaly.scan_config_id, anomaly.scope_type, anomaly.scope_ref)
        )
        state = classify_signal_state(
            anomaly_bucket=anomaly.bucket,
            latest_metric_bucket=latest_metric_bucket,
            now=now,
            interval=scan_interval_to_timedelta(scan_interval),
            recent_window=recent_windows.get(project_id),
            # An outage announced once and never re-emitted is re-checked against
            # the current series rather than its own age (tripl-l429.15), and only
            # while the anchor had volume to lose (tripl-wkwv.4). The magnitude
            # gate below already hides a zero-versus-zero row from this count, so
            # the expectation changes no number here today; it is passed because
            # the badge and the page must reach ``classify_signal_state`` with the
            # same inputs — classifying by different rules is how these two
            # surfaces drifted apart twice before.
            anomaly_actual_count=anomaly.actual_count,
            anomaly_expected_count=anomaly.expected_count,
            scan_latest_bucket=scan_latest_buckets.get(anomaly.scan_config_id),
        )
        if state is None:
            continue
        # Magnitude gate: the badge counts the AnomaliesPage's default "Significant"
        # view — every open signal across all scopes with relative effect >= 0.5,
        # incident children INCLUDED (the expanded page tags them, it does not drop
        # them). No incident dedup here, so the badge equals the page's headline
        # open count (tripl-yfsj.1).
        if not is_significant_signal(anomaly.actual_count, anomaly.expected_count):
            continue
        open_rows.append((project_id, scan_name, state, anomaly))

    # Triage runs over the open, significant rows only: one verdict query for
    # every project, and one incident lookup per project with open rows.
    hidden_keys = await signal_triage_service.uncounted_signal_keys(
        session,
        {
            project_id: [
                signal_triage_service.signal_key(
                    anomaly.scan_config_id, anomaly.scope_type, anomaly.scope_ref, anomaly.bucket
                )
                for row_project_id, _scan_name, _state, anomaly in open_rows
                if row_project_id == project_id
            ]
            for project_id in {row[0] for row in open_rows}
        },
    )
    for project_id, scan_name, state, anomaly in open_rows:
        if signal_triage_service.signal_key(
            anomaly.scan_config_id, anomaly.scope_type, anomaly.scope_ref, anomaly.bucket
        ) in hidden_keys.get(project_id, set()):
            continue

        summary = summaries[project_id]
        summary.monitoring_signal_count += 1

        signal = ProjectLatestSignal(
            scan_config_id=anomaly.scan_config_id,
            scan_name=scan_name,
            scope_type=anomaly.scope_type,
            scope_ref=anomaly.scope_ref,
            scope_name=_resolve_scope_name(
                anomaly,
                event_names=event_names,
                event_type_names=event_type_names,
            ),
            state=state,
            bucket=anomaly.bucket,
            actual_count=anomaly.actual_count,
            expected_count=anomaly.expected_count,
            z_score=anomaly.z_score,
            direction=anomaly.direction,
        )
        if summary.latest_signal is None or signal.bucket > summary.latest_signal.bucket:
            summary.latest_signal = signal


async def _serialize_project(
    session: AsyncSession, project: Project, *, branch_id: uuid.UUID | None = None
) -> ProjectResponse:
    summary = (await _get_project_summaries(session, [project.id], branch_id=branch_id))[project.id]
    response = ProjectResponse.model_validate(project)
    response.summary = summary
    return response


def _serialize_projects(
    projects: list[Project], summaries: dict[uuid.UUID, ProjectSummary]
) -> list[ProjectResponse]:
    return [
        ProjectResponse.model_validate(project).model_copy(
            update={"summary": summaries[project.id]}
        )
        for project in projects
    ]


async def list_projects(session: AsyncSession, user: User) -> list[ProjectResponse]:
    """Every listable project ``user`` is a member of (all of them for an owner).

    The cached list is instance-wide and shared by every caller; membership is
    applied after the cache read, so one cache entry serves every user and a
    membership change needs no cache invalidation.
    """
    # Imported here, not at module top: project_access reads project rows and
    # must stay importable without pulling this module in first.
    from tripl.services.project_access import member_project_ids

    visible = await member_project_ids(session, user)
    return _only_visible(await _list_all_projects(session), visible)


def _only_visible(
    responses: list[ProjectResponse], visible: set[uuid.UUID] | None
) -> list[ProjectResponse]:
    if visible is None:
        return responses
    return [response for response in responses if response.id in visible]


async def _list_all_projects(session: AsyncSession) -> list[ProjectResponse]:
    cached = await cache.get_json(cache.key_projects_list())
    if cached is not None:
        return [ProjectResponse.model_validate(item) for item in cached]

    # Hide demos that are still seeding or failed: a partially built or failed
    # demo must never surface as a real workspace. Real projects (is_demo=False)
    # and successfully seeded demos (generation_status="ready") are listed.
    result = await session.execute(
        select(Project)
        .where(
            or_(
                Project.is_demo.is_(False),
                Project.generation_status == ProjectGenerationStatus.ready.value,
            )
        )
        .order_by(Project.created_at.desc())
    )
    projects = list(result.scalars().all())
    summaries = await _get_project_summaries(session, [project.id for project in projects])
    responses = _serialize_projects(projects, summaries)
    await cache.set_json(
        cache.key_projects_list(),
        [response.model_dump(mode="json") for response in responses],
        ttl_seconds=await _projects_list_ttl(session, [project.id for project in projects]),
    )
    return responses


_PROJECTS_LIST_TTL_SECONDS = 60


async def _projects_list_ttl(session: AsyncSession, project_ids: list[uuid.UUID]) -> int:
    """The list's cache lifetime, cut short by the first timed mute to lapse.

    ``monitoring_signal_count`` leaves muted signals out, and nothing
    invalidates the cache when a mute runs out, so the cached count must expire
    with it; the signal lists apply triage after their own cache and need no
    such cap.
    """
    now = datetime.now(UTC)
    expiry = await signal_triage_service.earliest_mute_expiry(session, project_ids, now=now)
    if expiry is None:
        return _PROJECTS_LIST_TTL_SECONDS
    remaining = math.ceil((expiry - now).total_seconds())
    return max(1, min(_PROJECTS_LIST_TTL_SECONDS, remaining))


@dataclass(frozen=True)
class ProjectMutationScope:
    """The caller's standing in one project, as the mutation gate reads it.

    ``role`` is the caller's project role from
    :func:`tripl.services.project_access.member_role`: ``"owner"`` for the
    instance owner, the membership role (capped by the instance role) for a
    member, ``None`` for a non-member. Mutating a project's contents takes an
    editing role, the same rule ``api.deps.require_project_mutation_access``
    enforces on the routes; project-identity edits (rename, reset, delete) are
    narrower and stay with the instance owner and the project's creator
    (``api.v1.projects._is_project_manager``).
    """

    project_id: uuid.UUID
    is_demo: bool
    role: str | None

    def allows(self) -> bool:
        from tripl.services.project_access import can_edit

        return can_edit(self.role)


async def with_can_mutate(
    session: AsyncSession,
    projects: Sequence[ProjectResponse],
    user: User,
    may_mutate: Callable[[ProjectMutationScope], bool],
) -> list[ProjectResponse]:
    """Copies of ``projects`` with ``can_mutate`` and ``my_role`` for ``user``.

    ``may_mutate`` is the caller's own gate over a :class:`ProjectMutationScope`
    (``api.deps.can_mutate_project``), so the flag is the same predicate the
    mutation routes enforce, not a restatement of it. ``my_role`` is the
    caller's project role; it is left at its default for a project the caller
    is not a member of, which the membership-filtered callers never pass. The
    roles come from ONE membership query (none for an instance owner), so a
    project list costs a single extra round trip. Applied after
    ``list_projects``' cache read, never before its write: both fields belong to
    the caller, the cache to everyone.
    """
    # Imported here, not at module top: project_access is imported by the
    # request gates, and keeping this edge lazy keeps the import graph acyclic.
    from tripl.services.project_access import member_roles

    roles = await member_roles(session, user, [project.id for project in projects])
    annotated: list[ProjectResponse] = []
    for project in projects:
        role = roles.get(project.id)
        update: dict[str, object] = {
            "can_mutate": may_mutate(
                ProjectMutationScope(project_id=project.id, is_demo=project.is_demo, role=role)
            )
        }
        if role is not None:
            update["my_role"] = role
        annotated.append(project.model_copy(update=update))
    return annotated


# A demo's ``demo_last_accessed_at`` is only rewritten when it is this stale, so a
# burst of reads doesn't turn every GET into a write. Comfortably tighter than the
# tick's idle-pause window so an active viewer always keeps the demo resumed.
_DEMO_ACCESS_TOUCH_SECONDS = 60


async def get_project(
    session: AsyncSession, slug: str, *, branch_id: uuid.UUID | None = None
) -> ProjectResponse:
    """The project with its summary; ``branch_id`` scopes the plan counters (SH-11)."""
    project = await resolve_project(session, slug)
    # Decide WHILE attributes are fresh, serialize, then commit the touch last: the
    # commit expires the ORM object, but the response is already a detached model,
    # so serialization never lazy-loads on the async session (MissingGreenlet).
    should_touch = _should_touch_demo_access(project)
    response = await _serialize_project(session, project, branch_id=branch_id)
    if should_touch:
        project.demo_last_accessed_at = datetime.now(UTC)
        await session.commit()
    return response


def _should_touch_demo_access(project: Project) -> bool:
    """Whether to bump a demo's ``demo_last_accessed_at`` on this access.

    Explicit demo activity (a project GET, or a reset which returns through
    ``get_project``) marks the demo active so the runtime tick keeps advancing it;
    the tick pauses demos whose last access is older than ``DEMO_IDLE_PAUSE_MINUTES``
    so inactive demos stop consuming worker time, and resumes them on the next
    access. No-op for real projects; throttled so repeated reads don't write on
    every call.
    """
    if not project.is_demo:
        return False
    last = project.demo_last_accessed_at
    if last is None:
        return True
    last_aware = last if last.tzinfo is not None else last.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last_aware).total_seconds() >= _DEMO_ACCESS_TOUCH_SECONDS


async def create_project(
    session: AsyncSession,
    data: ProjectCreate,
    *,
    created_by: uuid.UUID | None = None,
) -> ProjectCreateResponse:
    """Create a real (non-demo) project, optionally seeded from a template.

    ``created_by`` records who made it, mirroring demo provisioning. The API
    always passes it; it stays optional so scripts/fixtures can create a
    creator-less project (which is then owner-managed, see
    ``api.v1.projects._require_project_manager``).

    ``data.template_id`` names a built-in template (F21, GH #274). It is resolved
    BEFORE any write, so an unknown id is a 422 that inserts nothing. The
    template's plan is seeded onto a draft branch in the SAME transaction as
    the project, its main branch and the creator's membership, with one commit:
    a seeding failure rolls the whole project back. Main stays empty, so the
    main-scoped summary counters read 0 until the branch is merged.
    """
    from tripl.services import project_template_service
    from tripl.services.project_member_service import grant_membership

    template = (
        project_template_service.resolve_template(data.template_id)
        if data.template_id is not None
        else None
    )
    existing = await session.execute(select(Project).where(Project.slug == data.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Project with this slug already exists")

    project = Project(
        **data.model_dump(exclude={"template_id"}),
        created_by_user_id=created_by,
        organization_id=owning_org_id(),
    )
    session.add(project)
    await session.flush()
    # Every project owns one main branch (the live plan); create it up front so
    # branch_id resolution on plan entities is a plain read thereafter.
    main_branch_id = await plan_branch_service.ensure_main_branch_id(session, project.id)
    # The creator is an editor member from the first commit: without the row a
    # non-owner creator could not even see the project they just made.
    if created_by is not None:
        await grant_membership(
            session,
            project_id=project.id,
            user_id=created_by,
            added_by_user_id=created_by,
        )
    template_branch_id: uuid.UUID | None = None
    if template is not None:
        branch = await project_template_service.apply_template(
            session,
            project_id=project.id,
            slug=project.slug,
            main_branch_id=main_branch_id,
            template=template,
            created_by=created_by,
        )
        template_branch_id = branch.id
    await session.commit()
    await session.refresh(project)
    await cache.delete_prefix(cache.prefix_projects())
    serialized = await _serialize_project(session, project)
    return ProjectCreateResponse(**serialized.model_dump(), template_branch_id=template_branch_id)


async def update_project(session: AsyncSession, slug: str, data: ProjectUpdate) -> ProjectResponse:
    project = await resolve_project(session, slug)
    update_data = data.model_dump(exclude_unset=True)
    new_slug = update_data.get("slug")
    if new_slug is not None and new_slug != project.slug:
        existing = await session.execute(
            select(Project).where(Project.slug == new_slug, Project.id != project.id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Project with this slug already exists")
    for key, value in update_data.items():
        setattr(project, key, value)
    # Keep the legacy per-scan field synchronized during the rolling-deploy
    # compatibility window. New runtime reads use Project exclusively, while an
    # older worker/API container still sees the same policy on versioned scans.
    keep_releases = update_data.get("app_version_keep_releases")
    if keep_releases is not None:
        await session.execute(
            update(ScanConfig)
            .where(
                ScanConfig.project_id == project.id,
                ScanConfig.app_version_column.is_not(None),
            )
            .values(app_version_keep_releases=keep_releases)
        )
    slug_changed = new_slug is not None and new_slug != slug
    await session.commit()
    await session.refresh(project)
    await cache.delete_prefix(cache.prefix_projects())
    if slug_changed:
        await _invalidate_slug_caches(slug)
        await _invalidate_slug_caches(project.slug)
        from tripl.services.search_service import reindex_project_branch

        branch_ids = (
            await session.scalars(select(PlanBranch.id).where(PlanBranch.project_id == project.id))
        ).all()
        for branch_id in branch_ids:
            await reindex_project_branch(
                session, project_id=project.id, branch_id=branch_id, slug=project.slug
            )
    return await _serialize_project(session, project)


def demo_data_source_name(slug: str) -> str:
    """Return the canonical DataSource name used by the demo project for *slug*."""
    return f"Demo warehouse {slug}"


async def purge_project_rows(session: AsyncSession, project: Project) -> None:
    """Delete a project and the data sources it owns, WITHOUT committing.

    Split out of :func:`delete_project` so demo reset can drop the old demo and
    seed its replacement inside a single transaction: if seeding fails, the
    rollback puts the old demo back untouched (tripl-2su6.13).

    Data sources OWNED by this project (a demo's synthetic warehouse) are deleted
    explicitly, ahead of the project row, so nothing leaks a workspace-wide
    orphan. Real, workspace-global sources carry project_id IS NULL and are
    untouched. The FK is ``ondelete="CASCADE"``, so
    both databases would remove them anyway — this used to claim otherwise, that
    SQLite has cascades off, which stopped being true when the suite began
    setting ``PRAGMA foreign_keys=ON`` on every connection (tests/_sqlite.py).
    Doing it explicitly is still worth the line: it puts the cleanup where a
    reader of this function can see it, rather than resting on a schema detail
    two files away.

    The metric-scope anomalies go first, and they are the one thing here that a
    cascade genuinely cannot reach: ``MetricAnomaly`` has no ``project_id`` and
    no FK to the metric it describes — a catalog-metric anomaly is addressed by
    ``scope_type='metric'`` plus a ``scope_ref`` holding the metric's UUID as
    TEXT, with a NULL ``scan_config_id``. Dropping the project cascades the
    ``metric_definitions`` rows away and leaves those anomalies behind forever,
    pointing at ids nothing resolves (tripl-0zpq.179). Scan-scope anomalies are
    not in this sweep: they hang off ``scan_config_id``, which the cascade does
    reach.

    The flush matters: it forces the DELETE out before any later INSERT, so a
    caller re-creating a project under the same (unique) slug within this same
    transaction cannot trip the unique constraint on SQLAlchemy's insert-before-
    delete unit-of-work ordering.
    """
    metric_ids = (
        await session.scalars(
            select(MetricDefinition.id).where(MetricDefinition.project_id == project.id)
        )
    ).all()
    if metric_ids:
        await session.execute(
            delete(MetricAnomaly).where(
                MetricAnomaly.scope_type == MetricScopeType.metric.value,
                MetricAnomaly.scope_ref.in_([str(metric_id) for metric_id in metric_ids]),
            )
        )
    await session.execute(delete(DataSource).where(DataSource.project_id == project.id))
    await session.delete(project)
    await session.flush()


async def delete_project(session: AsyncSession, slug: str) -> None:
    project = await resolve_project(session, slug)
    await purge_project_rows(session, project)
    await session.commit()
    await cache.delete_prefix(cache.prefix_projects())
    await cache.delete_prefix(cache.prefix_data_sources())
    await _invalidate_slug_caches(slug)


async def _invalidate_slug_caches(slug: str) -> None:
    await cache.delete_prefix(cache.prefix_event_types(slug))
    await cache.delete_prefix(cache.prefix_meta_fields(slug))
    await cache.delete_prefix(cache.prefix_signals(slug))
