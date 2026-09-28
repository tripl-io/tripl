"""Worker-side notification producers (#259): signals, lifecycle findings, branch merge.

The signal source is driven through ``produce_notifications(session, "signals",
config)`` against a file-backed SQLite database, with the two "open signal"
readers it shares with alert dispatch replaced by fixed anomaly rows — which
signals are open is ``metrics.signals``' business and has its own suite; what
is pinned here is who hears about them: event and event-type watchers, catalog
metric watchers, never a non-member or a muted watcher, never a triaged signal,
one notification per anomaly per subscriber, the 6h throttle, and that a
failure never escapes. The lifecycle source gets the same treatment, plus the
``check_lifecycle_findings`` hand-off. The ``branch_merged`` / ``branch_approved``
hooks and the merge re-keying a branch-only event's watchers onto main are
exercised through the API.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.notification import Notification
from tripl.models.plan_branch_reviewer import PlanBranchReviewer
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.scan_config import ScanConfig
from tripl.models.signal_triage import SignalTriage
from tripl.models.subscription import Subscription
from tripl.models.user import User
from tripl.services import subscription_service
from tripl.tests._members import add_member_by_slug
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_event_comment_merge_batch2 import _create_on_branch, _event_id
from tripl.tests.test_plan_branches import _approve_and_merge, _create_branch, _seed_plan
from tripl.worker.tasks import lifecycle, notification_producers
from tripl.worker.tasks.lifecycle import LifecycleSweepStats
from tripl.worker.tasks.metrics import signals as metric_signals

BUCKET = datetime(2026, 9, 27, 9, tzinfo=UTC)


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'producers.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@dataclass(frozen=True)
class World:
    project_id: uuid.UUID
    config_id: uuid.UUID
    type_id: uuid.UUID
    event_id: uuid.UUID
    metric_id: uuid.UUID
    anna: uuid.UUID  # watches the event
    oleg: uuid.UUID  # watches the event type (an owner)
    ivan: uuid.UUID  # watches the event, muted
    gone: uuid.UUID  # watches the event, not a member any more
    mia: uuid.UUID  # watches the metric


def _user(session: Session, email: str, name: str) -> uuid.UUID:
    user = User(id=uuid.uuid4(), email=email, name=name, password_hash="x", role="editor")
    session.add(user)
    session.flush()
    return user.id


def _watch(
    session: Session,
    world_project: uuid.UUID,
    user_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    *,
    muted: bool = False,
    reason: str = "manual",
) -> None:
    session.add(
        Subscription(
            user_id=user_id,
            project_id=world_project,
            entity_type=entity_type,
            entity_id=entity_id,
            reasons=[reason],
            muted=muted,
        )
    )


def _world(session: Session) -> World:
    project = Project(id=uuid.uuid4(), name="Shop", slug="shop")
    data_source = DataSource(
        id=uuid.uuid4(),
        name="DS",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    session.add_all([project, data_source])
    session.flush()
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    event_type = EventType(id=uuid.uuid4(), project_id=project.id, name="page", display_name="Page")
    session.add_all([config, event_type])
    session.flush()
    event = Event(
        id=uuid.uuid4(), project_id=project.id, event_type_id=event_type.id, name="page_view"
    )
    session.add(event)
    anna = _user(session, "anna@example.com", "Anna")
    oleg = _user(session, "oleg@example.com", "Oleg")
    ivan = _user(session, "ivan@example.com", "Ivan")
    gone = _user(session, "gone@example.com", "Gone")
    mia = _user(session, "mia@example.com", "Mia")
    for member in (anna, oleg, ivan, mia):
        session.add(ProjectMember(project_id=project.id, user_id=member, role="editor"))
    metric = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name="conv",
        display_name="Conversions",
        kind=MetricKind.fact.value,
        aggregation=MetricAggregation.count.value,
        composition=MetricComposition.single.value,
        config={},
        interval="1h",
        status=MetricStatus.active.value,
    )
    session.add(metric)
    session.flush()
    _watch(session, project.id, anna, "event", event.id, reason="author")
    _watch(session, project.id, oleg, "event_type", event_type.id, reason="owner")
    _watch(session, project.id, ivan, "event", event.id, muted=True)
    _watch(session, project.id, gone, "event", event.id)
    _watch(session, project.id, mia, "metric", metric.id)
    session.commit()
    return World(
        project_id=project.id,
        config_id=config.id,
        type_id=event_type.id,
        event_id=event.id,
        metric_id=metric.id,
        anna=anna,
        oleg=oleg,
        ivan=ivan,
        gone=gone,
        mia=mia,
    )


def _anomaly(
    session: Session,
    world: World,
    scope_type: str,
    *,
    bucket: datetime = BUCKET,
) -> MetricAnomaly:
    scope_ref = {
        "event": str(world.event_id),
        "event_type": str(world.type_id),
        "metric": str(world.metric_id),
        "project_total": str(world.config_id),
    }[scope_type]
    row = MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=None if scope_type == "metric" else world.config_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        event_id=world.event_id if scope_type == "event" else None,
        event_type_id=world.type_id if scope_type in ("event", "event_type") else None,
        bucket=bucket,
        actual_count=12,
        expected_count=40,
        stddev=4,
        z_score=-7,
        direction="drop",
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def open_signals(monkeypatch: pytest.MonkeyPatch) -> list[MetricAnomaly]:
    """The anomalies the two "latest active" readers report as open."""
    rows: list[MetricAnomaly] = []

    def _scan(_session: Session, _config: ScanConfig) -> dict[tuple[str, str], MetricAnomaly]:
        return {(str(r.scope_type), r.scope_ref): r for r in rows if r.scope_type != "metric"}

    def _metric(_session: Session, _config: ScanConfig) -> dict[tuple[str, str], MetricAnomaly]:
        return {(str(r.scope_type), r.scope_ref): r for r in rows if r.scope_type == "metric"}

    monkeypatch.setattr(metric_signals, "_get_latest_active_anomalies", _scan)
    monkeypatch.setattr(metric_signals, "_get_active_metric_anomaly_candidates", _metric)
    return rows


def _produce_signals(session: Session, world: World) -> int:
    config = session.get(ScanConfig, world.config_id)
    assert config is not None
    return notification_producers.produce_notifications(session, "signals", config)


def _notes(session: Session, **filters: object) -> list[Notification]:
    stmt = select(Notification).order_by(Notification.created_at, Notification.id)
    for column, value in filters.items():
        stmt = stmt.where(getattr(Notification, column) == value)
    return list(session.execute(stmt).scalars())


# --- signals ----------------------------------------------------------------------------


def test_event_signal_reaches_event_and_type_watchers_only(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly]
) -> None:
    with factory() as session:
        world = _world(session)
        open_signals.append(_anomaly(session, world, "event"))

        assert _produce_signals(session, world) == 2

        notes = _notes(session, kind="signal")
        # Anna watches the event, Oleg its type. Ivan muted it, Gone left.
        assert {note.user_id for note in notes} == {world.anna, world.oleg}
        note = notes[0]
        assert note.entity_type == "event"
        assert note.entity_id == world.event_id
        assert note.title == "Signal: drop on page_view"
        assert note.url == (
            f"/o/default/p/shop/monitoring/event/{world.event_id}"
            f"?{notification_producers.SIGNAL_URL_PARAM}=2026-09-27T09:00:00Z"
        )
        assert note.emailed_at is None and note.read_at is None


def test_metric_and_event_type_signals_reach_their_watchers(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly]
) -> None:
    with factory() as session:
        world = _world(session)
        open_signals.append(_anomaly(session, world, "metric"))
        open_signals.append(_anomaly(session, world, "event_type"))
        # The project total has nobody watching it.
        open_signals.append(_anomaly(session, world, "project_total"))

        _produce_signals(session, world)

        by_entity = {(n.entity_type, n.user_id) for n in _notes(session, kind="signal")}
        assert by_entity == {("metric", world.mia), ("event_type", world.oleg)}


def test_the_same_anomaly_notifies_once_even_past_the_throttle(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly]
) -> None:
    with factory() as session:
        world = _world(session)
        open_signals.append(_anomaly(session, world, "event"))
        _produce_signals(session, world)
        # Age every row past the 6h throttle: the re-scored anomaly is still known.
        session.execute(
            update(Notification).values(created_at=datetime.now(UTC) - timedelta(hours=7))
        )
        session.commit()

        assert _produce_signals(session, world) == 0
        assert len(_notes(session, kind="signal")) == 2


def test_a_pre_upgrade_org_less_url_still_dedupes_the_signal(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly]
) -> None:
    """Rows written before F20 PR8 carry ``/p/{slug}/...``; the first run after
    the upgrade must not re-announce every open signal to their readers."""
    with factory() as session:
        world = _world(session)
        open_signals.append(_anomaly(session, world, "event"))
        _produce_signals(session, world)
        notes = _notes(session, kind="signal")
        assert notes and all(note.url.startswith("/o/default/p/shop/") for note in notes)
        # Rewrite them into the pre-upgrade shape, past the 6h throttle.
        for note in notes:
            note.url = note.url.removeprefix("/o/default")
            note.created_at = datetime.now(UTC) - timedelta(hours=7)
        session.commit()

        assert _produce_signals(session, world) == 0
        assert len(_notes(session, kind="signal")) == 2


def test_the_same_anomaly_is_announced_again_after_a_week(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly]
) -> None:
    """The per-anomaly dedup is bounded (``ALREADY_TOLD_WINDOW``): a signal
    still open a week later is worth one more reminder."""
    with factory() as session:
        world = _world(session)
        open_signals.append(_anomaly(session, world, "event"))
        _produce_signals(session, world)
        session.execute(
            update(Notification).values(
                created_at=datetime.now(UTC)
                - notification_producers.ALREADY_TOLD_WINDOW
                - timedelta(hours=1)
            )
        )
        session.commit()

        assert _produce_signals(session, world) == 2
        assert len(_notes(session, kind="signal")) == 4


def test_a_new_anomaly_is_throttled_for_six_hours_per_entity(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly]
) -> None:
    with factory() as session:
        world = _world(session)
        open_signals.append(_anomaly(session, world, "event"))
        _produce_signals(session, world)

        open_signals.clear()
        open_signals.append(_anomaly(session, world, "event", bucket=BUCKET + timedelta(hours=1)))
        assert _produce_signals(session, world) == 0

        session.execute(
            update(Notification).values(created_at=datetime.now(UTC) - timedelta(hours=7))
        )
        session.commit()
        assert _produce_signals(session, world) == 2
        assert len(_notes(session, kind="signal")) == 4


@pytest.mark.parametrize("action", ["acknowledged", "expected", "false_positive", "muted"])
def test_triaged_signals_never_notify(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly], action: str
) -> None:
    with factory() as session:
        world = _world(session)
        anomaly = _anomaly(session, world, "event")
        open_signals.append(anomaly)
        session.add(
            SignalTriage(
                project_id=world.project_id,
                scan_config_id=world.config_id,
                scope_type="event",
                scope_ref=str(world.event_id),
                action=action,
                bucket=None if action == "muted" else BUCKET,
            )
        )
        session.commit()

        assert _produce_signals(session, world) == 0
        assert _notes(session) == []


def test_a_lapsed_mute_no_longer_hides_the_signal(
    factory: sessionmaker[Session], open_signals: list[MetricAnomaly]
) -> None:
    with factory() as session:
        world = _world(session)
        open_signals.append(_anomaly(session, world, "event"))
        session.add(
            SignalTriage(
                project_id=world.project_id,
                scan_config_id=world.config_id,
                scope_type="event",
                scope_ref=str(world.event_id),
                action="muted",
                bucket=None,
                muted_until=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        session.commit()

        assert _produce_signals(session, world) == 2


def test_a_failing_producer_never_raises_and_writes_nothing(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> dict[tuple[str, str], MetricAnomaly]:
        raise RuntimeError("signals unavailable")

    monkeypatch.setattr(metric_signals, "_get_latest_active_anomalies", _boom)
    with factory() as session:
        world = _world(session)
        assert _produce_signals(session, world) == 0
        assert _notes(session) == []
        # The session is still usable by the caller afterwards.
        assert session.get(Project, world.project_id) is not None


def test_unknown_source_is_a_no_op(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        assert notification_producers.produce_notifications(session, "nope", None) == 0


# --- lifecycle findings --------------------------------------------------------------


def test_lifecycle_finding_reaches_event_watchers(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        world = _world(session)

        written = notification_producers.produce_notifications(
            session, "lifecycle", [(world.project_id, world.event_id, "sunset_overdue")]
        )

        assert written == 2
        notes = _notes(session, kind="lifecycle")
        assert {note.user_id for note in notes} == {world.anna, world.oleg}
        assert notes[0].title == "Lifecycle: page_view is past its sunset and still receives data"
        assert notes[0].url == f"/o/default/p/shop/events/detail/{world.event_id}"


def test_the_lifecycle_sweep_hands_its_opened_findings_over(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with factory() as session:
        world = _world(session)
    opened = [(world.project_id, world.event_id, "successor_silent")]

    def _compute(_session: Session, *, now: datetime) -> LifecycleSweepStats:
        stats = LifecycleSweepStats(opened=1)
        stats.opened_findings.extend(opened)
        return stats

    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(lifecycle, "_get_sync_session", factory)
    monkeypatch.setattr(lifecycle, "compute_lifecycle_findings", _compute)
    monkeypatch.setattr(
        lifecycle,
        "produce_notifications",
        lambda _session, source, subject: seen.append((source, subject)) or 0,
    )

    result = lifecycle.check_lifecycle_findings.run()

    assert result["opened"] == 1
    assert "opened_findings" not in str(result)
    assert seen == [("lifecycle", opened)]


def test_a_failing_producer_never_fails_the_lifecycle_sweep(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a producer that breaks its never-raise contract cannot fail the sweep."""
    with factory() as session:
        world = _world(session)

    def _compute(_session: Session, *, now: datetime) -> LifecycleSweepStats:
        stats = LifecycleSweepStats(opened=1)
        stats.opened_findings.append((world.project_id, world.event_id, "sunset_overdue"))
        return stats

    def _boom(_session: Session, _source: str, _subject: object) -> int:
        raise RuntimeError("notifications unavailable")

    monkeypatch.setattr(lifecycle, "_get_sync_session", factory)
    monkeypatch.setattr(lifecycle, "compute_lifecycle_findings", _compute)
    monkeypatch.setattr(lifecycle, "produce_notifications", _boom)

    result = lifecycle.check_lifecycle_findings.run()

    assert result["opened"] == 1


