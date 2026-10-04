import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_ACCEPTED,
    SHADOW_STATUS_DISMISSED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.tests.conftest import TestSessionLocal

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation_metrics.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


async def _setup_project(client: AsyncClient, slug: str) -> tuple[str, str]:
    """Create project + event type + one event; return (event_type_id, event_id)."""
    await client.post("/api/v1/projects", json={"name": "R", "slug": slug})
    et_resp = await client.post(
        f"/api/v1/projects/{slug}/event-types",
        json={"name": "pv", "display_name": "Page View"},
    )
    et_id = et_resp.json()["id"]
    ev_resp = await client.post(
        f"/api/v1/projects/{slug}/events",
        json={"event_type_id": et_id, "name": "Known Event"},
    )
    return et_id, ev_resp.json()["id"]


async def _project_id(slug: str) -> uuid.UUID:
    from tripl.models.project import Project

    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
        return project_id


async def _insert_scan_config(
    project_id: uuid.UUID,
    *,
    event_name_format: str | None = None,
    event_type_id: uuid.UUID | None = None,
) -> uuid.UUID:
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
            project_id=project_id,
            data_source_id=ds.id,
            event_type_id=event_type_id,
            name="main scan",
            base_query="SELECT 1",
            event_name_format=event_name_format,
        )
        session.add(config)
        await session.commit()
        return config.id


async def _insert_candidate(
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID,
    *,
    event_name: str = "shadow | one",
    event_type_id: uuid.UUID | None = None,
    status: str = SHADOW_STATUS_NEW,
    observed_count: int = 42,
) -> uuid.UUID:
    async with TestSessionLocal() as session:
        candidate = ShadowEventCandidate(
            project_id=project_id,
            scan_config_id=scan_config_id,
            event_type_id=event_type_id,
            event_name=event_name,
            observed_count=observed_count,
            first_seen_at=NOW - timedelta(days=2),
            last_seen_at=NOW,
            status=status,
        )
        session.add(candidate)
        await session.commit()
        return candidate.id


# --- shadow events inbox ---


@pytest.mark.asyncio
async def test_list_shadow_events_orders_by_volume_and_counts_new(client: AsyncClient):
    et_id, _ = await _setup_project(client, "rec-list")
    project_id = await _project_id("rec-list")
    scan_id = await _insert_scan_config(project_id)
    await _insert_candidate(
        project_id, scan_id, event_name="small", observed_count=5, event_type_id=uuid.UUID(et_id)
    )
    await _insert_candidate(project_id, scan_id, event_name="big", observed_count=500)
    await _insert_candidate(project_id, scan_id, event_name="gone", status=SHADOW_STATUS_DISMISSED)

    resp = await client.get("/api/v1/projects/rec-list/reconciliation/shadow-events?status=new")
    assert resp.status_code == 200
    data = resp.json()
    assert [item["event_name"] for item in data["items"]] == ["big", "small"]
    assert data["new_count"] == 2
    assert data["total"] == 2
    # display_name, not the internal key: Reconciliation used to be the one
    # surface that showed "pv" where every other one showed "Page View".
    assert data["items"][1]["event_type_name"] == "Page View"
    assert data["items"][0]["scan_config_name"] == "main scan"


