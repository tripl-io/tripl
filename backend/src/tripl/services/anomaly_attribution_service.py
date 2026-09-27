"""Read side of "why did it change?" (F02, #255).

The metrics worker stores one ``MetricAnomalyAttribution`` per volume anomaly at
detection time (``worker.tasks.metrics.attribution``). This module lays those
rows onto the signal payloads — the signals lists, the drilldown's
``latest_signal`` — and serves the lazy per-anomaly route. Every read is a
batched lookup keyed by anomaly id, never one query per signal.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import ColumnElement, String, and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_METRIC,
    SCOPE_PROJECT_TOTAL,
)
from tripl.core.analyzers.attribution import attribution_headline, release_line
from tripl.models.event import Event
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.schemas.event_metric import (
    AnomalyAttributionResponse,
    AttributionStatus,
    EventMetricsResponse,
    MetricSignalResponse,
    SignalAttribution,
)
from tripl.services.project_lookup import get_project_by_slug

ATTRIBUTED_SCOPES: frozenset[str] = frozenset({SCOPE_PROJECT_TOTAL, SCOPE_EVENT_TYPE, SCOPE_EVENT})


def has_breakdown_columns() -> ColumnElement[bool]:
    """``events.metric_breakdown_columns`` is set and not an empty list."""
    as_text = cast(Event.metric_breakdown_columns, String)
    return and_(Event.metric_breakdown_columns.is_not(None), as_text != "[]", as_text != "null")


def _stored_payload(row: MetricAnomalyAttribution) -> dict[str, Any]:
    return {"delta": row.delta, "columns": row.columns or [], "release": row.release}


def attribution_from_row(
    row: MetricAnomalyAttribution, *, bucket: datetime, direction: str | None = None
) -> SignalAttribution:
    """The stored row as the API shape, with the one-liners the alerts use.

    ``headline`` / ``release_line`` come from ``core.analyzers.attribution`` —
    the same functions ``services.attribution_text`` joins into the alert line —
    and the UI renders them verbatim.
    """
    payload = _stored_payload(row)
    return SignalAttribution.model_validate(
        {
            **payload,
            "headline": attribution_headline(payload, direction),
            "release_line": release_line(payload, anomaly_bucket=bucket, direction=direction),
            "computed_at": row.computed_at,
        }
    )


async def load_attribution_rows(
    session: AsyncSession, anomaly_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, MetricAnomalyAttribution]:
    wanted = {anomaly_id for anomaly_id in anomaly_ids if anomaly_id is not None}
    if not wanted:
        return {}
    rows = await session.execute(
        select(MetricAnomalyAttribution).where(MetricAnomalyAttribution.anomaly_id.in_(wanted))
    )
    return {row.anomaly_id: row for row in rows.scalars()}


async def scans_with_breakdown_columns(
    session: AsyncSession, scan_config_ids: Iterable[uuid.UUID | None]
) -> set[uuid.UUID]:
    """The scans that have a breakdown column an attribution can split by.

    Same rule as the worker (``worker.tasks.metrics.attribution
    .scan_breakdown_columns``): scan-level ``metric_breakdown_columns`` and
    ``platform_column``, or any of the scan's events' own
    ``metric_breakdown_columns`` — never counting the app-version column.
    """
    wanted = {scan_id for scan_id in scan_config_ids if scan_id is not None}
    if not wanted:
        return set()
    configs = (
        await session.execute(
            select(
                ScanConfig.id,
                ScanConfig.project_id,
                ScanConfig.metric_breakdown_columns,
                ScanConfig.platform_column,
                ScanConfig.app_version_column,
            ).where(ScanConfig.id.in_(wanted))
        )
    ).all()
    app_version_by_scan: dict[uuid.UUID, str | None] = {}
    project_by_scan: dict[uuid.UUID, uuid.UUID] = {}
    result: set[uuid.UUID] = set()
    for scan_id, project_id, columns, platform_column, app_version_column in configs:
        app_version_by_scan[scan_id] = app_version_column
        project_by_scan[scan_id] = project_id
        named = {column for column in (columns or []) if column}
        if platform_column:
            named.add(platform_column)
        named.discard(app_version_column or "")
        if named:
            result.add(scan_id)
    pending = set(app_version_by_scan) - result
    if not pending:
        return result
    # The project's events, not a walk over event_metrics (unbounded): an
    # event-level breakdown column anywhere in the project counts, the same
    # rule the worker's scan_breakdown_columns applies.
    rows = await session.execute(
        select(Event.project_id, Event.metric_breakdown_columns).where(
            Event.project_id.in_({project_by_scan[scan_id] for scan_id in pending}),
            has_breakdown_columns(),
        )
    )
    columns_by_project: dict[uuid.UUID, set[str]] = {}
    for project_id, event_columns in rows.all():
        columns_by_project.setdefault(project_id, set()).update(
            column for column in (event_columns or []) if column
        )
    for scan_id in pending:
        app_version = app_version_by_scan.get(scan_id) or ""
        if columns_by_project.get(project_by_scan[scan_id], set()) - {app_version}:
            result.add(scan_id)
    return result


def _status(
    signal: MetricSignalResponse,
    rows: Mapping[uuid.UUID, MetricAnomalyAttribution],
    with_columns: set[uuid.UUID],
) -> AttributionStatus:
    if signal.anomaly_id is not None and signal.anomaly_id in rows:
        return "ready"
    if (
        signal.scope_type in ATTRIBUTED_SCOPES
        and signal.scan_config_id is not None
        and signal.scan_config_id not in with_columns
    ):
        return "no_breakdown_columns"
    return "not_computed"


async def attach_attributions(
    session: AsyncSession, signals: list[MetricSignalResponse]
) -> list[MetricSignalResponse]:
    """Each signal with its stored attribution and ``attribution_status``.

    Two queries for the whole list. Applied after the signals cache, like the
    triage fields, so a re-scored anomaly never serves a stale split.
    """
    if not signals:
        return signals
    rows = await load_attribution_rows(session, (signal.anomaly_id for signal in signals))
    with_columns = await scans_with_breakdown_columns(
        session,
        (signal.scan_config_id for signal in signals if signal.scope_type in ATTRIBUTED_SCOPES),
    )
    result: list[MetricSignalResponse] = []
    for signal in signals:
        row = rows.get(signal.anomaly_id) if signal.anomaly_id is not None else None
        result.append(
            signal.model_copy(
                update={
                    "attribution": (
                        attribution_from_row(row, bucket=signal.bucket, direction=signal.direction)
                        if row is not None
                        else None
                    ),
                    "attribution_status": _status(signal, rows, with_columns),
                }
            )
        )
    return result


async def with_latest_signal_attribution(
    session: AsyncSession, response: EventMetricsResponse
) -> EventMetricsResponse:
    """The drilldown response with its ``latest_signal`` attribution filled."""
    if response.latest_signal is None:
        return response
    (latest,) = await attach_attributions(session, [response.latest_signal])
    return response.model_copy(update={"latest_signal": latest})


async def _anomaly_in_project(
    session: AsyncSession, project_id: uuid.UUID, anomaly_id: uuid.UUID
) -> MetricAnomaly | None:
    anomaly = await session.get(MetricAnomaly, anomaly_id)
    if anomaly is None:
        return None
    if anomaly.scan_config_id is not None:
        owner = await session.scalar(
            select(ScanConfig.project_id).where(ScanConfig.id == anomaly.scan_config_id)
        )
        return anomaly if owner == project_id else None
    if anomaly.scope_type != SCOPE_METRIC:
        return None
    try:
        metric_id = uuid.UUID(anomaly.scope_ref)
    except ValueError:
        return None
    owner = await session.scalar(
        select(MetricDefinition.project_id).where(MetricDefinition.id == metric_id)
    )
    return anomaly if owner == project_id else None


async def get_anomaly_attribution(
    session: AsyncSession, slug: str, anomaly_id: uuid.UUID
) -> AnomalyAttributionResponse:
    """One anomaly's stored attribution; 404 when it is not this project's."""
    project = await get_project_by_slug(session, slug)
    anomaly = await _anomaly_in_project(session, project.id, anomaly_id)
    if anomaly is None:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    rows = await load_attribution_rows(session, [anomaly.id])
    row = rows.get(anomaly.id)
    with_columns = await scans_with_breakdown_columns(session, [anomaly.scan_config_id])
    status: AttributionStatus
    if row is not None:
        status = "ready"
    elif (
        anomaly.scope_type in ATTRIBUTED_SCOPES
        and anomaly.scan_config_id is not None
        and anomaly.scan_config_id not in with_columns
    ):
        status = "no_breakdown_columns"
    else:
        status = "not_computed"
    return AnomalyAttributionResponse(
        anomaly_id=anomaly.id,
        scan_config_id=anomaly.scan_config_id,
        attribution_status=status,
        attribution=(
            attribution_from_row(row, bucket=anomaly.bucket, direction=anomaly.direction)
            if row is not None
            else None
        ),
    )
