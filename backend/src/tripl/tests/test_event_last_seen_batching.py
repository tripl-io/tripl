"""``_bump_event_last_seen`` writes in batches, not one statement per event.

``process_chunk`` calls the bump once per replay chunk and commits per chunk, so
a per-event round trip on the sync worker engine — which has no pipelining —
multiplied out to chunks x catalog statements on a historical replay.
These tests pin the statement SHAPE: the row state they assert
alongside it is the same state the per-event form produced, and is what stops a
"one statement" count from being satisfied by a statement that writes the wrong
value.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.event import Event, EventStatus
from tripl.models.event_change import EventChange
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.worker.tasks.metrics import collect as metrics_collect

_BASE = datetime(2026, 5, 1, 10, tzinfo=UTC)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A sync SQLite session factory, mirroring the worker's sync engine."""
    engine = create_engine(f"sqlite:///{tmp_path / 'batch3_e1.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


@contextlib.contextmanager
def _captured_sql(engine: Engine) -> Iterator[list[str]]:
    """Collect every statement ``engine`` executes inside the block."""
    statements: list[str] = []

    def _record(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        sa_event.remove(engine, "before_cursor_execute", _record)


def _event_updates(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith("UPDATE EVENTS")]


def _seed_events(session: Session, statuses: list[EventStatus]) -> list[Event]:
    """One project, one event type, one Event per entry in ``statuses``.

    ``uq_event_scan_identity`` is on (event_type_id, source_name) and
    ``source_name`` stays NULL here, so the siblings coexist under one type.
    """
    project = Project(
        id=uuid.uuid4(),
        name="Batch Bump",
        slug=f"batch-bump-{uuid.uuid4().hex[:8]}",
        description="",
    )
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="structured",
        display_name="Structured",
        description="",
    )
    session.add_all([project, event_type])

    events = [
        Event(
            id=uuid.uuid4(),
            project_id=project.id,
            event_type_id=event_type.id,
            name=f"event_{index}",
            status=status.value,
        )
        for index, status in enumerate(statuses)
    ]
    session.add_all(events)
    session.commit()
    return events


def _last_seen(session: Session, event_id: uuid.UUID) -> datetime | None:
    session.expire_all()
    row = session.get(Event, event_id)
    assert row is not None
    value = row.last_seen_at
    # SQLite (test backend) drops tzinfo on read; normalize for compare.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def test_bump_event_last_seen_batches_into_one_update(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """25 events with 25 DISTINCT buckets cost one UPDATE, not 25."""
    with sync_session_factory() as session:
        # Drafts are outside AUTO_LIVE_FROM, so the promotion path stays quiet
        # and cannot pad the statement count.
        events = _seed_events(session, [EventStatus.draft] * 25)
        event_agg = {
            (uuid.uuid4(), event.id, _BASE + timedelta(hours=index)): 5
            for index, event in enumerate(events)
        }

        engine = session.get_bind()
        assert isinstance(engine, Engine)
        with _captured_sql(engine) as statements:
            metrics_collect._bump_event_last_seen(session, event_agg=event_agg)
            session.commit()

        assert len(_event_updates(statements)) == 1

        # Distinct buckets per event: one shared value written to all 25 rows
        # would satisfy the count above but not this.
        for index, event in enumerate(events):
            assert _last_seen(session, event.id) == _BASE + timedelta(hours=index)


def test_bump_event_last_seen_splits_batches_at_the_bind_ceiling(
    sync_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the per-statement bind ceiling the bump splits, and splits cleanly.

    The real ceiling is 20000 events per statement, which no test can afford to
    seed; shrinking it to 10 exercises the same loop with 25 rows.
    """
    monkeypatch.setattr(metrics_collect, "_MAX_EVENTS_PER_BUMP", 10)

    with sync_session_factory() as session:
        events = _seed_events(session, [EventStatus.draft] * 25)
        event_agg = {
            (uuid.uuid4(), event.id, _BASE + timedelta(hours=index)): 5
            for index, event in enumerate(events)
        }

        engine = session.get_bind()
        assert isinstance(engine, Engine)
        with _captured_sql(engine) as statements:
            metrics_collect._bump_event_last_seen(session, event_agg=event_agg)
            session.commit()

        assert len(_event_updates(statements)) == 3  # 10 + 10 + 5

        for index, event in enumerate(events):
            assert _last_seen(session, event.id) == _BASE + timedelta(hours=index)


def test_bump_event_last_seen_batches_the_auto_live_promotion(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """Promotion costs one UPDATE per distinct previous status, not per event."""
    with sync_session_factory() as session:
        statuses = [EventStatus.implemented] * 5 + [EventStatus.ready_for_dev] * 5
        events = _seed_events(session, statuses)
        event_agg = {(uuid.uuid4(), event.id, _BASE): 3 for event in events}

        engine = session.get_bind()
        assert isinstance(engine, Engine)
        with _captured_sql(engine) as statements:
            metrics_collect._bump_event_last_seen(session, event_agg=event_agg)
            session.commit()

        status_updates = [s for s in _event_updates(statements) if "status=" in s]
        assert len(status_updates) <= 2  # one per member of AUTO_LIVE_FROM

        session.expire_all()
        for event in events:
            refreshed = session.get(Event, event.id)
            assert refreshed is not None
            assert refreshed.status == EventStatus.live

        # Grouping the UPDATE must not smear the audit trail: every event keeps
        # its OWN previous status as ``old_value``.
        changes = session.execute(select(EventChange)).scalars().all()
        assert len(changes) == len(events)
        old_by_event = {change.event_id: change for change in changes}
        for event, previous in zip(events, statuses, strict=True):
            change = old_by_event[event.id]
            assert change.user_id is None
            assert change.field == "status"
            assert change.old_value == previous
            assert change.new_value == EventStatus.live