@pytest.mark.asyncio
async def test_accept_shadow_event_creates_event_with_source_identity(client: AsyncClient):
    et_id, _ = await _setup_project(client, "rec-accept")
    project_id = await _project_id("rec-accept")
    scan_id = await _insert_scan_config(project_id)
    candidate_id = await _insert_candidate(
        project_id,
        scan_id,
        event_name="screen | checkout",
        event_type_id=uuid.UUID(et_id),
    )

    resp = await client.post(
        f"/api/v1/projects/rec-accept/reconciliation/shadow-events/{candidate_id}/accept",
        json={"name": "Checkout Screen"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"

    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(data["event_id"]))
        assert event is not None
        assert event.name == "Checkout Screen"
        assert event.source_name == "screen | checkout"
        assert event.status == "live"
        candidate = await session.get(ShadowEventCandidate, candidate_id)
        assert candidate is not None
        assert candidate.status == SHADOW_STATUS_ACCEPTED
        assert candidate.accepted_event_id == event.id
        assert candidate.resolved_by is not None


@pytest.mark.asyncio
async def test_accept_shadow_event_works_when_a_scan_rule_names_the_event_type(
    client: AsyncClient,
):
    """Every production event type is governed by a name format; accept must survive one.

    A candidate carries no field values, so routing it through the name
    generator could only ever report every placeholder missing — the accept
    422'd on any rule-governed type until the identity was passed in instead.
    No test above seeds an ``event_name_format``, which is
    exactly why nothing caught it.

    The rule is BOUND to the event type, which is what makes the seed governing
    and this test a fang rather than a decoration. An unbound config used to
    govern the whole project, so ``event_name_format`` alone was enough; since
    tripl-0zpq.254 an unbound config with no ``event_type_column`` governs
    nothing, and this seeded exactly that — the type came out unruled, the 422
    below became unreachable, and reverting the fix left the test green.

    Reverting it now reddens ``assert resp.status_code == 200`` at the bottom:
    drop ``scan_identity=candidate.event_name`` from the ``create_event`` call
    in ``reconciliation_service.accept_shadow_event`` (restoring the post-hoc
    ``event.source_name = candidate.event_name``) and the accept routes through
    ``_generate_scan_template_name``, which resolves ``{action}`` against a
    candidate that has no field values and raises 422 "fill field values for:
    action".
    """
    et_id, _ = await _setup_project(client, "rec-ruled")
    project_id = await _project_id("rec-ruled")
    await client.post(
        f"/api/v1/projects/rec-ruled/event-types/{et_id}/fields",
        json={"name": "action", "display_name": "Action", "field_type": "string"},
    )
    scan_id = await _insert_scan_config(
        project_id, event_name_format="{action}", event_type_id=uuid.UUID(et_id)
    )
    candidate_id = await _insert_candidate(
        project_id, scan_id, event_name="wind_alert_created", event_type_id=uuid.UUID(et_id)
    )

    resp = await client.post(
        f"/api/v1/projects/rec-ruled/reconciliation/shadow-events/{candidate_id}/accept",
        json={"name": "Wind alert created"},
    )
    assert resp.status_code == 200, resp.text

    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(resp.json()["event_id"]))
        assert event is not None
        # The operator's name survives; the identity is the observed one, not a
        # template applied to values the candidate never had.
        assert event.name == "Wind alert created"
        assert event.source_name == "wind_alert_created"


