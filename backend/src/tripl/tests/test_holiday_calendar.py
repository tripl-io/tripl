"""A project's holiday calendar: public holidays as planned events (F18, #271)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.planned_event import PlannedEvent
from tripl.models.project import Project
from tripl.services.holiday_calendar import sync_project_holidays
from tripl.tests.conftest import TestSessionLocal
from tripl.tests.test_active_signals_incidents import _anomaly, _make_project_with_scan
from tripl.worker.tasks import holiday_calendar as holiday_task


async def _holiday_rows(slug: str) -> list[PlannedEvent]:
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == slug))
        return list(
            await session.scalars(
                select(PlannedEvent)
                .where(PlannedEvent.project_id == project_id, PlannedEvent.source == "holiday")
                .order_by(PlannedEvent.starts_at)
            )
        )


@pytest.mark.asyncio
async def test_choosing_a_country_adds_its_holidays_and_clearing_removes_them(
    client: AsyncClient,
) -> None:
    await client.post("/api/v1/projects", json={"name": "H", "slug": "hol-us"})
    countries = (
        await client.get("/api/v1/projects/hol-us/anomaly-settings/holiday-countries")
    ).json()
    assert "US" in countries and "DE" in countries and all(len(code) == 2 for code in countries)

    resp = await client.patch(
        "/api/v1/projects/hol-us/anomaly-settings", json={"holiday_country": "us"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["holiday_country"] == "US"

    rows = await _holiday_rows("hol-us")
    year = datetime.now(UTC).year
    new_year = datetime(year, 1, 1, tzinfo=UTC)
    by_start = {row.starts_at.replace(tzinfo=UTC): row for row in rows}
    assert by_start[new_year].label == "New Year's Day"
    assert by_start[new_year].ends_at.replace(tzinfo=UTC) == new_year + timedelta(days=1)
    assert by_start[new_year].direction is None and by_start[new_year].scope_type is None
    # Last year, this year and next year.
    assert {row.starts_at.year for row in rows} == {year - 1, year, year + 1}

    # Listed with the rest, marked as the calendar's.
    listed = (await client.get("/api/v1/projects/hol-us/planned-events")).json()
    assert {row["source"] for row in listed} == {"holiday"}

    # The calendar owns its rows: no hand edit or delete.
    holiday_id = str(rows[0].id)
    assert (
        await client.delete(f"/api/v1/projects/hol-us/planned-events/{holiday_id}")
    ).status_code == 409
    assert (
        await client.patch(
            f"/api/v1/projects/hol-us/planned-events/{holiday_id}", json={"label": "x"}
        )
    ).status_code == 409

    resp = await client.patch(
        "/api/v1/projects/hol-us/anomaly-settings", json={"holiday_country": None}
    )
    assert resp.status_code == 200 and resp.json()["holiday_country"] is None
    assert await _holiday_rows("hol-us") == []


@pytest.mark.asyncio
async def test_an_unknown_country_is_refused(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "H", "slug": "hol-bad"})
    resp = await client.patch(
        "/api/v1/projects/hol-bad/anomaly-settings", json={"holiday_country": "ZZ"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_switching_country_replaces_the_rows_and_a_holiday_spike_is_planned(
    client: AsyncClient,
) -> None:
    slug = "hol-switch"
    _type, _event, scan_config_id = await _make_project_with_scan(client, slug)
    year = datetime.now(UTC).year
    christmas = datetime(year, 12, 25, 10, tzinfo=UTC)
    async with TestSessionLocal() as session:
        anomaly = _anomaly(scan_config_id, "project_total", scan_config_id, christmas)
        session.add(anomaly)
        await session.commit()
        anomaly_id = anomaly.id

    await client.patch(f"/api/v1/projects/{slug}/anomaly-settings", json={"holiday_country": "DE"})
    german = {row.label for row in await _holiday_rows(slug)}
    assert "German Unity Day" in german

    async with TestSessionLocal() as session:
        stored = await session.get(MetricAnomaly, anomaly_id)
        assert stored is not None and stored.planned_event_id is not None
        tagged = await session.get(PlannedEvent, stored.planned_event_id)
        assert tagged is not None and tagged.source == "holiday"

    await client.patch(f"/api/v1/projects/{slug}/anomaly-settings", json={"holiday_country": "US"})
    american = {row.label for row in await _holiday_rows(slug)}
    assert "Independence Day" in american
    assert "German Unity Day" not in american


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_rolls_into_a_new_year(client: AsyncClient) -> None:
    await client.post("/api/v1/projects", json={"name": "H", "slug": "hol-roll"})
    await client.patch("/api/v1/projects/hol-roll/anomaly-settings", json={"holiday_country": "FR"})
    async with TestSessionLocal() as session:
        project_id = await session.scalar(select(Project.id).where(Project.slug == "hol-roll"))
        assert project_id is not None
        this_year = date.today()
        assert not await session.run_sync(sync_project_holidays, project_id)
        next_year = date(this_year.year + 1, 1, 2)
        assert await session.run_sync(
            lambda s: sync_project_holidays(s, project_id, today=next_year)
        )
        await session.commit()
    years = {row.starts_at.year for row in await _holiday_rows("hol-roll")}
    assert years == {this_year.year, this_year.year + 1, this_year.year + 2}


def test_the_nightly_task_is_scheduled() -> None:
    from tripl.worker.celery_app import celery_app

    assert any(
        entry["task"] == "tripl.worker.tasks.holiday_calendar.sync_holiday_calendars"
        for entry in celery_app.conf.beat_schedule.values()
    )
    assert holiday_task.sync_holiday_calendars.name in celery_app.tasks
