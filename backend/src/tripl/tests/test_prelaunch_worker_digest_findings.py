"""The scheduled messages count what the product's pages count.

Two pre-launch fixes to the outbound plan messages, each a second definition of
something the rest of the product already defined once:

* "Deprecated events still receiving data" (the weekly digest line and the
  daily sunset alert) read the sunset watch's open ``sunset_overdue`` findings
  instead of testing ``last_seen_at > sunset_at`` themselves. ``last_seen_at``
  only moves forward, so that test could never stop holding: a team that
  stopped sending a deprecated event, as asked, kept getting the alert every
  morning while the catalog chip, the health score and the Lifecycle alerts
  said it was fixed.
* "Dead implemented events" in the weekly digest uses ``core.dead_events``, the
  rule Govern -> Reconciliation -> Dead events lists by, so a never-seen event
  gets the same grace on both.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

import tripl.worker.celery_app  # noqa: F401
from tripl.core.dead_events import DEAD_EVENT_DAYS, dead_event_clause
from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event, EventStatus
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.lifecycle_finding import LifecycleFinding, LifecycleFindingKind
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services import health_weights
from tripl.tests._sunset_findings import open_sunset_finding
from tripl.worker.tasks import lifecycle as lifecycle_tasks
from tripl.worker.tasks.alerts_messages import (
    _build_plan_digest_message,
    _build_sunset_alert_message,
)

_NOW = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'prelaunch_digest.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory() as db:
            yield db
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_project(session: Session) -> tuple[Project, EventType, ScanConfig]:
    project = Project(
        id=uuid.uuid4(), name="Checkout", slug=f"checkout-{uuid.uuid4().hex[:8]}", description=""
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    event_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="track", display_name="Track", description=""
    )
    session.add_all([project, data_source, event_type])
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        event_type_id=event_type.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        interval="1h",
    )
    session.add(config)
    session.commit()
    return project, event_type, config


def _add_event(
    session: Session,
    project: Project,
    event_type: EventType,
    name: str,
    *,
    status: EventStatus,
    **extra: Any,
) -> Event:
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name=name,
        description="",
        status=status.value,
        **extra,
    )
    session.add(event)
    session.commit()
    return event


def _digest_count(message: str, label: str) -> int:
    match = re.search(rf"^- {re.escape(label)}: (\d+)$", message, re.MULTILINE)
    assert match is not None, f"the digest no longer carries its {label!r} line:\n{message}"
    return int(match.group(1))


def _sunset_digest_count(session: Session, project: Project, now: datetime) -> int:
    message = _build_plan_digest_message(session, project=project, now=now)
    return _digest_count(message, "Deprecated events still receiving data")


# --------------------------------------------------------------------------- #
# Deprecated events still receiving data
# --------------------------------------------------------------------------- #


def test_the_sunset_alert_stops_once_the_data_stops(session: Session) -> None:
    """Against the real sweep: the documented way out of the alert works.

    The event keeps ``last_seen_at`` past its sunset after the data stops, which
    is what the old predicate read, so reverting either builder to it keeps the
    second half naming the event.
    """
    project, event_type, config = _seed_project(session)
    old = _add_event(
        session,
        project,
        event_type,
        "old_checkout",
        status=EventStatus.deprecated,
        sunset_at=_NOW - timedelta(days=30),
        last_seen_at=_NOW - timedelta(hours=2),
    )
    session.add(
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=old.id,
            event_type_id=None,
            bucket=_NOW - timedelta(hours=2),
            count=1240,
        )
    )
    session.commit()

    lifecycle_tasks.compute_lifecycle_findings(session, now=_NOW)
    session.commit()

    alert = _build_sunset_alert_message(session, project=project, now=_NOW)
    assert alert is not None
    assert "Count: 1" in alert.splitlines()
    assert "- old_checkout (sunset 2026-08-28, 1,240 events in the last 24 hours)" in alert
    assert _sunset_digest_count(session, project, _NOW) == 1

    # Two days on with no new buckets: the sweep resolves the finding, and both
    # messages follow it.
    quiet = _NOW + timedelta(days=2)
    lifecycle_tasks.compute_lifecycle_findings(session, now=quiet)
    session.commit()

    session.expire_all()
    still_past_sunset = session.get(Event, old.id)
    assert still_past_sunset is not None
    assert still_past_sunset.last_seen_at is not None
    assert still_past_sunset.sunset_at is not None
    assert still_past_sunset.last_seen_at > still_past_sunset.sunset_at
    assert _build_sunset_alert_message(session, project=project, now=quiet) is None
    assert _sunset_digest_count(session, project, quiet) == 0


def test_a_resolved_finding_names_nothing(session: Session) -> None:
    """``last_seen_at > sunset_at`` alone is not "still receiving data"."""
    project, event_type, _config = _seed_project(session)
    event = _add_event(
        session,
        project,
        event_type,
        "old_signup",
        status=EventStatus.deprecated,
        sunset_at=_NOW - timedelta(days=60),
        last_seen_at=_NOW - timedelta(days=10),
    )
    finding = open_sunset_finding(event, at=_NOW - timedelta(days=12))
    finding.resolved_at = _NOW - timedelta(days=9)
    session.add(finding)
    session.commit()

    assert _build_sunset_alert_message(session, project=project, now=_NOW) is None
    assert _sunset_digest_count(session, project, _NOW) == 0


@pytest.mark.parametrize(
    "plan_edit",
    [
        {"status": EventStatus.archived.value},
        {"sunset_at": _NOW + timedelta(days=14)},
        {"sunset_at": None},
    ],
    ids=["retired", "sunset-moved-out", "sunset-cleared"],
)
def test_a_finding_the_plan_has_moved_past_is_not_named(
    session: Session, plan_edit: dict[str, object]
) -> None:
    """A plan edit after the morning sweep takes effect before the next one."""
    project, event_type, _config = _seed_project(session)
    event = _add_event(
        session,
        project,
        event_type,
        "old_cart",
        status=EventStatus.deprecated,
        sunset_at=_NOW - timedelta(days=3),
        last_seen_at=_NOW - timedelta(hours=1),
    )
    session.add(open_sunset_finding(event, at=_NOW))
    session.commit()
    assert _build_sunset_alert_message(session, project=project, now=_NOW) is not None

    session.execute(update(Event).where(Event.id == event.id).values(**plan_edit))
    session.commit()

    assert _build_sunset_alert_message(session, project=project, now=_NOW) is None
    assert _sunset_digest_count(session, project, _NOW) == 0


def test_only_sunset_overdue_findings_count(session: Session) -> None:
    """A silent successor is a lifecycle finding too, but not "still receiving data"."""
    project, event_type, _config = _seed_project(session)
    event = _add_event(
        session,
        project,
        event_type,
        "old_search",
        status=EventStatus.deprecated,
        sunset_at=_NOW - timedelta(days=3),
    )
    session.add(
        LifecycleFinding(
            id=uuid.uuid4(),
            project_id=project.id,
            event_id=event.id,
            kind=LifecycleFindingKind.successor_silent.value,
            first_seen_at=_NOW,
            last_seen_at=_NOW,
            resolved_at=None,
            successor_volume_7d=0,
        )
    )
    session.commit()

    assert _build_sunset_alert_message(session, project=project, now=_NOW) is None
    assert _sunset_digest_count(session, project, _NOW) == 0


# --------------------------------------------------------------------------- #
# Dead implemented events
# --------------------------------------------------------------------------- #


def test_dead_event_clause_gives_only_never_seen_events_a_grace(session: Session) -> None:
    project, event_type, _config = _seed_project(session)
    cutoff = _NOW - timedelta(days=DEAD_EVENT_DAYS)
    old_row = _NOW - timedelta(days=DEAD_EVENT_DAYS + 10)
    young_row = _NOW - timedelta(days=1)
    cases = {
        # Authored long ago and never arrived: dead.
        "never_seen_old": (EventStatus.implemented, None, old_row),
        # Authored yesterday: not dead yet, it may simply not have shipped.
        "never_seen_new": (EventStatus.implemented, None, young_row),
        # Seen, then quiet past the cutoff: dead however young its plan row is.
        "quiet_live": (EventStatus.live, _NOW - timedelta(days=DEAD_EVENT_DAYS + 1), young_row),
        "recent_live": (EventStatus.live, _NOW - timedelta(days=1), old_row),
        # Not something the plan says should be sending.
        "old_draft": (EventStatus.draft, None, old_row),
        "old_deprecated": (EventStatus.deprecated, None, old_row),
    }
    for name, (status, seen, created) in cases.items():
        _add_event(
            session,
            project,
            event_type,
            name,
            status=status,
            last_seen_at=seen,
            created_at=created,
        )

    dead = set(
        session.execute(
            select(Event.name).where(Event.project_id == project.id, dead_event_clause(cutoff))
        ).scalars()
    )

    assert dead == {"never_seen_old", "quiet_live"}
    digest = _build_plan_digest_message(session, project=project, now=_NOW)
    assert _digest_count(digest, "Dead implemented events") == len(dead)


def test_the_health_scores_stale_window_is_the_dead_event_window() -> None:
    """``health_weights`` documents its stale window as this one; keep them equal."""
    assert health_weights.SEEN_STALE_DAYS == DEAD_EVENT_DAYS