@pytest.mark.asyncio
async def test_accept_requires_event_type_when_not_detected(client: AsyncClient):
    await _setup_project(client, "rec-no-et")
    project_id = await _project_id("rec-no-et")
    scan_id = await _insert_scan_config(project_id)
    candidate_id = await _insert_candidate(project_id, scan_id, event_type_id=None)

    resp = await client.post(
        f"/api/v1/projects/rec-no-et/reconciliation/shadow-events/{candidate_id}/accept",
        json={},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_accept_conflicts_on_resolved_candidate_and_duplicate_identity(
    client: AsyncClient,
):
    et_id, _ = await _setup_project(client, "rec-conflict")
    project_id = await _project_id("rec-conflict")
    scan_id = await _insert_scan_config(project_id)
    candidate_id = await _insert_candidate(
        project_id, scan_id, event_name="dup | name", event_type_id=uuid.UUID(et_id)
    )

    first = await client.post(
        f"/api/v1/projects/rec-conflict/reconciliation/shadow-events/{candidate_id}/accept",
        json={},
    )
    assert first.status_code == 200

    # Already resolved → 409.
    again = await client.post(
        f"/api/v1/projects/rec-conflict/reconciliation/shadow-events/{candidate_id}/accept",
        json={},
    )
    assert again.status_code == 409

    # The same identity surfacing via another scan config → duplicate
    # source_name 409 (an event with that identity already exists).
    other_scan_id = await _insert_scan_config(project_id)
    other_id = await _insert_candidate(
        project_id, other_scan_id, event_name="dup | name", event_type_id=uuid.UUID(et_id)
    )
    dup = await client.post(
        f"/api/v1/projects/rec-conflict/reconciliation/shadow-events/{other_id}/accept",
        json={},
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_accept_answers_a_lost_identity_race_with_the_create_409(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Both of the accept's probes are SELECTs; ``uq_event_scan_identity`` closes the gap.

    A competing create that commits the candidate's identity after the probes
    and before the INSERT used to reach the operator as a bare 500.
    The accept authors through ``create_event``, so it inherits that door's
    answer: 409, naming the winner — and the candidate stays ``new``, since the
    whole request rolled back.

    The in-memory database has one connection, so the competitor is written
    through the request's own session at the last read before the INSERT (the
    next-order lookup inside ``create_event``) and committed there, which is
    what another request's commit looks like from this one's point of view.
    """
    from tripl.services import event_service

    et_id, _ = await _setup_project(client, "rec-race")
    project_id = await _project_id("rec-race")
    scan_id = await _insert_scan_config(project_id)
    candidate_id = await _insert_candidate(
        project_id, scan_id, event_name="screen | checkout", event_type_id=uuid.UUID(et_id)
    )

    original = event_service._get_next_event_order
    landed: dict[str, uuid.UUID] = {}

    async def _racing(session: AsyncSession, project_id: uuid.UUID, branch_id: uuid.UUID) -> int:
        if "id" not in landed:
            competitor = Event(
                project_id=project_id,
                branch_id=branch_id,
                event_type_id=uuid.UUID(et_id),
                name="Checkout (won)",
                source_name="screen | checkout",
            )
            session.add(competitor)
            await session.commit()
            landed["id"] = competitor.id
        return await original(session, project_id, branch_id)

    monkeypatch.setattr(event_service, "_get_next_event_order", _racing)

    resp = await client.post(
        f"/api/v1/projects/rec-race/reconciliation/shadow-events/{candidate_id}/accept",
        json={"name": "Checkout Screen"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "'screen | checkout'" in detail
    assert str(landed["id"]) in detail

    async with TestSessionLocal() as session:
        holders = (
            (
                await session.execute(
                    select(Event.id).where(Event.source_name == "screen | checkout")
                )
            )
            .scalars()
            .all()
        )
        assert holders == [landed["id"]]
        candidate = await session.get(ShadowEventCandidate, candidate_id)
        assert candidate is not None
        assert candidate.status == SHADOW_STATUS_NEW
        assert candidate.accepted_event_id is None


@pytest.mark.asyncio
async def test_dismiss_shadow_event(client: AsyncClient):
    await _setup_project(client, "rec-dismiss")
    project_id = await _project_id("rec-dismiss")
    scan_id = await _insert_scan_config(project_id)
    candidate_id = await _insert_candidate(project_id, scan_id)

    resp = await client.post(
        f"/api/v1/projects/rec-dismiss/reconciliation/shadow-events/{candidate_id}/dismiss",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"

    again = await client.post(
        f"/api/v1/projects/rec-dismiss/reconciliation/shadow-events/{candidate_id}/dismiss",
    )
    assert again.status_code == 409


# --- dead events ---


@pytest.mark.asyncio
async def test_dead_events_reports_stale_implemented_events_only(client: AsyncClient):
    et_id, event_id = await _setup_project(client, "rec-dead")
    project_id = await _project_id("rec-dead")
    now = datetime.now(UTC)

    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        # Implemented long ago, last seen 60 days ago → dead.
        event.status = "live"
        event.created_at = now - timedelta(days=90)
        event.last_seen_at = now - timedelta(days=60)

        fresh = Event(
            project_id=project_id,
            branch_id=event.branch_id,
            event_type_id=uuid.UUID(et_id),
            name="Fresh Event",
            status="live",
            last_seen_at=now - timedelta(days=1),
        )
        fresh.created_at = now - timedelta(days=90)
        never_seen_old = Event(
            project_id=project_id,
            branch_id=event.branch_id,
            event_type_id=uuid.UUID(et_id),
            name="Never Seen Old",
            status="implemented",
            last_seen_at=None,
        )
        never_seen_old.created_at = now - timedelta(days=90)
        recent_unseen = Event(
            project_id=project_id,
            branch_id=event.branch_id,
            event_type_id=uuid.UUID(et_id),
            name="Recent Unseen",
            status="implemented",
            last_seen_at=None,
        )
        not_implemented = Event(
            project_id=project_id,
            branch_id=event.branch_id,
            event_type_id=uuid.UUID(et_id),
            name="Planned Only",
            status="draft",
            last_seen_at=None,
        )
        not_implemented.created_at = now - timedelta(days=90)
        session.add_all([fresh, never_seen_old, recent_unseen, not_implemented])
        await session.commit()

    resp = await client.get("/api/v1/projects/rec-dead/reconciliation/dead-events?days=30")
    assert resp.status_code == 200
    data = resp.json()
    names = [item["name"] for item in data["items"]]
    assert "Known Event" in names
    assert "Never Seen Old" in names
    assert "Fresh Event" not in names
    assert "Recent Unseen" not in names
    assert "Planned Only" not in names
    # Never-seen events sort first.
    assert names[0] == "Never Seen Old"


@pytest.mark.asyncio
async def test_dead_events_flags_a_stale_event_with_a_young_plan_row(client: AsyncClient):
    """A seen-then-quiet event is dead even if its plan row was written today.

    The grace period exists for events that have never been seen — a freshly
    authored event legitimately has no data yet. Applying it to events that DO
    have a last_seen_at hid genuinely stale instrumentation behind a young row
    and made every backdated demo event permanently unflaggable.
    """
    et_id, _ = await _setup_project(client, "rec-dead-young-row")
    project_id = await _project_id("rec-dead-young-row")
    now = datetime.now(UTC)

    async with TestSessionLocal() as session:
        branch_id = (
            await session.execute(
                select(Event.branch_id).where(Event.project_id == project_id).limit(1)
            )
        ).scalar_one()
        stale_but_new_row = Event(
            project_id=project_id,
            branch_id=branch_id,
            event_type_id=uuid.UUID(et_id),
            name="Subscription Cancelled",
            status="implemented",
            last_seen_at=now - timedelta(days=45),
        )
        # The plan row is two days old; the instrumentation has been quiet 45.
        stale_but_new_row.created_at = now - timedelta(days=2)
        session.add(stale_but_new_row)
        await session.commit()

    resp = await client.get(
        "/api/v1/projects/rec-dead-young-row/reconciliation/dead-events?days=30"
    )
    assert resp.status_code == 200
    assert "Subscription Cancelled" in [item["name"] for item in resp.json()["items"]]


# --- dead events: bulk archive ---


@pytest.mark.asyncio
async def test_archive_dead_events_marks_events_archived(client: AsyncClient):
    et_id, event_id = await _setup_project(client, "rec-archive")
    second = await client.post(
        "/api/v1/projects/rec-archive/events",
        json={"event_type_id": et_id, "name": "Stale Two"},
    )
    second_id = second.json()["id"]

    resp = await client.post(
        "/api/v1/projects/rec-archive/reconciliation/dead-events/archive",
        json={"event_ids": [event_id, second_id]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "archived"
    assert data["archived_count"] == 2
    assert set(data["event_ids"]) == {event_id, second_id}

    async with TestSessionLocal() as session:
        for eid in (event_id, second_id):
            event = await session.get(Event, uuid.UUID(eid))
            assert event is not None
            assert event.status == "archived"


@pytest.mark.asyncio
async def test_archive_dead_events_supports_deprecated_status(client: AsyncClient):
    _, event_id = await _setup_project(client, "rec-deprecate")

    resp = await client.post(
        "/api/v1/projects/rec-deprecate/reconciliation/dead-events/archive",
        json={"event_ids": [event_id], "status": "deprecated"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deprecated"

    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        assert event.status == "deprecated"


@pytest.mark.asyncio
async def test_archive_dead_events_dedupes_repeated_ids(client: AsyncClient):
    _, event_id = await _setup_project(client, "rec-archive-dupe")

    resp = await client.post(
        "/api/v1/projects/rec-archive-dupe/reconciliation/dead-events/archive",
        json={"event_ids": [event_id, event_id]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["archived_count"] == 1
    assert data["event_ids"] == [event_id]


@pytest.mark.asyncio
async def test_archive_dead_events_404_is_atomic_when_an_id_is_unknown(client: AsyncClient):
    _, event_id = await _setup_project(client, "rec-archive-404")

    resp = await client.post(
        "/api/v1/projects/rec-archive-404/reconciliation/dead-events/archive",
        json={"event_ids": [event_id, str(uuid.uuid4())]},
    )
    assert resp.status_code == 404

    # A missing id rejects the whole batch — the valid event stays untouched.
    async with TestSessionLocal() as session:
        event = await session.get(Event, uuid.UUID(event_id))
        assert event is not None
        assert event.status != "archived"


@pytest.mark.asyncio
async def test_archive_dead_events_rejects_non_terminal_status(client: AsyncClient):
    _, event_id = await _setup_project(client, "rec-archive-bad")

    resp = await client.post(
        "/api/v1/projects/rec-archive-bad/reconciliation/dead-events/archive",
        json={"event_ids": [event_id], "status": "live"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_archive_dead_events_requires_at_least_one_id(client: AsyncClient):
    await _setup_project(client, "rec-archive-empty")

    resp = await client.post(
        "/api/v1/projects/rec-archive-empty/reconciliation/dead-events/archive",
        json={"event_ids": []},
    )
    assert resp.status_code == 422


# --- coverage ---


@pytest.mark.asyncio
async def test_coverage_aggregates_buckets_and_summary(client: AsyncClient):
    await _setup_project(client, "rec-cov")
    project_id = await _project_id("rec-cov")
    scan_id = await _insert_scan_config(project_id)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    async with TestSessionLocal() as session:
        session.add_all(
            [
                CoverageMetric(
                    scan_config_id=scan_id,
                    bucket=now - timedelta(days=1),
                    total_count=100,
                    matched_count=80,
                ),
                CoverageMetric(
                    scan_config_id=scan_id,
                    bucket=now,
                    total_count=200,
                    matched_count=120,
                ),
                # Outside the window — ignored.
                CoverageMetric(
                    scan_config_id=scan_id,
                    bucket=now - timedelta(days=30),
                    total_count=999,
                    matched_count=1,
                ),
            ]
        )
        await session.commit()

    resp = await client.get("/api/v1/projects/rec-cov/reconciliation/coverage?days=14")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["summary"]["total_count"] == 300
    assert data["summary"]["matched_count"] == 200
    assert data["summary"]["coverage_pct"] == pytest.approx(66.67)


# --- worker end-to-end: collect_metrics writes coverage + shadow rows ---


def test_collect_metrics_records_coverage_and_shadow_candidates(
    sync_session_factory,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from tripl.core.adapters.base import ColumnInfo
    from tripl.core.analyzers.event_generator import GenerationResult
    from tripl.tests.test_metrics_tasks import _create_scan_config
    from tripl.worker.tasks.metrics import tasks as metrics

    with sync_session_factory() as session:
        config = _create_scan_config(session, with_event_type=True)
        assert config.event_type_id is not None
        login_event = Event(
            id=uuid.uuid4(),
            project_id=config.project_id,
            event_type_id=config.event_type_id,
            name="event_name=Login",
            description="",
            status="live",
        )
        session.add(login_event)
        session.commit()
        config_id = str(config.id)
        scan_config_id = config.id
        login_event_id = login_event.id

    class FakeAdapter:
        def test_connection(self) -> bool:
            return True

        def get_columns(self, base_query: str) -> list[ColumnInfo]:
            return [
                ColumnInfo(name="time", type_name="DateTime"),
                ColumnInfo(name="event_name", type_name="String"),
            ]

        def get_time_bucketed_counts(
            self,
            base_query: str,
            time_column: str,
            interval: str,
            regular_columns: list[str],
            json_columns: list[str],
            json_value_paths: dict[str, list[str]] | None,
            time_from: datetime,
            time_to: datetime,
            limit: int = 100000,
        ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
            return (
                ["event_name"],
                [],
                [
                    (datetime(2026, 1, 1, 10), "Login", 12),
                    (datetime(2026, 1, 1, 10), "Mystery", 30),
                ],
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(metrics, "_get_sync_session", sync_session_factory)
    monkeypatch.setattr(metrics, "_build_adapter", lambda ds: FakeAdapter())
    monkeypatch.setattr(
        metrics,
        "_resolve_collection_window",
        lambda *args, **kwargs: (datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11), False),
    )
    monkeypatch.setattr(metrics, "analyze_cardinality", lambda *args, **kwargs: object())

    def fake_generate_events(*args: object, **kwargs: object) -> GenerationResult:
        with sync_session_factory() as session:
            persisted_event = session.get(Event, login_event_id)
            assert persisted_event is not None
            return GenerationResult(
                columns_analyzed=1,
                col_meta={"event_name": {"is_json": False, "is_low": True}},
                events_by_name={"event_name=Login": persisted_event},
            )

    monkeypatch.setattr(metrics, "generate_events", fake_generate_events)

    metrics.collect_metrics.run(config_id)

    session: Session
    with sync_session_factory() as session:
        coverage = session.execute(
            select(CoverageMetric).where(CoverageMetric.scan_config_id == scan_config_id)
        ).scalar_one()
        assert coverage.total_count == 42
        assert coverage.matched_count == 12

        candidate = session.execute(
            select(ShadowEventCandidate).where(
                ShadowEventCandidate.scan_config_id == scan_config_id
            )
        ).scalar_one()
        assert candidate.event_name == "event_name=Mystery"
        assert candidate.observed_count == 30
        assert candidate.status == SHADOW_STATUS_NEW


# --- worker upsert semantics ---


@pytest.mark.asyncio
async def test_shadow_upsert_does_not_resurrect_dismissed(client: AsyncClient):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tripl.models import Base
    from tripl.worker.tasks.metrics.metric_rows import _upsert_shadow_event_candidates

    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        sync_session = sessionmaker(engine)()

        project_id = uuid.uuid4()
        scan_id = uuid.uuid4()
        first_rows = [
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "scan_config_id": scan_id,
                "event_type_id": None,
                "event_name": "shadow | x",
                "observed_count": 10,
                "first_seen_at": NOW - timedelta(days=3),
                "last_seen_at": NOW - timedelta(days=1),
                "status": SHADOW_STATUS_NEW,
            }
        ]
        _upsert_shadow_event_candidates(sync_session, rows=first_rows)
        sync_session.commit()

        candidate = sync_session.execute(select(ShadowEventCandidate)).scalar_one()
        candidate.status = SHADOW_STATUS_DISMISSED
        sync_session.commit()

        # Re-collection: count refreshes, last_seen never rewinds, status survives.
        second_rows = [
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "scan_config_id": scan_id,
                "event_type_id": None,
                "event_name": "shadow | x",
                "observed_count": 7,
                "first_seen_at": NOW - timedelta(days=10),
                "last_seen_at": NOW - timedelta(days=5),
                "status": SHADOW_STATUS_NEW,
            }
        ]
        _upsert_shadow_event_candidates(sync_session, rows=second_rows)
        sync_session.commit()

        refreshed = sync_session.execute(select(ShadowEventCandidate)).scalar_one()
        assert refreshed.status == SHADOW_STATUS_DISMISSED
        assert refreshed.observed_count == 7
        # Stored without tz on sqlite; compare naive.
        assert refreshed.last_seen_at.replace(tzinfo=UTC) == NOW - timedelta(days=1)
        assert refreshed.first_seen_at.replace(tzinfo=UTC) == NOW - timedelta(days=3)
        sync_session.close()
    finally:
        engine.dispose()
