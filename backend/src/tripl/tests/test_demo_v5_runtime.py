"""The demo runtime on the shared demo warehouse (recipe 5).

A demo is advanced by the ordinary scheduled collection reading the shared
demo warehouse; there is no synthetic tick any more. These tests pin:

* the scheduler: a ``demo`` source is dispatched however far back its progress
  sits (the old synthetic resume gate is gone), the cooldown also runs from the
  demo's seeding, ``DEMO_RUNTIME_ENABLED=false`` freezes demos only, and a
  legacy ``synthetic`` demo is never dispatched;
* the window clamp: a demo window never reaches past what the warehouse holds;
* the daily demo-side retention prune: observed rows only, demos only;
* the shadow-candidate fix (every project): a candidate an event now holds is
  hidden, resolved by the collector, and Accept links instead of 409ing;
* the flaky demo sink: its first attempt fails, labelled and not transient, and
  Retry delivers — all without network.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

# Imported first and for its side effect: the worker task package is
# import-order sensitive, and celery_app's bottom-of-file registration is what
# pulls the task modules in an order they all survive.
import tripl.worker.celery_app  # noqa: F401
from tripl.config import settings
from tripl.core.bucketing import to_utc
from tripl.core.demo_warehouse import status as warehouse_status_module
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.event import Event, EventStatus
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_ACCEPTED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.services.shadow_identity import resolve_held_candidates
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts as alerts_task
from tripl.worker.tasks import demo_runtime
from tripl.worker.tasks._errors import ScanError, is_transient_send_error
from tripl.worker.tasks.metrics import demo_window
from tripl.worker.tasks.metrics import schedule as metrics_schedule
from tripl.worker.tasks.metrics import tasks as metrics_tasks

_HOUR = timedelta(hours=1)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'demo_v5_runtime.db'}")
    enable_sqlite_foreign_keys(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _memory_warehouse(monkeypatch: MonkeyPatch) -> Iterator[None]:
    """The in-memory engine, deterministically: no connect attempt anywhere."""
    monkeypatch.setattr(settings, "demo_warehouse_mode", "memory")
    warehouse_status_module.clear_cache()
    yield
    warehouse_status_module.clear_cache()


def _hour(now: datetime) -> datetime:
    return now.replace(minute=0, second=0, microsecond=0)


def _seed_config(
    session: Session,
    *,
    now: datetime,
    is_demo: bool = True,
    db_type: str = "demo",
    seeded_at: datetime | None = None,
    last_accessed: datetime | None = None,
    stale_hours: int = 30,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A due config whose stored history stops ``stale_hours`` back.

    The one dispatcher job on it is as old as that history, so neither the
    cooldown nor anything else but the gate under test decides dispatch.
    Returns ``(project_id, scan_config_id, event_type_id)``.
    """
    tag = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        name=f"V5 runtime {tag}",
        slug=f"v5-runtime-{tag}",
        description="",
        is_demo=is_demo,
        demo_seeded_at=seeded_at,
        demo_last_accessed_at=last_accessed,
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"V5 runtime DS {tag}",
        db_type=db_type,
        host="demo",
        port=0,
        database_name="demo",
        username="",
        password_encrypted="",
        project_id=project.id if is_demo else None,
    )
    session.add(project)
    session.flush()
    session.add(data_source)
    session.flush()
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name=f"screen_view-{tag}",
        display_name="Screen view",
        description="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name=f"Demo scan {tag}",
        base_query="SELECT event_time, event_type, event_name FROM events",
        time_column="event_time",
        cardinality_threshold=100,
        interval="1h",
    )
    session.add_all([event_type, config])
    session.flush()
    stale_at = _hour(now) - timedelta(hours=stale_hours)
    session.add_all(
        [
            EventMetric(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                event_id=None,
                event_type_id=event_type.id,
                bucket=stale_at,
                count=1200,
            ),
            ScanJob(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                status=ScanJobStatus.completed.value,
                created_at=stale_at,
                completed_at=stale_at,
                result_summary={"mode": metrics_tasks.METRICS_COLLECTION_MODE},
            ),
        ]
    )
    session.commit()
    return project.id, config.id, event_type.id


def _run_dispatcher(factory: sessionmaker[Session], monkeypatch: MonkeyPatch) -> list[str]:
    dispatched: list[str] = []
    monkeypatch.setattr(metrics_schedule, "_get_sync_session", factory)
    monkeypatch.setattr(
        metrics_schedule.collect_metrics,
        "delay",
        lambda scan_config_id, scan_job_id: dispatched.append(scan_config_id),
    )
    metrics_schedule.check_metrics_due.run()
    return dispatched


