"""Lifecycle findings and successor adoption, read side (#258).

The rows are written by the daily sunset watch (``worker.tasks.lifecycle``);
everything here only reads them. Findings hang on MAIN-branch events (scans only
see main), so a branch copy reads its main twin's findings the way it reads the
twin's ``last_seen_at`` (``_branch_counterparts``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.lifecycle_finding import LifecycleFinding
from tripl.models.scan_config import ScanConfig
from tripl.schemas.lifecycle import (
    EventMigrationResponse,
    EventMigrationSide,
    LifecycleFindingResponse,
)
from tripl.services._branch_counterparts import main_counterparts, metrics_row_for
from tripl.services.lifecycle_rules import (
    ADOPTION_WINDOW,
    adoption_ratio,
    daily_average,
    lookback_start,
    prorated_window_volume,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id

_RelatedEvent = aliased(Event)


def _finding_query() -> Select[LifecycleFinding, str, str]:
    # The related name is an OUTER join, so it reads None when there is none.
    return (
        select(LifecycleFinding, Event.name, _RelatedEvent.name)
        .join(Event, Event.id == LifecycleFinding.event_id)
        .outerjoin(_RelatedEvent, _RelatedEvent.id == LifecycleFinding.related_event_id)
    )


def _to_response(
    finding: LifecycleFinding, event_name: str, related_name: str | None
) -> LifecycleFindingResponse:
    return LifecycleFindingResponse(
        id=finding.id,
        event_id=finding.event_id,
        event_name=event_name,
        kind=finding.kind,
        related_event_id=finding.related_event_id,
        related_event_name=related_name,
        first_seen_at=finding.first_seen_at,
        last_seen_at=finding.last_seen_at,
        resolved_at=finding.resolved_at,
        volume_24h=finding.volume_24h,
        successor_volume_7d=finding.successor_volume_7d,
    )


async def list_findings(
    session: AsyncSession, slug: str, *, include_resolved: bool = False
) -> list[LifecycleFindingResponse]:
    """A project's findings: open ones first by default, newest episode first."""
    project_id = await resolve_project_id(session, slug)
    stmt = _finding_query().where(LifecycleFinding.project_id == project_id)
    if not include_resolved:
        stmt = stmt.where(LifecycleFinding.resolved_at.is_(None))
    rows = (
        await session.execute(
            stmt.order_by(
                LifecycleFinding.resolved_at.is_not(None),
                LifecycleFinding.first_seen_at.desc(),
                LifecycleFinding.id.asc(),
            )
        )
    ).all()
    return [_to_response(finding, name, related) for finding, name, related in rows]


async def _metrics_ids(
    session: AsyncSession, *, project_id: uuid.UUID, events: Sequence[Event]
) -> dict[uuid.UUID, uuid.UUID]:
    """Row id → the id findings are keyed on (its main twin for a branch copy)."""
    twins = await main_counterparts(session, project_id=project_id, events=events)
    return {ev.id: twins[ev.id].id if ev.id in twins else ev.id for ev in events}


async def attach_event_findings(
    session: AsyncSession, *, project_id: uuid.UUID, event: Event
) -> None:
    """Set ``event.lifecycle_findings``: open findings on the event or naming it as successor."""
    keyed = (await _metrics_ids(session, project_id=project_id, events=[event]))[event.id]
    rows = (
        await session.execute(
            _finding_query()
            .where(
                LifecycleFinding.project_id == project_id,
                LifecycleFinding.resolved_at.is_(None),
                or_(
                    LifecycleFinding.event_id == keyed,
                    LifecycleFinding.related_event_id == keyed,
                ),
            )
            .order_by(LifecycleFinding.first_seen_at.asc(), LifecycleFinding.id.asc())
        )
    ).all()
    event.lifecycle_findings = [  # type: ignore[attr-defined]
        _to_response(finding, name, related) for finding, name, related in rows
    ]


async def attach_list_warnings(
    session: AsyncSession, *, project_id: uuid.UUID, events: Sequence[Event]
) -> None:
    """Set ``event.lifecycle_warning`` on each catalog row: an open finding hangs on it.

    Only findings ON the event light the chip — a successor is not the one
    that needs a warning in the catalog. One query for the page, plus the twin
    lookup on a branch read.
    """
    if not events:
        return
    keyed = await _metrics_ids(session, project_id=project_id, events=events)
    flagged = set(
        (
            await session.execute(
                select(LifecycleFinding.event_id).where(
                    LifecycleFinding.project_id == project_id,
                    LifecycleFinding.resolved_at.is_(None),
                    LifecycleFinding.event_id.in_(set(keyed.values())),
                )
            )
        )
        .scalars()
        .all()
    )
    for ev in events:
        ev.lifecycle_warning = keyed[ev.id] in flagged  # type: ignore[attr-defined]


async def _event_in_project(
    session: AsyncSession, *, project_id: uuid.UUID, event_id: uuid.UUID, branch_id: uuid.UUID
) -> Event:
    """The event on the requested branch, else on any branch of the project (as ``get_event``)."""
    event = (
        await session.execute(
            select(Event).where(
                Event.id == event_id,
                Event.project_id == project_id,
                Event.branch_id == branch_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        event = (
            await session.execute(
                select(Event).where(Event.id == event_id, Event.project_id == project_id)
            )
        ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


async def event_migration(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
    *,
    now: datetime | None = None,
) -> EventMigrationResponse:
    """Old vs new daily volume for an event that names a successor.

    404 when the event does not exist in the project or names no successor.
    Volume is read off the main twins, where metrics land.
    """
    project_id = await resolve_project_id(session, slug)
    resolved_branch = await resolve_branch_id(session, project_id, branch_id)
    event = await _event_in_project(
        session, project_id=project_id, event_id=event_id, branch_id=resolved_branch
    )
    if event.superseded_by_event_id is None:
        raise HTTPException(status_code=404, detail="Event has no successor")
    successor = (
        await session.execute(
            select(Event).where(
                Event.id == event.superseded_by_event_id, Event.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if successor is None:
        raise HTTPException(status_code=404, detail="Event has no successor")

    old_row = await metrics_row_for(session, project_id=project_id, event=event)
    new_row = await metrics_row_for(session, project_id=project_id, event=successor)
    moment = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(EventMetric.event_id, EventMetric.bucket, EventMetric.count, ScanConfig.interval)
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                EventMetric.event_id.in_({old_row.id, new_row.id}),
                EventMetric.bucket >= lookback_start(moment, ADOPTION_WINDOW),
                EventMetric.count > 0,
            )
        )
    ).all()
    totals = prorated_window_volume(
        (
            (row_event_id, bucket, int(count), None if interval is None else str(interval))
            for row_event_id, bucket, count, interval in rows
            if row_event_id is not None
        ),
        now=moment,
        window=ADOPTION_WINDOW,
    )
    old_daily = daily_average(totals.get(old_row.id, 0))
    new_daily = daily_average(totals.get(new_row.id, 0))
    return EventMigrationResponse(
        old=EventMigrationSide(event_id=event.id, name=event.name, daily_avg_7d=old_daily),
        new=EventMigrationSide(event_id=successor.id, name=successor.name, daily_avg_7d=new_daily),
        ratio=adoption_ratio(old_daily, new_daily),
    )
