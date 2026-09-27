import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import BranchIdDep, EditorUserDep, SessionDep
from tripl.api.v1.alerting import incident_audit_name
from tripl.models.domain_enums import MetricScopeType
from tripl.models.event import EventStatus
from tripl.models.user import User
from tripl.schemas.event_metric import (
    ActiveSignalsQuery,
    AnomalyAttributionResponse,
    AppVersionAdoptionResponse,
    AppVersionSeriesResponse,
    BreakdownTimelineResponse,
    DistributionDriftsResponse,
    EventMetricBreakdownsResponse,
    EventMetricsResponse,
    EventWindowMetricsRequest,
    EventWindowMetricsResponse,
    MetricSignalResponse,
    OverviewKpiSeriesResponse,
    ReleaseRegressionsResponse,
    SeasonalityHeatmapResponse,
    SignalExpectedRequest,
    SignalMuteRequest,
    SignalSeriesQuery,
    SignalSeriesResponse,
    SignalTriageScope,
    SignalTriageState,
    SignalVerdictCountsResponse,
    SignalVerdictRequest,
    SignalVerdictResponse,
    TopEventResponse,
    TopMoverItem,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import (
    alerting_service,
    anomaly_attribution_service,
    audit_service,
    metrics_insights_service,
    metrics_service,
    signal_triage_service,
    signal_verdict_service,
)

router = APIRouter(tags=["metrics"])

TimeFrom = Annotated[datetime | None, Query(alias="from")]
TimeTo = Annotated[datetime | None, Query(alias="to")]
EventIds = Annotated[list[uuid.UUID] | None, Query(alias="event_id")]
# Every ``scope_type`` below is declared MetricScopeType rather than str: the
# value is bound against a native Postgres ``metric_scope_type`` column, so an
# out-of-enum string used to reach the driver and surface as a 500 instead of a
# 422 (tripl-57g0). Spelling out the full enum is deliberate — the endpoints
# that support only a subset (app-versions, distribution-drifts) already raise
# their own 4xx for the scopes they don't handle, so this rejects garbage at the
# edge without narrowing anything that works today.


@router.get(
    "/projects/{slug}/events-metrics",
    response_model=EventMetricsResponse,
)
async def get_events_metrics(
    session: SessionDep,
    slug: str,
    # ``?branch=``: evaluate the tag / status / search filter on that branch's
    # events, which the Events page lists, not on main's (tripl-vk1p).
    branch_id: BranchIdDep,
    event_type_id: uuid.UUID | None = None,
    # FreeTextFilter: binds into an ILIKE, so a NUL aborts inside asyncpg
    # before SQL runs (tripl-8wez).
    search: FreeTextFilter | None = None,
    tag: FreeTextFilter | None = None,
    # list[EventStatus] (not list[str]): the filter lands in Event.status.in_(),
    # a native Postgres enum, so one bad member 500'd the whole request. Mirrors
    # GET /events?status= — same filter, same enum, now the same 422 (tripl-57g0).
    status: Annotated[list[EventStatus] | None, Query()] = None,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> EventMetricsResponse:
    return await metrics_service.get_events_metrics(
        session,
        slug,
        event_type_id=event_type_id,
        search=search,
        tag=tag,
        status=[member.value for member in status] if status else None,
        time_from=time_from,
        time_to=time_to,
        branch_id=branch_id,
    )


@router.post(
    "/projects/{slug}/events/window-metrics",
    response_model=list[EventWindowMetricsResponse],
)
async def get_events_window_metrics(
    session: SessionDep,
    slug: str,
    data: EventWindowMetricsRequest,
) -> list[EventWindowMetricsResponse]:
    return await metrics_service.get_events_window_metrics(
        session,
        slug,
        event_ids=data.event_ids,
        time_from=data.time_from,
        time_to=data.time_to,
    )


@router.get(
    "/projects/{slug}/overview/kpi-series",
    response_model=OverviewKpiSeriesResponse,
)
async def get_overview_kpi_series(
    session: SessionDep,
    slug: str,
    days: int = Query(14, ge=1, le=365),
) -> OverviewKpiSeriesResponse:
    return await metrics_service.get_overview_kpi_series(session, slug, days=days)


@router.get(
    "/projects/{slug}/overview/top-events",
    response_model=list[TopEventResponse],
)
async def get_overview_top_events(
    session: SessionDep,
    slug: str,
    window_hours: int = Query(48, ge=1, le=720),
    limit: int = Query(6, ge=1, le=100),
) -> list[TopEventResponse]:
    return await metrics_service.get_top_events_by_volume(
        session,
        slug,
        window_hours=window_hours,
        limit=limit,
    )


@router.get(
    "/projects/{slug}/metrics/total",
    response_model=EventMetricsResponse,
)
async def get_project_total_metrics(
    session: SessionDep,
    slug: str,
    scan_config_id: uuid.UUID | None = None,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> EventMetricsResponse:
    return await metrics_service.get_project_total_metrics(
        session,
        slug,
        scan_config_id=scan_config_id,
        time_from=time_from,
        time_to=time_to,
    )


@router.get(
    "/projects/{slug}/events/{event_id}/metrics",
    response_model=EventMetricsResponse,
)
async def get_event_metrics(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> EventMetricsResponse:
    return await metrics_service.get_event_metrics(session, slug, event_id, time_from, time_to)


@router.get(
    "/projects/{slug}/events/{event_id}/metrics/breakdowns",
    response_model=EventMetricBreakdownsResponse,
)
async def get_event_metric_breakdowns(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    column: FreeTextFilter | None = None,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> EventMetricBreakdownsResponse:
    return await metrics_service.get_event_metric_breakdowns(
        session,
        slug,
        event_id,
        column=column,
        time_from=time_from,
        time_to=time_to,
    )


@router.get(
    "/projects/{slug}/event-types/{event_type_id}/metrics",
    response_model=EventMetricsResponse,
)
async def get_event_type_metrics(
    session: SessionDep,
    slug: str,
    event_type_id: uuid.UUID,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> EventMetricsResponse:
    return await metrics_service.get_event_type_metrics(
        session, slug, event_type_id, time_from, time_to
    )


@router.get(
    "/projects/{slug}/anomalies/signals",
    response_model=list[MetricSignalResponse],
)
async def get_active_signals(
    session: SessionDep,
    slug: str,
    event_ids: EventIds = None,
    expanded: bool = False,
    needs_verdict: bool = False,
) -> list[MetricSignalResponse]:
    """Cacheable no-args variant. For filtering by a large event-id list
    (>>a few), prefer ``POST /anomalies/signals/query`` — GET's query-string
    overflow is real once you cross ~50 ids (proxy/browser limits).

    ``expanded=true`` (the AnomaliesPage view) also surfaces per-event scopes
    and keeps each incident's child rows, tagged ``incident_child`` rather than
    collapsed into the parent project_total signal.

    ``needs_verdict=true`` keeps only signals with no verdict that nothing
    hides — the Anomalies page's default filter (F01, #254)."""
    signals = await metrics_insights_service.get_active_signals(
        session, slug, event_ids=event_ids, expanded=expanded
    )
    return metrics_insights_service.needing_verdict(signals) if needs_verdict else signals


@router.post(
    "/projects/{slug}/anomalies/signals/query",
    response_model=list[MetricSignalResponse],
)
async def query_active_signals(
    session: SessionDep,
    slug: str,
    data: ActiveSignalsQuery,
) -> list[MetricSignalResponse]:
    return await metrics_insights_service.get_active_signals(
        session, slug, event_ids=data.event_ids or None
    )


@router.get(
    "/projects/{slug}/anomalies/{anomaly_id}/attribution",
    response_model=AnomalyAttributionResponse,
)
async def get_anomaly_attribution(
    session: SessionDep,
    slug: str,
    anomaly_id: uuid.UUID,
) -> AnomalyAttributionResponse:
    """Why did it change, for one anomaly (F02, #255): the contribution
    breakdown and release context stored when it was detected, for lazy loading
    (a chart marker other than the latest signal). 404 when the anomaly is not
    this project's."""
    return await anomaly_attribution_service.get_anomaly_attribution(session, slug, anomaly_id)


@router.post(
    "/projects/{slug}/anomalies/signals/series",
    response_model=list[SignalSeriesResponse],
)
async def query_signal_series(
    session: SessionDep,
    slug: str,
    data: SignalSeriesQuery,
) -> list[SignalSeriesResponse]:
    """Row sparklines for many open signals in one request (MO-19). POST for
    the same reason as ``/signals/query``: the batch outgrows a query string."""
    return await metrics_insights_service.get_signal_series(session, slug, data.scopes)


# --- Signal triage (MO-4 / JR-5) ----------------------------------------------
# Verdicts on open signals no rule routed to an incident; a routed signal is
# triaged in the alert inbox and these refuse it (409). Each POST answers with
# the signal's triage fields as the lists will show them; each DELETE is the
# Undo and is idempotent. Scope query parameters mirror ``SignalTriageScope``.

ScanConfigParam = Annotated[uuid.UUID | None, Query()]
# FreeTextFilter strips a NUL before the length check: the value is bound into
# an equality, and a NUL aborts inside asyncpg before SQL runs (tripl-8wez).
ScopeRefParam = Annotated[FreeTextFilter, Query(min_length=1, max_length=64)]


def _signal_audit_name(scope_type: MetricScopeType | str, scope_ref: str) -> str:
    return f"{scope_type}:{scope_ref}"


@router.post(
    "/projects/{slug}/anomalies/signals/acknowledge",
    response_model=SignalTriageState,
)
async def acknowledge_signal(
    session: SessionDep,
    slug: str,
    data: SignalTriageScope,
    current_user: EditorUserDep,
) -> SignalTriageState:
    target = await signal_triage_service.resolve_target(session, slug, data)
    row, created = await signal_triage_service.acknowledge(session, target, user_id=current_user.id)
    # A repeat POST (retry, double click) changes nothing, so it is not audited.
    if created and row is not None:
        await audit_service.record(
            session,
            user=current_user,
            action="signal.acknowledge",
            target_type="signal",
            target_id=row.id,
            target_name=_signal_audit_name(data.scope_type, data.scope_ref),
            project=target.project,
            payload={"bucket": target.bucket.isoformat()},
        )
    return await signal_triage_service.state_after_write(session, target.project_id, target.key)


@router.delete("/projects/{slug}/anomalies/signals/acknowledge", status_code=204)
async def unacknowledge_signal(
    session: SessionDep,
    slug: str,
    current_user: EditorUserDep,
    scope_type: MetricScopeType,
    scope_ref: ScopeRefParam,
    bucket: datetime,
    scan_config_id: ScanConfigParam = None,
) -> None:
    project, row = await signal_triage_service.unacknowledge(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        bucket=bucket,
    )
    if row is None:
        return
    await audit_service.record(
        session,
        user=current_user,
        action="signal.unacknowledge",
        target_type="signal",
        target_id=row.id,
        target_name=_signal_audit_name(scope_type, scope_ref),
        project=project,
        payload={"bucket": bucket.isoformat()},
    )


@router.post(
    "/projects/{slug}/anomalies/signals/mute",
    response_model=SignalTriageState,
)
async def mute_signal_scope(
    session: SessionDep,
    slug: str,
    data: SignalMuteRequest,
    current_user: EditorUserDep,
) -> SignalTriageState:
    """Hide every signal on the scope for 24 h, 7 d or until unmuted."""
    target = await signal_triage_service.resolve_target(session, slug, data)
    row = await signal_triage_service.mute(session, target, data.duration, user_id=current_user.id)
    await audit_service.record(
        session,
        user=current_user,
        action="signal.mute",
        target_type="signal",
        target_id=row.id,
        target_name=_signal_audit_name(data.scope_type, data.scope_ref),
        project=target.project,
        payload={
            "duration": data.duration,
            "muted_until": row.muted_until.isoformat() if row.muted_until else None,
        },
    )
    return await signal_triage_service.state_after_write(session, target.project_id, target.key)


@router.delete("/projects/{slug}/anomalies/signals/mute", status_code=204)
async def unmute_signal_scope(
    session: SessionDep,
    slug: str,
    current_user: EditorUserDep,
    scope_type: MetricScopeType,
    scope_ref: ScopeRefParam,
    scan_config_id: ScanConfigParam = None,
) -> None:
    project, row = await signal_triage_service.unmute(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
    )
    if row is None:
        return
    await audit_service.record(
        session,
        user=current_user,
        action="signal.unmute",
        target_type="signal",
        target_id=row.id,
        target_name=_signal_audit_name(scope_type, scope_ref),
        project=project,
    )


@router.post(
    "/projects/{slug}/anomalies/signals/expected",
    response_model=SignalTriageState,
)
async def mark_signal_expected(
    session: SessionDep,
    slug: str,
    data: SignalExpectedRequest,
    current_user: EditorUserDep,
) -> SignalTriageState:
    """Record a known cause: annotate the signal's bucket and hide the signal."""
    target = await signal_triage_service.resolve_target(session, slug, data)
    row = await signal_triage_service.mark_expected(
        session, target, data.note, user_id=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="signal.mark_expected",
        target_type="signal",
        target_id=row.id,
        target_name=_signal_audit_name(data.scope_type, data.scope_ref),
        project=target.project,
        payload={"bucket": target.bucket.isoformat(), "note": row.note},
    )
    return await signal_triage_service.state_after_write(session, target.project_id, target.key)


@router.delete("/projects/{slug}/anomalies/signals/expected", status_code=204)
async def unmark_signal_expected(
    session: SessionDep,
    slug: str,
    current_user: EditorUserDep,
    scope_type: MetricScopeType,
    scope_ref: ScopeRefParam,
    bucket: datetime,
    scan_config_id: ScanConfigParam = None,
) -> None:
    project, row = await signal_triage_service.unmark_expected(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        bucket=bucket,
    )
    if row is None:
        return
    await audit_service.record(
        session,
        user=current_user,
        action="signal.unmark_expected",
        target_type="signal",
        target_id=row.id,
        target_name=_signal_audit_name(scope_type, scope_ref),
        project=project,
        payload={"bucket": bucket.isoformat()},
    )


# --- Signal verdicts (F01, #254) ----------------------------------------------
# What a signal turned out to be, on every signal: one routed to an incident
# writes through to the incident (the source of truth), see
# ``signal_verdict_service``. POST answers with the signal's state as the lists
# will show it; DELETE clears the verdict and is idempotent.


async def _record_incident_move(
    session: AsyncSession,
    *,
    user: User,
    slug: str,
    correlation_group_id: uuid.UUID,
    action: str,
    note: str | None,
) -> None:
    """The ``alert_inbox.<action>`` audit row for an incident a verdict moved.

    A verdict on a routed signal writes through to its incident, so the move is
    audited exactly as the same click in the inbox is (``api/v1/alerting.py``):
    same action name, ``alert_correlation_group`` target, same target name,
    plus ``source`` saying the signal verdict made it. Commits, together with
    the ``signal.*`` row the caller filed before it.
    """
    try:
        group = await alerting_service.get_alert_inbox_group(session, slug, correlation_group_id)
    except HTTPException:
        # The incident's deliveries are gone; name it by id, as the inbox does.
        group = None
    await audit_service.record(
        session,
        user=user,
        action=f"alert_inbox.{action}",
        target_type="alert_correlation_group",
        target_id=correlation_group_id,
        target_name=incident_audit_name(group, correlation_group_id),
        project_slug=slug,
        payload={
            "action": action,
            "note": note,
            "muted_until": None,
            "source": "signal_verdict",
        },
        commit=False,
    )
    await session.commit()


@router.post("/projects/{slug}/signals/verdict", response_model=SignalVerdictResponse)
async def set_signal_verdict(
    session: SessionDep,
    slug: str,
    data: SignalVerdictRequest,
    current_user: EditorUserDep,
) -> SignalVerdictResponse:
    user_id = current_user.id
    result = await signal_verdict_service.set_verdict(session, slug, data, user_id=user_id)
    # A repeat of the verdict on record changes nothing, so it is not audited.
    if result.changed:
        await audit_service.record(
            session,
            user=current_user,
            action="signal.verdict",
            target_type="signal",
            target_id=result.row_id,
            target_name=_signal_audit_name(data.scope_type, data.scope_ref),
            project=result.project,
            payload={
                "bucket": result.key[3].isoformat(),
                "verdict": str(data.verdict),
                "expected_reason": (
                    str(data.expected_reason) if data.expected_reason is not None else None
                ),
                "note": data.note,
                "previous": result.previous,
                "incident_id": str(result.incident_id) if result.incident_id else None,
            },
            commit=result.incident_action is None,
        )
        if result.incident_id is not None and result.incident_action is not None:
            await _record_incident_move(
                session,
                user=current_user,
                slug=slug,
                correlation_group_id=result.incident_id,
                action=result.incident_action,
                note=data.note,
            )
    return await signal_verdict_service.verdict_state(session, result.project_id, result.key)


@router.delete("/projects/{slug}/signals/verdict", status_code=204)
async def clear_signal_verdict(
    session: SessionDep,
    slug: str,
    current_user: EditorUserDep,
    scope_type: MetricScopeType,
    scope_ref: ScopeRefParam,
    bucket: datetime,
    scan_config_id: ScanConfigParam = None,
) -> None:
    result = await signal_verdict_service.clear_verdict(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        bucket=bucket,
        user_id=current_user.id,
    )
    if not result.changed:
        return
    await audit_service.record(
        session,
        user=current_user,
        action="signal.clear_verdict",
        target_type="signal",
        target_id=result.row_id,
        target_name=_signal_audit_name(scope_type, scope_ref),
        project=result.project,
        payload={
            "bucket": bucket.isoformat(),
            "previous": result.previous,
            "reopened_incident_id": (
                str(result.reopened_incident_id) if result.reopened_incident_id else None
            ),
        },
        commit=result.reopened_incident_id is None,
    )
    if result.reopened_incident_id is not None:
        await _record_incident_move(
            session,
            user=current_user,
            slug=slug,
            correlation_group_id=result.reopened_incident_id,
            action="reopen",
            note=None,
        )


@router.get(
    "/projects/{slug}/signals/verdict-counts",
    response_model=SignalVerdictCountsResponse,
)
async def get_signal_verdict_counts(
    session: SessionDep,
    slug: str,
) -> SignalVerdictCountsResponse:
    """Verdict tallies over the open signals (for the project health score)."""
    return await metrics_insights_service.get_signal_verdict_counts(session, slug)


@router.get(
    "/projects/{slug}/scans/{scan_config_id}/top-movers",
    response_model=list[TopMoverItem],
)
async def get_top_movers(
    session: SessionDep,
    slug: str,
    scan_config_id: uuid.UUID,
    scope_type: MetricScopeType,
    scope_ref: FreeTextFilter,
    bucket: datetime,
    limit: int = Query(10, ge=1, le=100),
) -> list[TopMoverItem]:
    """Top-N breakdown rows that "moved" a given anomaly bucket, |z| desc."""
    return await metrics_insights_service.get_top_movers(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        bucket=bucket,
        limit=limit,
    )


@router.get(
    "/projects/{slug}/scans/{scan_config_id}/seasonality",
    response_model=SeasonalityHeatmapResponse,
)
async def get_seasonality_heatmap(
    session: SessionDep,
    slug: str,
    scan_config_id: uuid.UUID,
    scope_type: MetricScopeType,
    scope_ref: FreeTextFilter,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> SeasonalityHeatmapResponse:
    """7×24 hour-of-day × weekday heatmap of volume and anomaly density."""
    return await metrics_insights_service.get_seasonality_heatmap(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        time_from=time_from,
        time_to=time_to,
    )


@router.get(
    "/projects/{slug}/scans/{scan_config_id}/breakdown-timeline",
    response_model=BreakdownTimelineResponse,
)
async def get_breakdown_timeline(
    session: SessionDep,
    slug: str,
    scan_config_id: uuid.UUID,
    scope_type: MetricScopeType,
    scope_ref: FreeTextFilter,
    breakdown_column: FreeTextFilter,
    breakdown_value: FreeTextFilter,
    is_other: bool = False,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> BreakdownTimelineResponse:
    """Per-bucket count timeline for one breakdown_value (drill-down)."""
    return await metrics_insights_service.get_breakdown_timeline(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        breakdown_column=breakdown_column,
        breakdown_value=breakdown_value,
        is_other=is_other,
        time_from=time_from,
        time_to=time_to,
    )


@router.get(
    "/projects/{slug}/scans/{scan_config_id}/app-versions",
    response_model=AppVersionSeriesResponse,
)
async def get_app_version_series(
    session: SessionDep,
    slug: str,
    scan_config_id: uuid.UUID,
    scope_type: MetricScopeType = MetricScopeType.project_total,
    scope_ref: FreeTextFilter | None = None,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> AppVersionSeriesResponse:
    return await metrics_service.get_app_version_series(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        time_from=time_from,
        time_to=time_to,
    )


@router.get(
    "/projects/{slug}/scans/{scan_config_id}/version-adoption",
    response_model=AppVersionAdoptionResponse,
)
async def get_app_version_adoption(
    session: SessionDep,
    slug: str,
    scan_config_id: uuid.UUID,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> AppVersionAdoptionResponse:
    return await metrics_service.get_app_version_adoption(
        session,
        slug,
        scan_config_id=scan_config_id,
        time_from=time_from,
        time_to=time_to,
    )


@router.get(
    "/projects/{slug}/scans/{scan_config_id}/release-regressions",
    response_model=ReleaseRegressionsResponse,
)
async def get_release_regressions(
    session: SessionDep,
    slug: str,
    scan_config_id: uuid.UUID,
    scope_type: MetricScopeType | None = None,
) -> ReleaseRegressionsResponse:
    return await metrics_service.get_release_regressions(
        session,
        slug,
        scan_config_id=scan_config_id,
        scope_type=scope_type,
    )


@router.get(
    "/projects/{slug}/distribution-drifts",
    response_model=DistributionDriftsResponse,
)
async def get_distribution_drifts(
    session: SessionDep,
    slug: str,
    scope_type: MetricScopeType,
    scope_ref: FreeTextFilter,
    scan_config_id: uuid.UUID | None = None,
    time_from: TimeFrom = None,
    time_to: TimeTo = None,
) -> DistributionDriftsResponse:
    return await metrics_insights_service.get_distribution_drifts(
        session,
        slug,
        scope_type=scope_type,
        scope_ref=scope_ref,
        scan_config_id=scan_config_id,
        time_from=time_from,
        time_to=time_to,
    )
