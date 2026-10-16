"""Regression tests for the demo builders.

- The sweep drops the audit trail of a shell that never became a demo.
- The planted dead event has no volume after its last sighting and the demo
  warehouse never sends it, so a collection cannot revive it.
- A new demo takes the lowest free name, not ``count + 1``.
- The scan and replay over the demo warehouse run off the event loop.
- One ``demo_sink`` local notice, written down once and carried by every
  delivery the worker sends.
- ``SimulatedRuleFiring.from_candidate`` carries every candidate field.

The cancel path (a stage boundary honouring the flag) is pinned in
``test_demo_v5_provisioning``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

import tripl
from tripl.alerting_matching import DriftAlertCandidate
from tripl.core.demo_warehouse import DEAD_EVENT, roster
from tripl.core.demo_warehouse import rows as demo_rows
from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import MetricScopeType, ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.schemas.alerting import SimulatedRuleFiring
from tripl.services import demo_service
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import scan as scan_task
from tripl.worker.tasks.metrics import tasks as metrics_tasks


def _utc(value: datetime) -> datetime:
    """SQLite drops the zone on round-trip; compare everything as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ── the sweep ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweeping_a_stalled_shell_drops_its_audit_trail() -> None:
    """A shell that never became a workspace keeps no orphaned trail."""
    stalled_at = datetime.now(UTC) - timedelta(hours=demo_service.STALLED_SEEDING_HOURS + 1)
    async with TestSessionLocal() as session:
        stalled = Project(
            name="Demo Project",
            slug="demo-stalled",
            is_demo=True,
            generation_status=ProjectGenerationStatus.seeding.value,
            # Abandoned: nothing has written to it since it was created, and the
            # stall is judged by the last write (every stage bumps updated_at).
            created_at=stalled_at,
            updated_at=stalled_at,
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
        event = (
            await session.execute(
                # The main plan's row: the one the warehouse series and the
                # dead-events review are about.
                select(Event)
                .join(PlanBranch, PlanBranch.id == Event.branch_id)
                .where(
                    Event.project_id == project_id,
                    Event.name == DEAD_EVENT,
                    PlanBranch.kind == BranchKind.main.value,
                )
            )
        ).scalar_one()
        stored = await session.scalar(
            select(func.count()).select_from(EventMetric).where(EventMetric.event_id == event.id)
        )
    # The scan and the replay never saw it, so its authored last sighting stands.
    assert stored == 0, "the 'dead for 45 days' event has traffic"
    assert event.last_seen_at is not None
    assert _utc(event.created_at) < _utc(event.last_seen_at)

    resp = await client.get(f"/api/v1/projects/{slug}/reconciliation/dead-events?days=30")
    assert DEAD_EVENT in [item["name"] for item in resp.json()["items"]]


def test_the_demo_warehouse_never_sends_the_dead_event() -> None:
    """A collection must not bump its ``last_seen_at`` and promote it to live."""
    assert roster.event(DEAD_EVENT).base_rate == 0
    assert DEAD_EVENT not in {event.name for event in roster.live_events()}
    # Over a whole day of the warehouse, at any hour.
    start = datetime(2026, 3, 4, tzinfo=UTC)
    for hour in range(24):
        counts = demo_rows.hour_counts(start + timedelta(hours=hour))
        assert not {name for name, _platform in counts} & {DEAD_EVENT}


# ── lowest free demo name ─────────────────────────────────────────────────


def test_demo_name_takes_the_lowest_free_number() -> None:
    assert demo_service._demo_project_name([]) == "Demo Project"
    assert demo_service._demo_project_name(["Demo Project"]) == "Demo Project 2"
    assert demo_service._demo_project_name(["Demo Project 2"]) == "Demo Project"
    assert demo_service._demo_project_name(["Demo Project", "Demo Project 3"]) == "Demo Project 2"
    assert demo_service._demo_project_name(["Demo Project", "Demo Project 2"]) == "Demo Project 3"


@pytest.mark.asyncio
async def test_a_demo_created_after_a_delete_does_not_repeat_a_live_name(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # About the names alone: promote the shells without seeding them.
    async def _nothing(_session: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(demo_service, "_seed_demo_content", _nothing)

    first = (await client.post("/api/v1/projects/demo")).json()
    second = (await client.post("/api/v1/projects/demo")).json()
    assert (first["name"], second["name"]) == ("Demo Project", "Demo Project 2")

    assert (await client.delete(f"/api/v1/projects/demo/{first['slug']}")).status_code == 204

    third = (await client.post("/api/v1/projects/demo")).json()
    assert third["name"] != second["name"]
    assert third["name"] == "Demo Project"


# ── blocking work runs off the event loop ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_api_request_runs_no_demo_build(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a demo commits a shell and queues its build; the API never builds.

    The scan and the replay are the provisioning's whole cost, and
    ``session.run_sync`` runs them on the calling thread, so they belong on the
    worker (``worker/tasks/demo_provision.py``), never in a request. With the
    queue held, the request answers with a seeding shell having run neither.
    """
    queued: list[uuid.UUID] = []

    async def _hold(_session: object, project_id: uuid.UUID, _request_id: str | None) -> None:
        queued.append(project_id)

    ran: list[str] = []

    def refusing(key: str) -> Callable[..., Any]:
        def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            ran.append(key)
            raise AssertionError(f"{key} ran inside the API request")

        return _refuse

    monkeypatch.setattr(demo_service, "enqueue_demo_seed", _hold)
    monkeypatch.setattr(scan_task, "execute_scan", refusing("scan"))
    monkeypatch.setattr(metrics_tasks, "execute_collect_metrics", refusing("collect"))

    resp = await client.post("/api/v1/projects/demo")

    assert resp.status_code == 202, resp.text
    assert resp.json()["generation_status"] == ProjectGenerationStatus.seeding.value
    assert [str(project_id) for project_id in queued] == [resp.json()["id"]]
    assert ran == []


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
