"""Recurring planned windows suggested from ``expected`` verdicts (F18 × F01, #271).

When people keep answering signals on one series at the same hour of the same
weekday with *expected* — a weekly newsletter, a Monday batch import — the
move is a schedule, not news. This finds those runs and proposes the next few
occurrences as planned events, so the next one is not raised either.

A suggestion is a series (the verdict's ``scope_type`` / ``scope_ref``), a UTC
weekday and hour, and :data:`NEXT_WINDOWS` one-hour windows starting at the
next occurrences. It needs :data:`MIN_WEEKS` expected verdicts in distinct weeks
within the last :data:`LOOKBACK`. It is dropped once a planned event (for the
series or project-wide, in a compatible direction) covers the next
occurrence, which is what accepting it does: the client creates the windows
through the ordinary planned-event API.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.bucketing import to_utc
from tripl.models.domain_enums import ChartAnnotationScopeType, SignalTriageAction
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.planned_event import PlannedEvent
from tripl.models.signal_triage import SignalTriage

LOOKBACK = timedelta(weeks=8)
MIN_WEEKS = 3
NEXT_WINDOWS = 4
WINDOW = timedelta(hours=1)

# Scopes a planned event can carry; a verdict on another kind of signal (schema
# drift, a release regression) has no series to plan a window on.
_PLANNABLE = frozenset(scope.value for scope in ChartAnnotationScopeType)


@dataclass(frozen=True)
class SuggestedWindow:
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class PlannedWindowSuggestion:
    scope_type: str
    scope_ref: str
    # 0 = Monday, as ``datetime.weekday``; UTC.
    weekday: int
    hour: int
    # The one direction every matched anomaly moved in, else None (either).
    direction: str | None
    verdict_count: int
    last_bucket: datetime
    # The newest verdict's note, the likeliest name for the window.
    note: str | None
    windows: tuple[SuggestedWindow, ...]


@dataclass
class _Run:
    buckets: list[datetime] = field(default_factory=list)
    directions: set[str | None] = field(default_factory=set)
    note: str | None = None
    note_at: datetime | None = None


def _week(bucket: datetime) -> tuple[int, int]:
    year, week, _ = bucket.isocalendar()
    return year, week


def _next_occurrences(weekday: int, hour: int, now: datetime) -> tuple[SuggestedWindow, ...]:
    """The next :data:`NEXT_WINDOWS` slots at ``weekday`` ``hour`` UTC after ``now``."""
    day = now.astimezone(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    first = day + timedelta(days=(weekday - day.weekday()) % 7)
    if first <= now:
        first += timedelta(weeks=1)
    return tuple(
        SuggestedWindow(first + timedelta(weeks=n), first + timedelta(weeks=n) + WINDOW)
        for n in range(NEXT_WINDOWS)
    )


def _covered(
    planned: list[PlannedEvent],
    scope_type: str,
    scope_ref: str,
    direction: str | None,
    window: SuggestedWindow,
) -> bool:
    for event in planned:
        if event.scope_type is not None and (
            str(event.scope_type) != scope_type or event.scope_ref != scope_ref
        ):
            continue
        if event.direction is not None and str(event.direction) != direction:
            continue
        if to_utc(event.starts_at) <= window.starts_at < to_utc(event.ends_at):
            return True
    return False


async def suggest_recurring_windows(
    session: AsyncSession, project_id: uuid.UUID, *, now: datetime | None = None
) -> list[PlannedWindowSuggestion]:
    """The project's recurring-window suggestions, most verdicts first."""
    now = now or datetime.now(UTC)
    verdicts = (
        await session.execute(
            select(
                SignalTriage.scope_type,
                SignalTriage.scope_ref,
                SignalTriage.bucket,
                SignalTriage.note,
                MetricAnomaly.direction,
            )
            .outerjoin(
                MetricAnomaly,
                and_(
                    MetricAnomaly.scope_type == SignalTriage.scope_type,
                    MetricAnomaly.scope_ref == SignalTriage.scope_ref,
                    MetricAnomaly.bucket == SignalTriage.bucket,
                    or_(
                        MetricAnomaly.scan_config_id == SignalTriage.scan_config_id,
                        and_(
                            MetricAnomaly.scan_config_id.is_(None),
                            SignalTriage.scan_config_id.is_(None),
                        ),
                    ),
                ),
            )
            .where(
                SignalTriage.project_id == project_id,
                SignalTriage.action == SignalTriageAction.expected.value,
                SignalTriage.bucket >= now - LOOKBACK,
            )
        )
    ).all()

    runs: dict[tuple[str, str, int, int], _Run] = defaultdict(_Run)
    for scope_type, scope_ref, raw_bucket, note, raw_direction in verdicts:
        if raw_bucket is None or str(scope_type) not in _PLANNABLE:
            continue
        bucket = to_utc(raw_bucket)
        run = runs[(str(scope_type), scope_ref, bucket.weekday(), bucket.hour)]
        run.buckets.append(bucket)
        run.directions.add(str(raw_direction) if raw_direction is not None else None)
        if note and (run.note_at is None or bucket > run.note_at):
            run.note, run.note_at = note, bucket

    candidates = {
        key: run for key, run in runs.items() if len({_week(b) for b in run.buckets}) >= MIN_WEEKS
    }
    if not candidates:
        return []
    planned = list(
        await session.scalars(
            select(PlannedEvent).where(
                PlannedEvent.project_id == project_id, PlannedEvent.ends_at > now
            )
        )
    )

    suggestions: list[PlannedWindowSuggestion] = []
    for (scope_type, scope_ref, weekday, hour), run in candidates.items():
        known = run.directions - {None}
        direction = next(iter(known)) if len(known) == 1 and None not in run.directions else None
        windows = _next_occurrences(weekday, hour, now)
        if _covered(planned, scope_type, scope_ref, direction, windows[0]):
            continue
        suggestions.append(
            PlannedWindowSuggestion(
                scope_type=scope_type,
                scope_ref=scope_ref,
                weekday=weekday,
                hour=hour,
                direction=direction,
                verdict_count=len(run.buckets),
                last_bucket=max(run.buckets),
                note=run.note,
                windows=windows,
            )
        )
    suggestions.sort(key=lambda s: (-s.verdict_count, -s.last_bucket.timestamp()))
    return suggestions