# --- branch merged (API) ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_notifies_reviewers_but_not_the_merger(client: AsyncClient) -> None:
    slug = "notify-merge"
    project = await client.post(
        "/api/v1/projects", json={"name": slug, "slug": slug, "description": ""}
    )
    assert project.status_code == 201
    created = await client.post(f"/api/v1/projects/{slug}/branches", json={"name": "feature"})
    assert created.status_code == 201
    branch_id = uuid.UUID(created.json()["id"])

    reviewer_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        session.add_all(
            [
                User(
                    id=reviewer_id,
                    email="merge-reviewer@example.com",
                    password_hash="!seed",
                    role="editor",
                ),
                User(
                    id=outsider_id,
                    email="merge-outsider@example.com",
                    password_hash="!seed",
                    role="editor",
                ),
            ]
        )
        await session.commit()
    await add_member_by_slug(slug, "merge-reviewer@example.com", "editor")
    async with TestSessionLocal() as session:
        # Seeded directly: this test is about the merge hook, not add_reviewer.
        session.add(PlanBranchReviewer(branch_id=branch_id, user_id=reviewer_id))
        # A reviewer row for someone who is not a project member.
        session.add(PlanBranchReviewer(branch_id=branch_id, user_id=outsider_id))
        await session.commit()

    for action in ("submit", "approve"):
        resp = await client.post(
            f"/api/v1/projects/{slug}/branches/{branch_id}/transition", json={"action": action}
        )
        assert resp.status_code == 200
    merged = await client.post(f"/api/v1/projects/{slug}/branches/{branch_id}/merge")
    assert merged.status_code == 200

    async with TestSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(Notification).where(Notification.kind == "branch_merged")
                )
            )
            .scalars()
            .all()
        )
    assert [row.user_id for row in rows] == [reviewer_id]
    assert rows[0].entity_type == "branch"
    assert rows[0].entity_id == branch_id
    assert rows[0].url == f"/o/default/p/{slug}/branches/{branch_id}"
    assert rows[0].title.endswith("merged branch feature into main")

    # The approval reached the same audience as the merge: the reviewer, not
    # the approver (who is also the author here) and not the non-member.
    async with TestSessionLocal() as session:
        approved = (
            (
                await session.execute(
                    select(Notification.user_id).where(Notification.kind == "branch_approved")
                )
            )
            .scalars()
            .all()
        )
    assert approved == [reviewer_id]