# ── The scheduler ────────────────────────────────────────────────────────────


def test_an_active_demo_is_collected_however_far_back_its_progress_sits(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """No synthetic resume gate: the warehouse holds the gap, so collect it."""
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        _, config_id, _ = _seed_config(
            session,
            now=now,
            seeded_at=now - timedelta(days=3),
            last_accessed=now - timedelta(minutes=5),
            stale_hours=30,
        )
    assert _run_dispatcher(sync_session_factory, monkeypatch) == [str(config_id)]


def test_the_cooldown_also_runs_from_the_demo_seeding(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    """Provisioning replayed up to now: no dispatch straight after it."""
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        _seed_config(
            session,
            now=now,
            seeded_at=now - timedelta(minutes=20),
            last_accessed=now - timedelta(minutes=1),
        )
    assert _run_dispatcher(sync_session_factory, monkeypatch) == []


def test_a_legacy_synthetic_demo_is_never_dispatched(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    with sync_session_factory() as session:
        _, legacy_id, _ = _seed_config(
            session,
            now=now,
            db_type="synthetic",
            seeded_at=now - timedelta(days=3),
            last_accessed=now - timedelta(minutes=5),
        )
        _, live_id, _ = _seed_config(
            session,
            now=now,
            seeded_at=now - timedelta(days=3),
            last_accessed=now - timedelta(minutes=5),
        )
    dispatched = _run_dispatcher(sync_session_factory, monkeypatch)
    assert str(legacy_id) not in dispatched
    assert dispatched == [str(live_id)]


def test_demo_runtime_disabled_freezes_demos_and_only_demos(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr(settings, "demo_runtime_enabled", False)
    with sync_session_factory() as session:
        _seed_config(
            session,
            now=now,
            seeded_at=now - timedelta(days=3),
            last_accessed=now - timedelta(minutes=5),
        )
        _, real_id, _ = _seed_config(session, now=now, is_demo=False, db_type="postgres")
    assert _run_dispatcher(sync_session_factory, monkeypatch) == [str(real_id)]


# ── The window clamp ─────────────────────────────────────────────────────────


def test_a_demo_window_is_clamped_to_the_warehouse_retention() -> None:
    now = datetime(2026, 10, 16, 9, 40, tzinfo=UTC)
    horizon = _hour(now) - timedelta(days=settings.demo_warehouse_retention_days)
    deep = horizon - timedelta(days=5)
    time_to = _hour(now)

    clamped = demo_window.clamp_demo_window_start(deep, time_to, interval_code="1h", now=now)
    assert clamped == horizon

    inside = time_to - timedelta(hours=3)
    assert (
        demo_window.clamp_demo_window_start(inside, time_to, interval_code="1h", now=now) == inside
    )


def test_a_clamp_rounds_up_onto_the_grid_and_keeps_naive_bounds_naive(
    monkeypatch: MonkeyPatch,
) -> None:
    """On PostgreSQL the horizon is the writer's oldest retained instant."""
    now = datetime(2026, 10, 16, 9, 40, tzinfo=UTC)
    oldest = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    status = warehouse_status_module.warehouse_status_unknown()
    monkeypatch.setattr(
        demo_window,
        "warehouse_status",
        lambda: warehouse_status_module.WarehouseStatus(
            **{**status.as_dict(), "engine": "postgres", "oldest": oldest}  # type: ignore[arg-type]
        ),
    )
    naive_from = datetime(2026, 9, 20, 0, 0)
    clamped = demo_window.clamp_demo_window_start(
        naive_from, datetime(2026, 10, 16, 9, 0), interval_code="1h", now=now
    )
    assert clamped == datetime(2026, 10, 1, 1, 0)
    assert clamped.tzinfo is None


def test_a_window_the_warehouse_holds_nothing_of_is_refused_not_zeroed() -> None:
    now = datetime(2026, 10, 16, 9, 40, tzinfo=UTC)
    horizon = _hour(now) - timedelta(days=settings.demo_warehouse_retention_days)
    with pytest.raises(ScanError):
        demo_window.clamp_demo_window_start(
            horizon - timedelta(days=3),
            horizon - timedelta(days=1),
            interval_code="1h",
            now=now,
        )


# ── The retention prune ──────────────────────────────────────────────────────


def test_the_prune_deletes_old_observed_rows_of_demos_only(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    now = datetime(2026, 10, 16, 3, 17, tzinfo=UTC)
    cutoff = demo_runtime.demo_retention_cutoff(now)
    with sync_session_factory() as session:
        demo_project, demo_config, demo_type = _seed_config(
            session, now=now, seeded_at=now - timedelta(days=40), stale_hours=1
        )
        real_project, real_config, real_type = _seed_config(
            session, now=now, is_demo=False, db_type="postgres", stale_hours=1
        )
        for project_id, config_id, type_id in (
            (demo_project, demo_config, demo_type),
            (real_project, real_config, real_type),
        ):
            session.add_all(
                [
                    EventMetric(
                        id=uuid.uuid4(),
                        scan_config_id=config_id,
                        event_id=None,
                        event_type_id=type_id,
                        bucket=cutoff - _HOUR,
                        count=7,
                    ),
                    MetricAnomaly(
                        id=uuid.uuid4(),
                        scan_config_id=config_id,
                        scope_type="event_type",
                        scope_ref=str(type_id),
                        event_type_id=type_id,
                        bucket=cutoff - _HOUR,
                        actual_count=1,
                        expected_count=10,
                        stddev=1,
                        z_score=-9,
                        direction="drop",
                    ),
                ]
            )
            session.add(
                Event(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    event_type_id=type_id,
                    name=f"Authored {uuid.uuid4().hex[:6]}",
                )
            )
        session.commit()

    monkeypatch.setattr(demo_runtime, "_get_sync_session", sync_session_factory)
    result = demo_runtime.prune_demo_history.run(now=now)
    assert result["pruned"] == 1

    with sync_session_factory() as session:

        def buckets(config_id: uuid.UUID) -> list[datetime]:
            return list(
                session.execute(
                    select(EventMetric.bucket).where(EventMetric.scan_config_id == config_id)
                ).scalars()
            )

        assert all(to_utc(b) >= cutoff for b in buckets(demo_config))
        assert len(buckets(demo_config)) == 1  # the recent seeded bucket survives
        assert len(buckets(real_config)) == 2  # a real project is never pruned
        assert (
            session.execute(
                select(MetricAnomaly.id).where(MetricAnomaly.scan_config_id == demo_config)
            ).first()
            is None
        )
        # The authored plan stays whole.
        assert session.execute(select(Event.id).where(Event.project_id == demo_project)).first()


# ── Shadow candidates an event now holds ─────────────────────────────────────


def _candidate(
    project_id: uuid.UUID,
    config_id: uuid.UUID,
    type_id: uuid.UUID | None,
    name: str,
    now: datetime,
) -> ShadowEventCandidate:
    return ShadowEventCandidate(
        id=uuid.uuid4(),
        project_id=project_id,
        scan_config_id=config_id,
        event_type_id=type_id,
        event_name=name,
        observed_count=12,
        first_seen_at=now - timedelta(days=2),
        last_seen_at=now - timedelta(days=1),
        status=SHADOW_STATUS_NEW,
    )


def test_the_collector_resolves_candidates_an_event_now_holds(
    sync_session_factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
    with sync_session_factory() as session:
        project_id, config_id, type_id = _seed_config(session, now=now, stale_hours=1)
        held = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            event_type_id=type_id,
            name="App Heartbeat",
            source_name="app_heartbeat_v1",
            status=EventStatus.in_review,
        )
        archived = Event(
            id=uuid.uuid4(),
            project_id=project_id,
            event_type_id=type_id,
            name="old_ping",
            status=EventStatus.archived,
        )
        session.add_all([held, archived])
        session.flush()
        session.add_all(
            [
                _candidate(project_id, config_id, type_id, "app_heartbeat_v1", now),
                _candidate(project_id, config_id, type_id, "old_ping", now),
                _candidate(project_id, config_id, type_id, "still_unmapped", now),
            ]
        )
        session.commit()

        assert resolve_held_candidates(session, config_id, now=now) == 1
        session.commit()
        rows = {
            c.event_name: c for c in session.execute(select(ShadowEventCandidate)).scalars().all()
        }
        assert rows["app_heartbeat_v1"].status == SHADOW_STATUS_ACCEPTED
        assert rows["app_heartbeat_v1"].accepted_event_id == held.id
        assert rows["app_heartbeat_v1"].resolved_by is None
        # An archived event is no answer; the inbox hides that one its own way.
        assert rows["old_ping"].status == SHADOW_STATUS_NEW
        assert rows["still_unmapped"].status == SHADOW_STATUS_NEW
        # Idempotent: nothing left to resolve.
        assert resolve_held_candidates(session, config_id, now=now) == 0


async def _project_and_type(client: AsyncClient, slug: str) -> tuple[uuid.UUID, str]:
    await client.post("/api/v1/projects", json={"name": "V5 shadow", "slug": slug})
    et = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "pv", "display_name": "Page"}
    )
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
    return project_id, et.json()["id"]


async def _async_config_and_candidate(project_id: uuid.UUID, type_id: str, name: str) -> uuid.UUID:
    async with TestSessionLocal() as session:
        ds = DataSource(
            name=f"wh-{uuid.uuid4().hex[:8]}",
            db_type="clickhouse",
            host="localhost",
            port=9000,
            database_name="db",
            username="u",
        )
        session.add(ds)
        await session.flush()
        config = ScanConfig(
            project_id=project_id, data_source_id=ds.id, name="scan", base_query="SELECT 1"
        )
        session.add(config)
        await session.flush()
        now = datetime.now(UTC)
        candidate = _candidate(project_id, config.id, uuid.UUID(type_id), name, now)
        session.add(candidate)
        await session.commit()
        return candidate.id


@pytest.mark.asyncio
async def test_the_inbox_hides_a_held_candidate_and_accept_links_it(
    client: AsyncClient,
) -> None:
    slug = "v5-shadow-link"
    project_id, type_id = await _project_and_type(client, slug)
    candidate_id = await _async_config_and_candidate(project_id, type_id, "late_name")

    listed = await client.get(f"/api/v1/projects/{slug}/reconciliation/shadow-events")
    assert listed.status_code == 200
    assert [row["event_name"] for row in listed.json()["items"]] == ["late_name"]

    # The identity is taken after the candidate was collected (an authored event
    # adopts its name as identity).
    created = await client.post(
        f"/api/v1/projects/{slug}/events", json={"event_type_id": type_id, "name": "late_name"}
    )
    assert created.status_code in (200, 201)
    event_id = created.json()["id"]

    listed = await client.get(f"/api/v1/projects/{slug}/reconciliation/shadow-events")
    assert listed.status_code == 200
    assert listed.json()["items"] == []
    assert listed.json()["new_count"] == 0

    accepted = await client.post(
        f"/api/v1/projects/{slug}/reconciliation/shadow-events/{candidate_id}/accept",
        json={},
    )
    assert accepted.status_code == 200
    assert accepted.json()["event_id"] == event_id
    assert accepted.json()["status"] == SHADOW_STATUS_ACCEPTED

    events = await client.get(f"/api/v1/projects/{slug}/events")
    assert events.status_code == 200
    names = [item["name"] for item in events.json()["items"]]
    assert names.count("late_name") == 1  # linked, not duplicated


# ── The flaky demo sink ──────────────────────────────────────────────────────


def test_a_flaky_demo_sink_fails_its_first_attempt_then_delivers(
    sync_session_factory: sessionmaker[Session], monkeypatch: MonkeyPatch
) -> None:
    now = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
    with sync_session_factory() as session:
        project_id, config_id, type_id = _seed_config(session, now=now, stale_hours=1)
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project_id,
            type=AlertDestinationType.demo_sink.value,
            name="Local demo sink (flaky)",
            enabled=True,
            simulate_first_attempt_failure=True,
        )
        session.add(destination)
        session.flush()
        rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=destination.id,
            name="Spike & drift watch (demo)",
            enabled=True,
            message_format="plain",
        )
        session.add(rule)
        session.flush()
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project_id,
            scan_config_id=config_id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel=AlertDestinationType.demo_sink.value,
            status=AlertDeliveryStatus.pending.value,
            matched_count=1,
            payload_snapshot={},
        )
        session.add(delivery)
        session.flush()
        session.add(
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type="event_type",
                scope_ref=str(type_id),
                scope_name="Screen view",
                bucket=now - _HOUR,
                direction="spike",
                actual_count=900,
                expected_count=300,
                absolute_delta=600,
                percent_delta=200,
            )
        )
        session.commit()
        delivery_id = str(delivery.id)

    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__, "_get_sync_session", sync_session_factory
    )
    first = alerts_task.send_alert_delivery.run(delivery_id)
    assert first["status"] == "failed"
    with sync_session_factory() as session:
        row = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert row is not None
        assert row.status == AlertDeliveryStatus.failed.value
        assert row.error_message == alerts_task.SIMULATED_FAILURE_MESSAGE
        # The reaper leaves it alone: the demo's point is the Retry button.
        assert not is_transient_send_error(row.error_message)
        # What Retry does before it re-enqueues.
        row.status = AlertDeliveryStatus.pending.value
        row.error_message = None
        row.dispatch_attempts = 0
        session.commit()

    second = alerts_task.send_alert_delivery.run(delivery_id)
    assert second["status"] == "sent"
    with sync_session_factory() as session:
        row = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert row is not None
        assert row.status == AlertDeliveryStatus.sent.value
        assert row.payload_snapshot is not None
        assert row.payload_snapshot["is_local"] is True
