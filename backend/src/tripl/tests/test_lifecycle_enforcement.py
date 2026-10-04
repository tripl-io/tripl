"""Lifecycle enforcement (#258): seen in data, sunset watch, successor adoption.

Covers the backend data side:

* the pure window arithmetic (``services.lifecycle_rules``) — a daily bucket
  that started before the 24h window still counts, traffic before the sunset
  does not, the 7-day average prorates a bucket straddling the window's start,
  and the migration ratio is ``new / old``;
* auto-live in ``metrics.collect._bump_event_last_seen``: ``first_seen_at``
  stamped and only moved earlier, the required-fields check, main only, an
  EventChange attributed to the scan (``source='scan'``), ``updated_at`` left alone, and one
  "seen in data" ticket comment published AFTER the commit (never on rollback);
* the daily sweep (``worker.tasks.lifecycle``): open, update, resolve and
  reopen ``sunset_overdue`` / ``successor_silent`` findings;
* the API: ``GET /lifecycle-findings``, ``lifecycle_findings`` on the event
  read, ``lifecycle_warning`` on catalog rows, ``GET /events/{id}/migration``,
  ``author_label`` on history, the activity rail entry (both read ``source``,
  so a deleted user's status edit is not "Seen in data"), and the membership 404.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.event import Event, EventStatus
from tripl.models.event_change import (
    EVENT_CHANGE_SOURCE_SCAN,
    SCAN_AUTHOR_LABEL,
    EventChange,
    create_event_change,
)
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.implementation_ticket import ImplementationTicket
from tripl.models.lifecycle_finding import LifecycleFinding, LifecycleFindingKind
from tripl.models.plan_branch import BranchKind, BranchStatus, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.lifecycle_rules import (
    ADOPTION_WINDOW,
    SUNSET_VOLUME_WINDOW,
    adoption_ratio,
    daily_average,
    prorated_window_volume,
    window_volume,
)
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.worker import celery_app as celery_app_module
from tripl.worker.tasks import lifecycle as lifecycle_tasks
from tripl.worker.tasks import metrics as _metrics_package  # noqa: F401
from tripl.worker.tasks.metrics import collect as metrics_collect

_NOW = datetime(2026, 9, 27, 6, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Pure window arithmetic
# --------------------------------------------------------------------------- #


def test_daily_bucket_overlapping_the_window_counts() -> None:
    event_id = uuid.uuid4()
    # Yesterday's daily bucket starts 30h before 06:00 today but ends inside
    # the 24h window, so it is "volume in the last 24 hours".
    yesterday = datetime(2026, 9, 26, tzinfo=UTC)
    rows = [(event_id, yesterday, 40, "1d")]
    assert window_volume(rows, now=_NOW, window=SUNSET_VOLUME_WINDOW) == {event_id: 40}
    # The same bucket read as hourly ended 29h ago: out of the window.
    hourly = [(event_id, yesterday, 40, "1h")]
    assert window_volume(hourly, now=_NOW, window=SUNSET_VOLUME_WINDOW) == {}
    # An interval-less scan is treated as hourly.
    assert (
        window_volume([(event_id, yesterday, 40, None)], now=_NOW, window=SUNSET_VOLUME_WINDOW)
        == {}
    )


def test_since_drops_buckets_that_ended_before_the_sunset() -> None:
    event_id = uuid.uuid4()
    rows = [
        (event_id, _NOW - timedelta(hours=5), 10, "1h"),
        (event_id, _NOW - timedelta(hours=2), 3, "1h"),
        (event_id, _NOW - timedelta(hours=1), 0, "1h"),
    ]
    sunset = {event_id: _NOW - timedelta(hours=3)}
    assert window_volume(rows, now=_NOW, window=SUNSET_VOLUME_WINDOW, since=sunset) == {event_id: 3}


def test_adoption_ratio_is_new_over_old() -> None:
    assert daily_average(700) == 100.0
    # "Old 1,240/day -> New 3,800/day": the successor gets ~3x the old volume.
    assert adoption_ratio(1240.0, 3800.0) == 3.06
    assert adoption_ratio(1000.0, 500.0) == 0.5
    assert adoption_ratio(10.0, 0.0) == 0.0
    # Undefined when the old event received nothing.
    assert adoption_ratio(0.0, 10.0) is None
    assert adoption_ratio(0.0, 0.0) is None


def test_seven_day_average_prorates_the_window_edge() -> None:
    event_id = uuid.uuid4()
    # _NOW is 06:00, so the window opens at 06:00 seven days back. Daily
    # buckets starting at midnight 1..7 days back: six lie wholly inside, the
    # oldest has 18 of its 24 hours inside and counts 3/4.
    rows = [(event_id, datetime(2026, 9, 27 - day, tzinfo=UTC), 1000, "1d") for day in range(1, 8)]
    # An eighth, older day ends before the window opens and counts nothing.
    rows.append((event_id, datetime(2026, 9, 19, tzinfo=UTC), 1000, "1d"))
    totals = prorated_window_volume(rows, now=_NOW, window=ADOPTION_WINDOW)
    assert totals[event_id] == pytest.approx(6750.0)
    assert daily_average(totals[event_id]) == pytest.approx(964.3)
    # The whole-bucket count would have over-read the same data.
    assert window_volume(rows, now=_NOW, window=ADOPTION_WINDOW) == {event_id: 7000}

    # Today's bucket, still in progress, holds only what arrived so far: whole.
    today = [(event_id, datetime(2026, 9, 27, tzinfo=UTC), 240, "1d")]
    assert prorated_window_volume(today, now=_NOW, window=ADOPTION_WINDOW) == {event_id: 240.0}
    # A weekly bucket that opened 10 days ago: 4 of its 7 days are inside.
    weekly = [(event_id, _NOW - timedelta(days=10), 700, "1w")]
    assert prorated_window_volume(weekly, now=_NOW, window=ADOPTION_WINDOW)[
        event_id
    ] == pytest.approx(400.0)
    # A bucket that starts after ``now`` is not in the window.
    future = [(event_id, _NOW + timedelta(hours=1), 5, "1h")]
    assert prorated_window_volume(future, now=_NOW, window=ADOPTION_WINDOW) == {}


# --------------------------------------------------------------------------- #
# Sync seeding
# --------------------------------------------------------------------------- #


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def _seed_project(
    session: Session, *, interval: str = "1h"
) -> tuple[Project, EventType, ScanConfig]:
    project = Project(
        id=uuid.uuid4(), name="Life", slug=f"life-{uuid.uuid4().hex[:8]}", description=""
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
        cardinality_threshold=100,
        interval=interval,
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
    branch_id: uuid.UUID | None = None,
    **extra: Any,
) -> Event:
    edited_at = datetime(2026, 1, 1, 9, tzinfo=UTC)
    kwargs: dict[str, Any] = {}
    if branch_id is not None:
        kwargs["branch_id"] = branch_id
    event = Event(
        id=uuid.uuid4(),
        project_id=project.id,
        event_type_id=event_type.id,
        name=name,
        status=status.value,
        created_at=edited_at,
        updated_at=edited_at,
        **kwargs,
        **extra,
    )
    session.add(event)
    session.commit()
    return event


def _metric(
    session: Session, config: ScanConfig, event: Event, bucket: datetime, count: int
) -> None:
    session.add(
        EventMetric(
            id=uuid.uuid4(),
            scan_config_id=config.id,
            event_id=event.id,
            event_type_id=None,
            bucket=bucket,
            count=count,
        )
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


# --------------------------------------------------------------------------- #
# Auto-live
# --------------------------------------------------------------------------- #


def test_auto_live_promotes_complete_main_events_only(
    sync_session_factory: sessionmaker[Session],
) -> None:
    bucket = datetime(2026, 5, 1, 10, tzinfo=UTC)
    with sync_session_factory() as session:
        project, event_type, config = _seed_project(session)
        required = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name="screen",
            display_name="Screen",
            field_type="string",
            is_required=True,
        )
        optional = FieldDefinition(
            id=uuid.uuid4(),
            event_type_id=event_type.id,
            name="note",
            display_name="Note",
            field_type="string",
            is_required=False,
        )
        session.add_all([required, optional])
        session.commit()

        complete = _add_event(
            session, project, event_type, "complete", status=EventStatus.ready_for_dev
        )
        blank = _add_event(session, project, event_type, "blank", status=EventStatus.implemented)
        absent = _add_event(
            session, project, event_type, "absent", status=EventStatus.ready_for_dev
        )
        session.add_all(
            [
                EventFieldValue(
                    id=uuid.uuid4(),
                    event_id=complete.id,
                    field_definition_id=required.id,
                    value="home",
                ),
                EventFieldValue(
                    id=uuid.uuid4(), event_id=blank.id, field_definition_id=required.id, value="   "
                ),
                # Only the optional field has a value: the required one is absent.
                EventFieldValue(
                    id=uuid.uuid4(), event_id=absent.id, field_definition_id=optional.id, value="x"
                ),
            ]
        )
        branch = PlanBranch(
            id=uuid.uuid4(),
            project_id=project.id,
            name="feature",
            kind=BranchKind.working.value,
            status=BranchStatus.draft.value,
        )
        session.add(branch)
        session.commit()
        on_branch = _add_event(
            session,
            project,
            event_type,
            "branch_copy",
            status=EventStatus.ready_for_dev,
            branch_id=branch.id,
        )

        metrics_collect._bump_event_last_seen(
            session,
            event_agg={
                (config.id, complete.id, bucket): 5,
                (config.id, blank.id, bucket): 5,
                (config.id, absent.id, bucket): 5,
                (config.id, on_branch.id, bucket): 5,
            },
        )
        session.commit()
        session.expire_all()

        statuses = {row.name: row.status for row in session.execute(select(Event)).scalars().all()}
        assert statuses == {
            "complete": EventStatus.live.value,
            "blank": EventStatus.implemented.value,
            "absent": EventStatus.ready_for_dev.value,
            "branch_copy": EventStatus.ready_for_dev.value,
        }
        changes = session.execute(select(EventChange)).scalars().all()
        assert [
            (c.event_id, c.user_id, c.source, c.field, c.old_value, c.new_value) for c in changes
        ] == [(complete.id, None, EVENT_CHANGE_SOURCE_SCAN, "status", "ready_for_dev", "live")]
        promoted = session.get(Event, complete.id)
        assert promoted is not None
        # The transition is announced through its EventChange, not by bumping
        # the row's updated_at.
        assert _utc(promoted.updated_at) == datetime(2026, 1, 1, 9, tzinfo=UTC)
        # Every event with volume gets its first sighting, promoted or not.
        for event in (complete, blank, absent, on_branch):
            row = session.get(Event, event.id)
            assert row is not None
            assert _utc(row.first_seen_at) == bucket


def test_first_seen_only_moves_earlier(sync_session_factory: sessionmaker[Session]) -> None:
    first = datetime(2026, 5, 1, 10, tzinfo=UTC)
    with sync_session_factory() as session:
        project, event_type, config = _seed_project(session)
        event = _add_event(session, project, event_type, "e", status=EventStatus.live)
        agg = {
            (config.id, event.id, first): 3,
            (config.id, event.id, first + timedelta(hours=2)): 4,
            # A zero-count bucket is not a sighting.
            (config.id, event.id, first - timedelta(hours=5)): 0,
        }
        metrics_collect._bump_event_last_seen(session, event_agg=agg)
        session.commit()
        metrics_collect._bump_event_last_seen(
            session, event_agg={(config.id, event.id, first + timedelta(days=1)): 9}
        )
        session.commit()
        session.expire_all()
        row = session.get(Event, event.id)
        assert row is not None
        assert _utc(row.first_seen_at) == first
        assert _utc(row.last_seen_at) == first + timedelta(days=1)

        # A replay of an older window corrects it backwards.
        earlier = first - timedelta(days=3)
        metrics_collect._bump_event_last_seen(
            session, event_agg={(config.id, event.id, earlier): 1}
        )
        session.commit()
        session.expire_all()
        row = session.get(Event, event.id)
        assert row is not None
        assert _utc(row.first_seen_at) == earlier
        assert _utc(row.last_seen_at) == first + timedelta(days=1)


def test_ticket_comment_is_published_after_commit_only(
    sync_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, list[Any]]] = []

    def _send_task(name: str, args: list[Any] | None = None, **_kwargs: Any) -> None:
        sent.append((name, list(args or [])))

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", _send_task)
    bucket = datetime(2026, 5, 1, 10, tzinfo=UTC)
    with sync_session_factory() as session:
        project, event_type, config = _seed_project(session)
        covered = _add_event(
            session, project, event_type, "covered", status=EventStatus.implemented
        )
        uncovered = _add_event(
            session, project, event_type, "uncovered", status=EventStatus.ready_for_dev
        )
        rolled_back = _add_event(
            session, project, event_type, "rolled", status=EventStatus.ready_for_dev
        )
        main_branch_id = covered.branch_id
        ticket = ImplementationTicket(
            id=uuid.uuid4(),
            project_id=project.id,
            branch_id=main_branch_id,
            tracker_type="jira",
            external_id="10001",
            external_key="PLAN-1",
            event_ids=[str(covered.id), str(rolled_back.id)],
        )
        session.add(ticket)
        session.commit()
        # Plain values: the session expires on commit and is closed before the
        # assertion below, so an ORM attribute read there would need a refresh.
        ticket_id = str(ticket.id)
        covered_id = str(covered.id)

        # A rolled-back promotion must not comment.
        metrics_collect._bump_event_last_seen(
            session, event_agg={(config.id, rolled_back.id, bucket): 2}
        )
        session.rollback()
        assert sent == []

        metrics_collect._bump_event_last_seen(
            session,
            event_agg={
                (config.id, covered.id, bucket): 2,
                (config.id, uncovered.id, bucket): 2,
            },
        )
        assert sent == []  # nothing before the commit
        session.commit()

    assert sent == [
        (
            metrics_collect.SEEN_IN_DATA_COMMENT_TASK,
            [ticket_id, [covered_id], bucket.isoformat()],
        )
    ]


# --------------------------------------------------------------------------- #
# Sunset watch
# --------------------------------------------------------------------------- #


def _findings(session: Session) -> dict[tuple[uuid.UUID, str], LifecycleFinding]:
    session.expire_all()
    return {
        (row.event_id, row.kind): row
        for row in session.execute(select(LifecycleFinding)).scalars().all()
    }


def test_sweep_opens_updates_resolves_and_reopens(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        project, event_type, config = _seed_project(session, interval="1d")
        successor = _add_event(
            session, project, event_type, "new_checkout", status=EventStatus.live
        )
        old = _add_event(
            session,
            project,
            event_type,
            "old_checkout",
            status=EventStatus.deprecated,
            sunset_at=_NOW - timedelta(days=10),
            superseded_by_event_id=successor.id,
        )
        # Deprecated but its sunset is still ahead: never overdue.
        pending = _add_event(
            session,
            project,
            event_type,
            "pending",
            status=EventStatus.deprecated,
            sunset_at=_NOW + timedelta(days=3),
        )
        # Yesterday's daily bucket overlaps the last 24 hours at 06:00.
        _metric(session, config, old, datetime(2026, 9, 26, tzinfo=UTC), 1240)
        _metric(session, config, pending, datetime(2026, 9, 26, tzinfo=UTC), 50)
        session.commit()

        stats = lifecycle_tasks.compute_lifecycle_findings(session, now=_NOW)
        session.commit()
        assert stats.opened == 2
        found = _findings(session)
        assert set(found) == {
            (old.id, LifecycleFindingKind.sunset_overdue.value),
            (old.id, LifecycleFindingKind.successor_silent.value),
        }
        overdue = found[(old.id, LifecycleFindingKind.sunset_overdue.value)]
        assert overdue.volume_24h == 1240
        assert overdue.project_id == project.id
        silent = found[(old.id, LifecycleFindingKind.successor_silent.value)]
        assert silent.related_event_id == successor.id
        assert silent.successor_volume_7d == 0
        assert [
            row.id for row in lifecycle_tasks.load_open_findings(session, project_id=project.id)
        ]

        # Next day: still overdue (updated, first_seen kept); the successor
        # picked up traffic, so its finding resolves.
        later = _NOW + timedelta(days=1)
        _metric(session, config, old, datetime(2026, 9, 27, tzinfo=UTC), 900)
        _metric(session, config, successor, datetime(2026, 9, 27, tzinfo=UTC), 3800)
        session.commit()
        stats = lifecycle_tasks.compute_lifecycle_findings(session, now=later)
        session.commit()
        assert (stats.updated, stats.resolved, stats.opened) == (1, 1, 0)
        found = _findings(session)
        overdue = found[(old.id, LifecycleFindingKind.sunset_overdue.value)]
        assert overdue.volume_24h == 900
        assert overdue.first_seen_at == _NOW
        assert overdue.last_seen_at == later
        assert overdue.resolved_at is None
        assert found[(old.id, LifecycleFindingKind.successor_silent.value)].resolved_at == later

        # Old event goes quiet (and the successor too, 9 days on): overdue
        # resolves, the successor finding reopens as a new episode.
        quiet = _NOW + timedelta(days=9)
        stats = lifecycle_tasks.compute_lifecycle_findings(session, now=quiet)
        session.commit()
        assert (stats.resolved, stats.reopened) == (1, 1)
        found = _findings(session)
        assert found[(old.id, LifecycleFindingKind.sunset_overdue.value)].resolved_at == quiet
        reopened = found[(old.id, LifecycleFindingKind.successor_silent.value)]
        assert reopened.resolved_at is None
        assert reopened.first_seen_at == quiet

        # Un-deprecating the event clears everything on the next run.
        session.execute(
            update(Event).where(Event.id == old.id).values(status=EventStatus.live.value)
        )
        session.commit()
        lifecycle_tasks.compute_lifecycle_findings(session, now=quiet + timedelta(days=1))
        session.commit()
        assert lifecycle_tasks.load_open_findings(session, project_id=project.id) == []


def test_sweep_ignores_traffic_before_the_sunset(
    sync_session_factory: sessionmaker[Session],
) -> None:
    with sync_session_factory() as session:
        project, event_type, config = _seed_project(session, interval="1h")
        event = _add_event(
            session,
            project,
            event_type,
            "just_retired",
            status=EventStatus.deprecated,
            sunset_at=_NOW - timedelta(hours=2),
        )
        _metric(session, config, event, _NOW - timedelta(hours=4), 30)
        session.commit()
        stats = lifecycle_tasks.compute_lifecycle_findings(session, now=_NOW)
        session.commit()
        assert stats.opened == 0


def test_beat_schedules_the_sweep_daily() -> None:
    entry = celery_app_module.celery_app.conf.beat_schedule["check-lifecycle-findings"]
    assert entry["task"] == "tripl.worker.tasks.lifecycle.check_lifecycle_findings"
    assert entry["schedule"].hour == {5}
    assert (
        "tripl.worker.tasks.lifecycle.check_lifecycle_findings"
        in celery_app_module.celery_app.tasks
    )


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


async def _post(client: AsyncClient, url: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(url, json=body)
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert isinstance(data, dict)
    return data


async def _seed_api(client: AsyncClient, slug: str) -> dict[str, Any]:
    await _post(client, "/api/v1/projects", {"name": slug, "slug": slug, "description": ""})
    base = f"/api/v1/projects/{slug}"
    track = (await _post(client, f"{base}/event-types", {"name": "track", "display_name": "T"}))[
        "id"
    ]
    old = (await _post(client, f"{base}/events", {"event_type_id": track, "name": "old_checkout"}))[
        "id"
    ]
    new = (await _post(client, f"{base}/events", {"event_type_id": track, "name": "new_checkout"}))[
        "id"
    ]
    other = (await _post(client, f"{base}/events", {"event_type_id": track, "name": "other"}))["id"]
    old_id, new_id, other_id = uuid.UUID(old), uuid.UUID(new), uuid.UUID(other)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async with TestSessionLocal() as session:
        project = (await session.execute(select(Project).where(Project.slug == slug))).scalar_one()
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"DS {slug}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add(data_source)
        config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project.id,
            event_type_id=uuid.UUID(track),
            name="Scan",
            base_query="SELECT time, event_name FROM events",
            time_column="time",
            cardinality_threshold=100,
            interval="1d",
        )
        session.add(config)
        await session.execute(
            update(Event)
            .where(Event.id == old_id)
            .values(
                status=EventStatus.deprecated.value,
                sunset_at=now - timedelta(days=5),
                superseded_by_event_id=new_id,
            )
        )
        # 7 daily buckets inside the window: 1240/day old, 3800/day new.
        for day in range(1, 8):
            bucket = (now - timedelta(days=day)).replace(hour=0)
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=old_id,
                    bucket=bucket,
                    count=1240,
                )
            )
            session.add(
                EventMetric(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    event_id=new_id,
                    bucket=bucket,
                    count=3800,
                )
            )
        finding = LifecycleFinding(
            id=uuid.uuid4(),
            project_id=project.id,
            event_id=old_id,
            kind=LifecycleFindingKind.sunset_overdue.value,
            first_seen_at=now,
            last_seen_at=now,
            volume_24h=1240,
        )
        resolved = LifecycleFinding(
            id=uuid.uuid4(),
            project_id=project.id,
            event_id=old_id,
            kind=LifecycleFindingKind.successor_silent.value,
            related_event_id=new_id,
            first_seen_at=now - timedelta(days=3),
            last_seen_at=now - timedelta(days=2),
            resolved_at=now - timedelta(days=1),
            successor_volume_7d=0,
        )
        session.add_all([finding, resolved])
        await session.commit()
    return {"base": base, "old": old_id, "new": new_id, "other": other_id, "finding": finding.id}


@pytest.mark.asyncio
async def test_findings_route_event_payload_and_catalog_chip(client: AsyncClient) -> None:
    s = await _seed_api(client, "life-api")
    base = s["base"]

    resp = await client.get(f"{base}/lifecycle-findings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == str(s["finding"])
    assert item["kind"] == "sunset_overdue"
    assert item["event_name"] == "old_checkout"
    assert item["volume_24h"] == 1240
    assert item["resolved_at"] is None

    everything = await client.get(f"{base}/lifecycle-findings", params={"include_resolved": "true"})
    kinds = [row["kind"] for row in everything.json()["items"]]
    assert kinds == ["sunset_overdue", "successor_silent"]  # open first
    silent = everything.json()["items"][1]
    assert silent["related_event_name"] == "new_checkout"

    event = await client.get(f"{base}/events/{s['old']}")
    assert event.status_code == 200
    assert [f["kind"] for f in event.json()["lifecycle_findings"]] == ["sunset_overdue"]
    # The event's first sighting falls back to the metrics when unstamped.
    assert event.json()["first_seen_at"] is not None
    other = await client.get(f"{base}/events/{s['other']}")
    assert other.json()["lifecycle_findings"] == []

    listing = await client.get(f"{base}/events")
    chips = {row["name"]: row["lifecycle_warning"] for row in listing.json()["items"]}
    assert chips == {"old_checkout": True, "new_checkout": False, "other": False}


@pytest.mark.asyncio
async def test_migration_route(client: AsyncClient) -> None:
    s = await _seed_api(client, "life-migrate")
    resp = await client.get(f"{s['base']}/events/{s['old']}/migration")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["old"]["event_id"] == str(s["old"])
    assert body["old"]["name"] == "old_checkout"
    assert body["new"]["name"] == "new_checkout"
    # Six daily buckets lie inside the 7-day window; the oldest straddles its
    # start and is prorated, so the average dips below the daily count by up
    # to 1/7 depending on the hour the test runs.
    assert body["old"]["daily_avg_7d"] == pytest.approx(1240.0, rel=0.15)
    assert body["new"]["daily_avg_7d"] == pytest.approx(3800.0, rel=0.15)
    assert body["old"]["daily_avg_7d"] <= 1240.0
    # new / old: the successor receives ~3x what the old event still does.
    assert body["ratio"] == pytest.approx(3.06, abs=0.01)

    no_successor = await client.get(f"{s['base']}/events/{s['other']}/migration")
    assert no_successor.status_code == 404
    missing = await client.get(f"{s['base']}/events/{uuid.uuid4()}/migration")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_history_label_and_activity_entry(client: AsyncClient) -> None:
    s = await _seed_api(client, "life-history")
    async with TestSessionLocal() as session:
        session.add(
            create_event_change(
                event_id=s["other"],
                user_id=None,
                field="status",
                old_value="ready_for_dev",
                new_value="live",
                source=EVENT_CHANGE_SOURCE_SCAN,
            )
        )
        # A PERSON's status edit whose author was since deleted: ``user_id``
        # is ON DELETE SET NULL, so the row reads NULL there exactly like the
        # scan's — but it carries no ``source``, and it is still a person's.
        session.add(
            create_event_change(
                event_id=s["new"],
                user_id=None,
                field="status",
                old_value="ready_for_dev",
                new_value="live",
            )
        )
        await session.commit()

    history = await client.get(f"{s['base']}/events/{s['other']}/history")
    assert history.status_code == 200
    scan_rows = [row for row in history.json() if row["field"] == "status"]
    assert scan_rows and scan_rows[0]["author_label"] == SCAN_AUTHOR_LABEL
    person = await client.get(f"{s['base']}/events/{s['new']}/history")
    assert person.status_code == 200
    person_rows = [row for row in person.json() if row["field"] == "status"]
    assert person_rows and person_rows[0]["author_label"] is None

    activity = await client.get("/api/v1/activity/projects/life-history")
    assert activity.status_code == 200, activity.text
    titles = [row["title"] for row in activity.json()]
    assert "Seen in data: other is live" in titles
    assert "Seen in data: new_checkout is live" not in titles
    entry = next(row for row in activity.json() if row["title"] == "Seen in data: other is live")
    assert entry["detail"].startswith(SCAN_AUTHOR_LABEL)


@pytest.mark.asyncio
async def test_non_member_gets_404_and_viewer_can_read(client: AsyncClient) -> None:
    s = await _seed_api(client, "life-private")
    await client.post("/api/v1/auth/logout")
    registered = await client.post(
        "/api/v1/auth/register",
        json={"email": "stranger@example.com", "password": "Password123!", "name": "Stranger"},
    )
    assert registered.status_code == 201, registered.text
    urls = [f"{s['base']}/lifecycle-findings", f"{s['base']}/events/{s['old']}/migration"]
    for url in urls:
        resp = await client.get(url)
        assert resp.status_code == 404, (url, resp.text)
        assert resp.json()["detail"] == "Project not found"

    await add_member_by_slug("life-private", "stranger@example.com", "viewer")
    for url in urls:
        resp = await client.get(url)
        assert resp.status_code == 200, (url, resp.text)
