"""Planned events (F18, #271): windows in which a project expects its numbers to move.

A campaign, a sale or a holiday moves the series on purpose, and an alert about
it is noise. A planned event names the window, optionally the direction it
expects and the one series it is about; every stored anomaly that falls inside
is tagged with it (``MetricAnomaly.planned_event_id``). A tagged anomaly is
still detected, stored and drawn — the chart shows it muted, inside the
event's band — but no alert, notification, open signal or badge counts it.

Tagging is recomputed for the whole project by :func:`retag_planned_anomalies`
whenever either side changes: after detection writes its rows (the worker) and
after a planned event is created, edited or deleted (the API). It is a pure
function of the two tables, so running it twice changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.models.domain_enums import MetricScopeType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.planned_event import PlannedEvent
from tripl.models.scan_config import ScanConfig
from tripl.services.project_lookup import resolve_project_id

NOT_FOUND_DETAIL = "Planned event not found"


def retag_planned_anomalies(session: Session, project_id: uuid.UUID) -> int:
    """Tag the project's anomalies with the planned event that expected them.

    Clears every tag the project's events hold, then walks the events oldest
    window first and tags each untagged anomaly whose bucket is in
    ``[starts_at, ends_at)``, whose direction matches (a NULL direction expects
    either) and whose series matches (a NULL scope covers the project). Where
    two events overlap, the earlier one keeps the anomaly. Returns how many rows
    are tagged; commits nothing.

    The project's anomalies are its scans' rows plus its catalog metrics' rows,
    which carry no scan config and are keyed by ``scope_ref = str(metric id)``.
    The metric ids are read here rather than cast in SQL, because the stored
    text of a UUID differs between PostgreSQL and SQLite.
    """
    project_events = select(PlannedEvent.id).where(PlannedEvent.project_id == project_id)
    session.execute(
        update(MetricAnomaly)
        .where(MetricAnomaly.planned_event_id.in_(project_events))
        .values(planned_event_id=None)
        .execution_options(synchronize_session=False)
    )
    events = list(
        session.scalars(
            select(PlannedEvent)
            .where(PlannedEvent.project_id == project_id)
            .order_by(PlannedEvent.starts_at, PlannedEvent.created_at, PlannedEvent.id)
        )
    )
    if not events:
        return 0

    metric_refs = [
        str(metric_id)
        for metric_id in session.scalars(
            select(MetricDefinition.id).where(MetricDefinition.project_id == project_id)
        )
    ]
    in_project = or_(
        MetricAnomaly.scan_config_id.in_(
            select(ScanConfig.id).where(ScanConfig.project_id == project_id)
        ),
        and_(
            MetricAnomaly.scan_config_id.is_(None),
            MetricAnomaly.scope_type == MetricScopeType.metric.value,
            MetricAnomaly.scope_ref.in_(metric_refs),
        ),
    )
    tagged = 0
    for event in events:
        conditions = [
            MetricAnomaly.planned_event_id.is_(None),
            MetricAnomaly.bucket >= event.starts_at,
            MetricAnomaly.bucket < event.ends_at,
            in_project,
        ]
        if event.direction is not None:
            conditions.append(MetricAnomaly.direction == event.direction)
        if event.scope_type is not None:
            conditions.append(MetricAnomaly.scope_type == event.scope_type)
            conditions.append(MetricAnomaly.scope_ref == event.scope_ref)
        result = session.execute(
            update(MetricAnomaly)
            .where(*conditions)
            .values(planned_event_id=event.id)
            .execution_options(synchronize_session=False)
        )
        tagged += result.rowcount or 0  # type: ignore[attr-defined]
    return tagged


async def _retag(session: AsyncSession, project_id: uuid.UUID) -> None:
    await session.run_sync(retag_planned_anomalies, project_id)


async def list_planned_events(
    session: AsyncSession,
    slug: str,
    *,
    scope_type: str | None = None,
    scope_ref: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
) -> list[PlannedEvent]:
    """Planned events whose window touches ``[time_from, time_to]``, oldest first.

    Project-wide events (scope NULL) are always included; scoped ones only for
    the given (scope_type, scope_ref) pair when it is passed.
    """
    project_id = await resolve_project_id(session, slug)
    conditions = [PlannedEvent.project_id == project_id]
    if scope_type is not None and scope_ref is not None:
        conditions.append(
            or_(
                PlannedEvent.scope_type.is_(None),
                and_(PlannedEvent.scope_type == scope_type, PlannedEvent.scope_ref == scope_ref),
            )
        )
    if time_from is not None:
        conditions.append(PlannedEvent.ends_at > time_from)
    if time_to is not None:
        conditions.append(PlannedEvent.starts_at <= time_to)
    rows = await session.execute(
        select(PlannedEvent).where(*conditions).order_by(PlannedEvent.starts_at.asc())
    )
    return list(rows.scalars().all())


async def create_planned_event(
    session: AsyncSession,
    slug: str,
    *,
    label: str,
    description: str | None,
    starts_at: datetime,
    ends_at: datetime,
    direction: str | None,
    scope_type: str | None,
    scope_ref: str | None,
    user_id: uuid.UUID | None,
) -> PlannedEvent:
    project_id = await resolve_project_id(session, slug)
    event = PlannedEvent(
        project_id=project_id,
        label=label.strip(),
        description=description.strip() or None if description else None,
        starts_at=starts_at,
        ends_at=ends_at,
        direction=direction,
        scope_type=scope_type,
        scope_ref=scope_ref,
        created_by_user_id=user_id,
    )
    session.add(event)
    await session.flush()
    await _retag(session, project_id)
    await session.commit()
    await session.refresh(event)
    return event


async def get_planned_event(session: AsyncSession, slug: str, event_id: uuid.UUID) -> PlannedEvent:
    project_id = await resolve_project_id(session, slug)
    event = await session.get(PlannedEvent, event_id)
    if event is None or event.project_id != project_id:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    return event


async def update_planned_event(
    session: AsyncSession, event: PlannedEvent, changes: Mapping[str, object]
) -> PlannedEvent:
    """Apply already-validated ``changes`` to ``event`` and retag its project."""
    for field, value in changes.items():
        if field == "label" and isinstance(value, str):
            value = value.strip()
        elif field == "description" and isinstance(value, str):
            value = value.strip() or None
        setattr(event, field, value)
    await session.flush()
    await _retag(session, event.project_id)
    await session.commit()
    await session.refresh(event)
    return event


async def delete_planned_event(session: AsyncSession, event: PlannedEvent) -> None:
    project_id = event.project_id
    await session.delete(event)
    await session.flush()
    # ``ON DELETE SET NULL`` already untagged its anomalies; the retag lets an
    # overlapping event pick them up.
    await _retag(session, project_id)
    await session.commit()