# --- subscriptions follow a branch-only event to main ----------------------------------


def test_rekeying_merges_into_an_existing_subscription(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        world = _world(session)
        branch_copy = Event(
            id=uuid.uuid4(),
            project_id=world.project_id,
            event_type_id=world.type_id,
            name="page_view_branch",
        )
        session.add(branch_copy)
        session.flush()
        # Anna watches both rows; Mia only the branch row, muted.
        _watch(session, world.project_id, world.anna, "event", branch_copy.id, reason="commenter")
        _watch(session, world.project_id, world.mia, "event", branch_copy.id, muted=True)
        session.commit()

        subscription_service.rekey_event_subscriptions_sync(
            session, target_by_event_id={branch_copy.id: world.event_id}
        )
        session.commit()

        rows = {
            (row.user_id, row.entity_id): row
            for row in session.execute(select(Subscription)).scalars()
        }
        assert not any(entity_id == branch_copy.id for _user, entity_id in rows)
        anna = rows[(world.anna, world.event_id)]
        assert anna.reasons == ["author", "commenter"]
        assert anna.muted is False
        mia = rows[(world.mia, world.event_id)]
        assert mia.reasons == ["manual"]
        assert mia.muted is True


@pytest.mark.asyncio
async def test_merge_moves_a_branch_only_events_watchers_to_main(client: AsyncClient) -> None:
    slug = "notify-rekey"
    await _seed_plan(client, slug)
    branch_id = await _create_branch(client, slug)
    quiet_id = await _create_on_branch(client, slug, branch_id, "quiet:event")
    talked_id = await _create_on_branch(client, slug, branch_id, "talked:event")
    comment = await client.post(
        f"/api/v1/projects/{slug}/events/{talked_id}/comments", json={"body": "why?"}
    )
    assert comment.status_code == 201, comment.text

    async def _event_subscriptions(event_id: str) -> list[Subscription]:
        async with TestSessionLocal() as session:
            return list(
                (
                    await session.execute(
                        select(Subscription).where(
                            Subscription.entity_type == "event",
                            Subscription.entity_id == uuid.UUID(event_id),
                        )
                    )
                )
                .scalars()
                .all()
            )

    # No main twin yet: the author's watch is kept under the branch rows.
    assert [row.reasons for row in await _event_subscriptions(quiet_id)] == [["author"]]
    assert [row.reasons for row in await _event_subscriptions(talked_id)] == [
        ["author", "commenter"]
    ]

    merged = await _approve_and_merge(client, slug, branch_id)
    assert merged.status_code == 200, merged.text

    for name, branch_row, reasons in (
        ("quiet:event", quiet_id, ["author"]),
        ("talked:event", talked_id, ["author", "commenter"]),
    ):
        main_id = await _event_id(client, slug, name)
        assert main_id != branch_row
        assert await _event_subscriptions(branch_row) == []
        assert [row.reasons for row in await _event_subscriptions(main_id)] == [reasons]
