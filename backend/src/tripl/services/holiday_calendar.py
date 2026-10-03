"""A project's holiday calendar: public holidays as planned events (F18, #271).

A project names a country (``ProjectAnomalySettings.holiday_country``) and its
public holidays become project-wide planned events that expect a move either
way: a holiday's dip or surge is drawn but not alerted on. The holidays come
from the ``holidays`` package. Each one is a UTC calendar day — the warehouse's
buckets are UTC, and a project carries no time zone of its own.

:func:`sync_project_holidays` keeps the ``source = 'holiday'`` rows in step
with the country for last year, this year and next year, then retags the
project's anomalies. It runs when the country changes and nightly, so the
calendar rolls into a new year by itself. It is idempotent: a run that finds
the rows already right writes nothing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from functools import cache

import holidays
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tripl.models.planned_event import PLANNED_EVENT_SOURCE_HOLIDAY, PlannedEvent
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.services.planned_event_service import retag_planned_anomalies


@cache
def supported_countries() -> tuple[str, ...]:
    """Every ISO 3166-1 alpha-2 code the ``holidays`` package has a calendar for."""
    return tuple(sorted(code for code in holidays.list_supported_countries() if len(code) == 2))


def is_supported_country(code: str) -> bool:
    return code in supported_countries()


def holidays_for(country: str, years: range) -> dict[date, str]:
    """``{day: name}`` for the country's public holidays in ``years``.

    Named in English wherever the package has an English calendar for the
    country: left to itself it picks the country's own language (or the
    process locale's), so the same project read "Tag der Deutschen Einheit" on
    one host and "German Unity Day" on another. The package already joins two
    holidays falling on one day into one name, so a day is one planned event.
    """
    calendar = holidays.country_holidays(country, years=list(years), language="en_US")
    return dict(sorted(calendar.items()))


def _years_around(today: date) -> range:
    return range(today.year - 1, today.year + 2)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _description(country: str | None) -> str:
    return f"Public holiday ({country}), from the project's holiday calendar."


def sync_project_holidays(
    session: Session, project_id: uuid.UUID, *, today: date | None = None
) -> bool:
    """Bring the project's holiday rows in line with its country; True when they changed.

    Flushes, does not commit. Retags the project's anomalies when anything
    changed, since a holiday window added or removed moves what is expected.
    """
    country = session.scalar(
        select(ProjectAnomalySettings.holiday_country).where(
            ProjectAnomalySettings.project_id == project_id
        )
    )
    wanted: dict[datetime, str] = {}
    if country and is_supported_country(country):
        years = _years_around(today or datetime.now(UTC).date())
        for day, name in holidays_for(country, years).items():
            wanted[_day_start(day)] = name[:200]

    existing = list(
        session.scalars(
            select(PlannedEvent).where(
                PlannedEvent.project_id == project_id,
                PlannedEvent.source == PLANNED_EVENT_SOURCE_HOLIDAY,
            )
        )
    )
    description = _description(country)
    stale = [
        row.id
        for row in existing
        if wanted.get(row.starts_at) != row.label or row.description != description
    ]
    kept = {row.starts_at for row in existing if row.id not in stale}
    missing = {start: name for start, name in wanted.items() if start not in kept}
    if not stale and not missing:
        return False

    if stale:
        session.execute(
            delete(PlannedEvent)
            .where(PlannedEvent.id.in_(stale))
            .execution_options(synchronize_session=False)
        )
    for start, name in sorted(missing.items()):
        session.add(
            PlannedEvent(
                project_id=project_id,
                label=name,
                description=description,
                starts_at=start,
                ends_at=start + timedelta(days=1),
                direction=None,
                scope_type=None,
                scope_ref=None,
                source=PLANNED_EVENT_SOURCE_HOLIDAY,
            )
        )
    session.flush()
    retag_planned_anomalies(session, project_id)
    return True


def projects_with_a_calendar(session: Session) -> list[uuid.UUID]:
    """Projects the nightly run refreshes: a country set, or holiday rows left over."""
    with_country = select(ProjectAnomalySettings.project_id).where(
        ProjectAnomalySettings.holiday_country.is_not(None)
    )
    with_rows = select(PlannedEvent.project_id).where(
        PlannedEvent.source == PLANNED_EVENT_SOURCE_HOLIDAY
    )
    return sorted(set(session.scalars(with_country)) | set(session.scalars(with_rows)))
