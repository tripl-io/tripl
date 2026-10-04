"""Regression tests for batch 16 of an earlier backend review (demo builders).

- The search builder must not commit mid-seed, and the cancel and sweep paths
  drop the audit trail of a demo that never became one.
- The planted dead event has no volume after its last sighting, is not
  backdated a further 30 days, and the synthetic warehouse cannot revive it.
- A new demo takes the lowest free name, not ``count + 1``.
- The MSTL detector, the PSI ladder and the synthetic adapter run off the event
  loop.
- One ``demo_sink`` local notice, shared by seeder and worker.
- The demo runtime retires the "Injected demo spike" marker with the anomaly it
  explains.
- Both ``SimulatedRuleFiring`` builders go through
  ``SimulatedRuleFiring.from_candidate``.

One further item in the batch corrected a code comment only and has no behaviour to pin.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

import tripl
from tripl.alert_templates import DEMO_SINK_LOCAL_NOTICE
from tripl.alerting_matching import DriftAlertCandidate
from tripl.core.adapters import synthetic
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.audit_log import AuditLog
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.domain_enums import MetricScopeType, ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.schemas.alerting import SimulatedRuleFiring
from tripl.services import demo_service
from tripl.services.demo.builders import alerts as alerts_builder
from tripl.services.demo.builders import catalog, governance, monitoring
from tripl.services.demo.builders.warehouse import (
    DEAD_EVENT_AGE_DAYS,
    DEAD_EVENT_NAME,
    SPIKE_ANNOTATION_LABEL,
)
from tripl.services.demo.scenario import DemoContext
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import demo_runtime


def _utc(value: datetime) -> datetime:
    """SQLite drops the zone on round-trip; compare everything as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ── search builder does not commit mid-seed ───────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_provision_commits_nothing_and_leaves_no_audit_trail(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seed stays one uncommitted transaction until the cancel check.

    ``build_search`` called ``reindex_project_branch`` with its default
    ``commit=True``, which committed the whole seed — ``demo_seed`` audit rows
    included — before ``create_demo_project`` looked at the cancel flag, so the
    rollback undid nothing and the trail outlived the discarded shell.
    """
    real_seed = demo_service._seed_demo_content
    commits: list[str] = []

    async def cancelling_seed(session: AsyncSession, **kwargs: object) -> None:
        real_commit = session.commit

        async def counting_commit() -> None:
            commits.append("commit")
            await real_commit()

        session.commit = counting_commit  # type: ignore[method-assign]
        try:
            await real_seed(session, **kwargs)  # type: ignore[arg-type]
        finally:
            del session.commit
        await session.execute(
            update(Project)
            .where(Project.id == kwargs["project_id"])
            .values(generation_stage=demo_service.DEMO_CANCEL_REQUESTED_STAGE)
        )

    monkeypatch.setattr(demo_service, "_seed_demo_content", cancelling_seed)

    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 409
    assert commits == [], "a demo builder committed inside the phase-2 transaction"

    async with TestSessionLocal() as session:
        entries = (await session.execute(select(AuditLog))).scalars().all()
    seeded = [entry for entry in entries if (entry.payload or {}).get("demo_seed")]
    assert seeded == []
    assert [entry for entry in entries if entry.target_type == "data_source"] == []


@pytest.mark.asyncio
async def test_sweeping_a_stalled_shell_drops_its_audit_trail() -> None:
    """A shell that never became a workspace keeps no orphaned trail."""
    async with TestSessionLocal() as session:
        stalled = Project(
            name="Demo Project",
            slug="demo-stalled",
            is_demo=True,
            generation_status=ProjectGenerationStatus.seeding.value,
            created_at=datetime.now(UTC) - timedelta(hours=demo_service.STALLED_SEEDING_HOURS + 1),
        )
        session.add(stalled)
        await session.flush()
        session.add(
            AuditLog(
                project_id=stalled.id,
                project_slug=stalled.slug,
                action="batch16.seeded",
                target_type="event",
            )
        )
        await session.commit()

    async with TestSessionLocal() as session:
        assert await demo_service._sweep_failed_demo_shells(session) == 1

    async with TestSessionLocal() as session:
        left = await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "batch16.seeded")
        )
    assert left == 0


# ── dead event stays dead ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_planted_dead_event_has_no_volume_after_it_died(client: AsyncClient) -> None:
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]

    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        events = (
            (
                await session.execute(
                    # The main plan's row: the one the warehouse series and the
                    # dead-events review are about.
                    select(Event)
                    .join(PlanBranch, PlanBranch.id == Event.branch_id)
                    .where(
                        Event.project_id == project_id,
                        Event.name == DEAD_EVENT_NAME,
                        PlanBranch.kind == BranchKind.main.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert events
        for event in events:
            assert event.last_seen_at is not None
            last_seen = _utc(event.last_seen_at)
            later_buckets = await session.scalar(
                select(func.count())
                .select_from(EventMetric)
                .where(EventMetric.event_id == event.id, EventMetric.bucket > last_seen)
            )
            assert later_buckets == 0, "the 'dead for 45 days' event has recent traffic"
            created = _utc(event.created_at)
            # Seen no earlier than written down, and not pushed a further 30
            # days ahead of every other event.
            assert created <= last_seen
            assert created >= last_seen - timedelta(days=1)

    resp = await client.get(f"/api/v1/projects/{slug}/reconciliation/dead-events?days=30")
    assert DEAD_EVENT_NAME in [item["name"] for item in resp.json()["items"]]


@pytest.mark.asyncio
async def test_dead_event_clamp_handles_a_created_at_reloaded_from_the_database(
    client: AsyncClient,
) -> None:
    """The clamp compares a reloaded ``created_at`` without a TypeError.

    A fresh session reloads the event, and SQLite hands ``created_at`` back
    naive. Comparing that directly with the aware ``last_seen`` raised
    "can't compare offset-naive and offset-aware datetimes".
    """
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    now = datetime.now(UTC)

    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        row = (
            await session.execute(
                select(Event.id, Event.branch_id)
                .join(PlanBranch, PlanBranch.id == Event.branch_id)
                .where(
                    Event.project_id == project_id,
                    Event.name == DEAD_EVENT_NAME,
                    PlanBranch.kind == BranchKind.main.value,
                )
            )
        ).one()
        # Make the clamp fire: first-seen later than the planted last sighting.
        await session.execute(update(Event).where(Event.id == row.id).values(created_at=now))
        await session.commit()

    assert project_id is not None
    async with TestSessionLocal() as session:
        ctx = DemoContext(
            project_id=project_id,
            branch_id=row.branch_id,
            slug=slug,
            now=now,
            event_ids={DEAD_EVENT_NAME: row.id},
        )
        await governance._build_dead_event(session, ctx)
        await session.commit()

    async with TestSessionLocal() as session:
        event = await session.get(Event, row.id)
        assert event is not None
        assert _utc(event.created_at) == now - timedelta(days=DEAD_EVENT_AGE_DAYS)


def test_synthetic_warehouse_never_emits_the_dead_event() -> None:
    """A collection must not bump its ``last_seen_at`` and promote it to live."""
    retired = {ev.event_name for ev in synthetic._EVENT_DEFS if ev.retired}
    assert retired == {DEAD_EVENT_NAME}
    # Still on the roster, so a rescan keeps folding it onto the catalog event.
    assert DEAD_EVENT_NAME in synthetic.SYNTHETIC_EVENT_NAMES

    anchor = datetime(2026, 3, 4, 10, tzinfo=UTC)
    rows = synthetic._generate_events(
        synthetic.DEFAULT_SEED,
        anchor,
        synthetic.SYNTHETIC_HISTORY_DAYS,
        synthetic.SYNTHETIC_MAX_ROWS,
    )
    assert rows
    assert synthetic.SYNTHETIC_HISTORY_DAYS < DEAD_EVENT_AGE_DAYS
    assert [row for row in rows if row["event_name"] == DEAD_EVENT_NAME] == []


# ── lowest free demo name ─────────────────────────────────────────────────


def test_demo_name_takes_the_lowest_free_number() -> None:
    assert demo_service._demo_project_name([]) == "Demo Project"
    assert demo_service._demo_project_name(["Demo Project"]) == "Demo Project 2"
    assert demo_service._demo_project_name(["Demo Project 2"]) == "Demo Project"
    assert demo_service._demo_project_name(["Demo Project", "Demo Project 3"]) == "Demo Project 2"
    assert demo_service._demo_project_name(["Demo Project", "Demo Project 2"]) == "Demo Project 3"


@pytest.mark.asyncio
async def test_a_demo_created_after_a_delete_does_not_repeat_a_live_name(
    client: AsyncClient,
) -> None:
    first = (await client.post("/api/v1/projects/demo")).json()
    second = (await client.post("/api/v1/projects/demo")).json()
    assert (first["name"], second["name"]) == ("Demo Project", "Demo Project 2")

    assert (await client.delete(f"/api/v1/projects/demo/{first['slug']}")).status_code == 204

    third = (await client.post("/api/v1/projects/demo")).json()
    assert third["name"] != second["name"]
    assert third["name"] == "Demo Project"


# ── blocking work runs off the event loop ─────────────────────────────────


@pytest.mark.asyncio
async def test_demo_cpu_work_runs_off_the_event_loop(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MSTL, PSI and the synthetic adapter must not block the API loop."""
    loop_thread = threading.get_ident()
    seen: dict[str, list[int]] = {"detect": [], "psi": [], "adapter": []}

    def recording(key: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            seen[key].append(threading.get_ident())
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        monitoring, "detect_anomalies", recording("detect", monitoring.detect_anomalies)
    )
    monkeypatch.setattr(monitoring, "compute_psi", recording("psi", monitoring.compute_psi))
    monkeypatch.setattr(catalog, "build_adapter", recording("adapter", catalog.build_adapter))

    assert (await client.post("/api/v1/projects/demo")).status_code == 201

    for key, threads in seen.items():
        assert threads, f"{key} never ran"
        assert loop_thread not in threads, f"{key} ran on the event loop"


# ── demo_sink local notice ────────────────────────────────────────────────


def test_demo_sink_notice_is_written_down_once() -> None:
    """Seeder and worker both import the notice; neither restates it."""
    package = Path(tripl.__file__).parent
    holders = [
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "tests" not in path.relative_to(package).parts
        and "Simulated local delivery (demo_sink)" in path.read_text(encoding="utf-8")
    ]
    assert holders == ["alert_templates.py"]
    assert not hasattr(alerts_builder, "_LOCAL_NOTICE")


@pytest.mark.asyncio
async def test_seeded_demo_delivery_carries_the_shared_notice(client: AsyncClient) -> None:
    slug = (await client.post("/api/v1/projects/demo")).json()["slug"]
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        snapshots = (
            (
                await session.execute(
                    select(AlertDelivery.payload_snapshot).where(
                        AlertDelivery.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )
    notices = {(snapshot or {}).get("local_notice") for snapshot in snapshots}
    assert notices == {DEMO_SINK_LOCAL_NOTICE}


# ── spike marker retires with its anomaly ─────────────────────────────────


def test_retention_retires_the_spike_marker_with_its_anomaly() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    past_cutoff = now - timedelta(days=demo_runtime.DEMO_RETENTION_DAYS + 2)
    recent = now - timedelta(hours=3)
    project_id = uuid.uuid4()
    scan_config_id = uuid.uuid4()
    try:
        with Session(engine) as session:
            session.add(Project(id=project_id, name="Demo Project", slug="demo-age", is_demo=True))
            session.add_all(
                [
                    ChartAnnotation(
                        project_id=project_id, bucket=past_cutoff, label=SPIKE_ANNOTATION_LABEL
                    ),
                    ChartAnnotation(
                        project_id=project_id, bucket=recent, label=SPIKE_ANNOTATION_LABEL
                    ),
                    ChartAnnotation(
                        project_id=project_id, bucket=past_cutoff, label="Pricing change"
                    ),
                ]
            )
            session.commit()

            demo_runtime._prune_retention(session, project_id, scan_config_id, now)
            session.commit()

            left = {
                (label, _utc(bucket))
                for label, bucket in session.execute(
                    select(ChartAnnotation.label, ChartAnnotation.bucket)
                ).all()
            }
    finally:
        engine.dispose()

    assert left == {
        (SPIKE_ANNOTATION_LABEL, recent),
        ("Pricing change", past_cutoff),
    }


# ── SimulatedRuleFiring.from_candidate ────────────────────────────────────


def test_from_candidate_carries_every_candidate_field() -> None:
    candidate = DriftAlertCandidate(
        id=uuid.uuid4(),
        scan_config_id=uuid.uuid4(),
        scope_type=MetricScopeType.release_regression.value,
        scope_ref="ref",
        event_id=uuid.uuid4(),
        event_type_id=None,
        bucket=datetime(2026, 9, 20, 12, tzinfo=UTC),
        direction="drop",
        actual_count=10.0,
        expected_count=40.0,
        drift_field="1.4.0",
        drift_type="volume_drop",
        sample_value="1.3.0",
        window_from=datetime(2026, 9, 18, 9, tzinfo=UTC),
    )
    firing = SimulatedRuleFiring.from_candidate(candidate, scope_name="x" * 400)

    assert firing.anomaly_id == candidate.id
    assert firing.scan_config_id == candidate.scan_config_id
    assert firing.drift_field == "1.4.0"
    assert firing.sample_value == "1.3.0"
    assert firing.window_from == candidate.window_from
    assert firing.absolute_delta == 30.0
    assert firing.percent_delta == pytest.approx(75.0)
    assert len(firing.scope_name) < 400


@pytest.mark.asyncio
async def test_demo_firings_carry_the_fields_the_simulator_sets() -> None:
    """The demo seeder used to drop ``scan_config_id`` (and the drift fields)."""
    project = Project(id=uuid.uuid4(), name="Demo", slug="demo-324", description="", is_demo=True)
    scan_config_id = uuid.uuid4()
    anomaly = MetricAnomaly(
        id=uuid.uuid4(),
        scan_config_id=scan_config_id,
        scope_type=MetricScopeType.project_total.value,
        scope_ref=str(scan_config_id),
        bucket=datetime(2026, 9, 14, 9, tzinfo=UTC),
        actual_count=120.0,
        expected_count=40.0,
        stddev=5.0,
        effective_stddev=5.0,
        z_score=16.0,
        detector_kind="phase",
        direction="spike",
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_local() as session:
            session.add_all([project, anomaly])
            await session.commit()
            firings = await alerts_builder._build_firings(session, project, [anomaly])
    finally:
        await engine.dispose()

    assert len(firings) == 1
    expected = SimulatedRuleFiring.from_candidate(
        anomaly, scope_name=project.name, bucket=_utc(anomaly.bucket)
    )
    assert firings[0] == expected
    assert firings[0].scan_config_id == scan_config_id
