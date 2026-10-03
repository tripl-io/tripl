"""Recurring planned windows suggested from expected verdicts (F18 × F01, #271)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.project import Project
from tripl.models.signal_triage import SignalTriage
from tripl.services.demo.builders.alerts import DEMO_WEEKLY_PROMO_NOTE
from tripl.services.planned_window_suggestions import (
    NEXT_WINDOWS,
    _next_occurrences,
    suggest_recurring_windows,
)
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_active_signals_incidents import _anomaly, _make_project_with_scan
from tripl.tests.test_demo_project import _FIXED_NOW, _seed_fixture


def _slot(weeks_back: int, *, hour: int = 9) -> datetime:
    """A Monday at ``hour`` UTC, ``weeks_back`` weeks before this week's."""
    today = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(weeks=weeks_back)


async def _expected_signals(
    client: AsyncClient, slug: str, buckets: list[datetime]
) -> tuple[str, uuid.UUID]:
    _type, event_id, scan_config_id = await _make_project_with_scan(client, slug)
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        assert project_id is not None
        for bucket in buckets:
            session.add(
                _anomaly(scan_config_id, "event", event_id, bucket, event_id=event_id)  # type: ignore[no-untyped-call]
            )
            session.add(
                SignalTriage(
                    project_id=project_id,
                    scan_config_id=uuid.UUID(scan_config_id),
                    scope_type="event",
                    scope_ref=event_id,
                    action="expected",
                    bucket=bucket,
                    note="Weekly newsletter",
                    expected_reason="campaign",
                )
            )
        await session.commit()
    return event_id, project_id


@pytest.mark.asyncio
async def test_three_expected_weeks_suggest_the_next_windows(client: AsyncClient) -> None:
    slug = "suggest-weekly"
    event_id, _project_id = await _expected_signals(client, slug, [_slot(1), _slot(2), _slot(3)])

    resp = await client.get(f"/api/v1/projects/{slug}/planned-events/suggestions")
    assert resp.status_code == 200, resp.text
    [suggestion] = resp.json()
    assert suggestion["scope_type"] == "event"
    assert suggestion["scope_ref"] == event_id
    assert suggestion["scope_name"] == "Landing Viewed"
    assert (suggestion["weekday"], suggestion["hour"]) == (0, 9)
    assert suggestion["verdict_count"] == 3
    assert suggestion["note"] == "Weekly newsletter"
    windows = suggestion["windows"]
    assert len(windows) == NEXT_WINDOWS
    starts = [datetime.fromisoformat(w["starts_at"]) for w in windows]
    assert all(s.weekday() == 0 and s.hour == 9 and s > datetime.now(UTC) for s in starts)
    assert starts[1] - starts[0] == timedelta(weeks=1)
    # The direction the anomalies moved, so the planned window expects it.
    assert suggestion["direction"] == "spike"

    # Accepting = creating the windows; the suggestion then drops out.
    for window in windows:
        created = await client.post(
            f"/api/v1/projects/{slug}/planned-events",
            json={
                "label": "Weekly newsletter",
                "starts_at": window["starts_at"],
                "ends_at": window["ends_at"],
                "direction": suggestion["direction"],
                "scope_type": "event",
                "scope_ref": event_id,
            },
        )
        assert created.status_code == 201, created.text
    assert (await client.get(f"/api/v1/projects/{slug}/planned-events/suggestions")).json() == []


@pytest.mark.asyncio
async def test_two_weeks_suggest_nothing(client: AsyncClient) -> None:
    await _expected_signals(client, "suggest-two", [_slot(1), _slot(2)])
    assert (
        await client.get("/api/v1/projects/suggest-two/planned-events/suggestions")
    ).json() == []


@pytest.mark.asyncio
async def test_scattered_hours_suggest_nothing(client: AsyncClient) -> None:
    await _expected_signals(
        client, "suggest-scattered", [_slot(1, hour=9), _slot(2, hour=10), _slot(3, hour=11)]
    )
    assert (
        await client.get("/api/v1/projects/suggest-scattered/planned-events/suggestions")
    ).json() == []


@pytest.mark.asyncio
async def test_a_project_wide_window_already_covering_it_suppresses_the_suggestion(
    client: AsyncClient,
) -> None:
    slug = "suggest-covered"
    _event_id, project_id = await _expected_signals(client, slug, [_slot(1), _slot(2), _slot(3)])
    async with TestSessionLocal() as session:
        [suggestion] = await suggest_recurring_windows(session, project_id)
    first = suggestion.windows[0]
    await client.post(
        f"/api/v1/projects/{slug}/planned-events",
        json={
            "label": "Freeze week",
            "starts_at": (first.starts_at - timedelta(days=1)).isoformat(),
            "ends_at": (first.starts_at + timedelta(days=1)).isoformat(),
        },
    )
    assert (await client.get(f"/api/v1/projects/{slug}/planned-events/suggestions")).json() == []


def test_next_occurrences_start_after_now() -> None:
    monday_morning = datetime(2026, 10, 5, 8, 30, tzinfo=UTC)  # a Monday
    later_today = _next_occurrences(0, 9, monday_morning)
    assert later_today[0].starts_at == datetime(2026, 10, 5, 9, tzinfo=UTC)
    assert later_today[0].ends_at == datetime(2026, 10, 5, 10, tzinfo=UTC)
    next_week = _next_occurrences(0, 8, monday_morning)
    assert next_week[0].starts_at == datetime(2026, 10, 12, 8, tzinfo=UTC)
    friday = _next_occurrences(4, 0, monday_morning)
    assert friday[0].starts_at == datetime(2026, 10, 9, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_the_demo_ships_a_weekly_promo_suggestion() -> None:
    async with TestSessionLocal() as session:
        project_id = await _seed_fixture(session, "demo-suggest")
        suggestions = await suggest_recurring_windows(session, project_id, now=_FIXED_NOW)
    [promo] = [s for s in suggestions if s.note == DEMO_WEEKLY_PROMO_NOTE]
    assert promo.verdict_count == 3
    assert promo.direction == "spike"
    assert promo.windows[0].starts_at > _FIXED_NOW
