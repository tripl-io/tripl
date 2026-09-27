"""Daily plan health snapshot and the weekly digest lines (F15, #268)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from tripl.models.event import Event, EventStatus
from tripl.models.project import Project
from tripl.models.project_health_snapshot import ProjectHealthSnapshot
from tripl.services.health_weights import SNAPSHOT_RETENTION_DAYS
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks.alerts_health_digest import _health_digest_lines
from tripl.worker.tasks.alerts_messages import _build_plan_digest_message
from tripl.worker.tasks.health import snapshot_all_projects

_NOW = datetime(2026, 9, 28, 5, 55, tzinfo=UTC)


async def _seed(client: AsyncClient, slug: str) -> uuid.UUID:
    """A project with one draft event: described, no owner -> score 50."""
    resp = await client.post("/api/v1/projects", json={"name": slug, "slug": slug})
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])
    base = f"/api/v1/projects/{slug}"
    event_type = await client.post(
        f"{base}/event-types", json={"name": "track", "display_name": "T"}
    )
    assert event_type.status_code == 201, event_type.text
    event = await client.post(
        f"{base}/events",
        json={"event_type_id": event_type.json()["id"], "name": "paywall_shown"},
    )
    assert event.status_code == 201, event.text
    async with TestSessionLocal() as session:
        await session.execute(
            update(Event)
            .where(Event.id == uuid.UUID(event.json()["id"]))
            .values(status=EventStatus.draft.value, description="Shown on the paywall")
        )
        await session.commit()
    return project_id


async def _rows(project_id: uuid.UUID) -> list[ProjectHealthSnapshot]:
    async with TestSessionLocal() as session:
        result = await session.execute(
            select(ProjectHealthSnapshot)
            .where(ProjectHealthSnapshot.project_id == project_id)
            .order_by(ProjectHealthSnapshot.day)
        )
        return list(result.scalars().all())


def _snapshot(project_id: uuid.UUID, day_offset: int, score: int | None, **extra: Any) -> Any:
    return ProjectHealthSnapshot(
        project_id=project_id,
        day=_NOW.date() - timedelta(days=day_offset),
        score=score,
        scored_events=1,
        healthy_count=0,
        warning_count=1,
        unhealthy_count=0,
        component_averages={},
        worst_events=extra.get("worst_events", []),
    )


@pytest.mark.asyncio
async def test_snapshot_upsert_is_idempotent_per_day(client: AsyncClient) -> None:
    project_id = await _seed(client, "hsnap-upsert")
    async with TestSessionLocal() as session:
        first = await snapshot_all_projects(session, _NOW)
        second = await snapshot_all_projects(session, _NOW + timedelta(hours=1))
    assert first["written"] >= 1
    assert second["written"] >= 1
    rows = await _rows(project_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.day == _NOW.date()
    assert row.score == 50
    assert row.scored_events == 1
    assert row.warning_count == 1
    assert row.worst_events[0]["name"] == "paywall_shown"
    assert row.worst_events[0]["top_issue"] == "No owner"
    assert row.component_averages["documentation"] == {"value": 0.5, "applies_count": 1}
    assert row.component_averages["contract"] == {"value": None, "applies_count": 0}


@pytest.mark.asyncio
async def test_snapshot_prunes_rows_past_retention(client: AsyncClient) -> None:
    project_id = await _seed(client, "hsnap-prune")
    async with TestSessionLocal() as session:
        session.add(_snapshot(project_id, SNAPSHOT_RETENTION_DAYS + 1, 40))
        session.add(_snapshot(project_id, SNAPSHOT_RETENTION_DAYS - 1, 45))
        await session.commit()
        stats = await snapshot_all_projects(session, _NOW)
    assert stats["pruned"] >= 1
    days = [row.day for row in await _rows(project_id)]
    assert _NOW.date() - timedelta(days=SNAPSHOT_RETENTION_DAYS + 1) not in days
    assert _NOW.date() - timedelta(days=SNAPSHOT_RETENTION_DAYS - 1) in days
    assert _NOW.date() in days


@pytest.mark.asyncio
async def test_project_health_trend_and_previous_score(client: AsyncClient) -> None:
    project_id = await _seed(client, "hsnap-trend")
    today = datetime.now(UTC)
    async with TestSessionLocal() as session:
        for offset, score in ((7, 44), (3, 47), (40, 10)):
            session.add(
                ProjectHealthSnapshot(
                    project_id=project_id,
                    day=today.date() - timedelta(days=offset),
                    score=score,
                    scored_events=1,
                    healthy_count=0,
                    warning_count=0,
                    unhealthy_count=1,
                    component_averages={},
                    worst_events=[],
                )
            )
        await session.commit()
    resp = await client.get("/api/v1/projects/hsnap-trend/health", params={"trend_days": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["score"] == 50
    assert body["previous_score"] == 44
    assert [point["score"] for point in body["trend"]] == [44, 47]
    assert body["trend"][0]["day"] == (today.date() - timedelta(days=7)).isoformat()


@pytest.mark.asyncio
async def test_digest_lines_render_with_delta_and_worst_events(client: AsyncClient) -> None:
    project_id = await _seed(client, "hsnap-digest")
    worst = [
        {
            "event_id": str(uuid.uuid4()),
            "name": "checkout_started",
            "score": 31,
            "top_issue": "Never seen in data",
        },
        {"event_id": str(uuid.uuid4()), "name": "paywall_shown", "score": 50, "top_issue": None},
    ]
    async with TestSessionLocal() as session:
        session.add(_snapshot(project_id, 0, 72, worst_events=worst))
        session.add(_snapshot(project_id, 7, 75))
        await session.commit()

        def _lines(sync_session: Session) -> list[str]:
            return _health_digest_lines(sync_session, project_id, _NOW)

        lines = await session.run_sync(_lines)
    assert lines == [
        "- Plan health: 72/100 (-3 vs last week)",
        "",
        "Least healthy events:",
        "- checkout_started: 31/100 (Never seen in data)",
        "- paywall_shown: 50/100",
    ]

    async with TestSessionLocal() as session:
        project = await session.get(Project, project_id)
        assert project is not None

        def _message(sync_session: Session) -> str:
            sync_project = sync_session.get(Project, project_id)
            assert sync_project is not None
            return _build_plan_digest_message(sync_session, project=sync_project, now=_NOW)

        message = await session.run_sync(_message)
    assert "- Plan health: 72/100 (-3 vs last week)" in message
    assert "Least healthy events:" in message


@pytest.mark.asyncio
async def test_digest_lines_without_prior_week_or_fresh_snapshot(client: AsyncClient) -> None:
    project_id = await _seed(client, "hsnap-stale")
    async with TestSessionLocal() as session:
        # Only a snapshot five days old: not fresh, so nothing renders.
        session.add(_snapshot(project_id, 5, 60))
        await session.commit()
        stale = await session.run_sync(
            lambda sync_session: _health_digest_lines(sync_session, project_id, _NOW)
        )
        assert stale == []

        # A fresh one with no week-old partner: the delta is omitted.
        session.add(_snapshot(project_id, 1, 61))
        await session.commit()
        fresh = await session.run_sync(
            lambda sync_session: _health_digest_lines(sync_session, project_id, _NOW)
        )
        assert fresh == ["- Plan health: 61/100"]
        count = await session.scalar(
            select(func.count(ProjectHealthSnapshot.id)).where(
                ProjectHealthSnapshot.project_id == project_id
            )
        )
        assert count == 2
