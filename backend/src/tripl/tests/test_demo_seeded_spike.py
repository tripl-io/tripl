"""The demo's seeded spike survives the first scheduled collection.

A generated demo seeds one spike (Home Screen View, three times its volume, the
newest full hour) straight into ``EventMetric`` and seeds the three anomalies it
trips. The scheduled collection re-reads the newest hours from the synthetic
warehouse and rewrites them, then re-runs detection over them. Two things used to
undo the seed at the first top of the hour after generation:

* the synthetic warehouse served an ordinary hour there, so the rewrite erased the
  spike from the series;
* the default two-hour ingestion-settling allowance withheld that newest bucket,
  so detection purged the seeded anomalies and emitted none.

So a newcomer following the coached chapters was sent to "drill into the Home
Screen View spike" on an Anomalies page that said there were none.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from tripl.core.adapters import synthetic as synth
from tripl.core.adapters.base import BaseAdapter
from tripl.core.adapters.registry import build_adapter
from tripl.core.adapters.synthetic import SyntheticAdapter, SyntheticSpike
from tripl.models.data_source import DataSource, DBType
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.services.demo.builders.warehouse import SPIKE_EVENT_NAME
from tripl.tests.conftest import TestSessionLocal

ANCHOR = datetime(2026, 3, 4, 14, tzinfo=UTC)
SPIKE_HOUR = ANCHOR - timedelta(hours=1)


def _hour_rows(adapter: BaseAdapter, event_name: str, hour: datetime) -> list[dict[str, object]]:
    assert isinstance(adapter, SyntheticAdapter)
    end = hour + timedelta(hours=1)
    return [
        row
        for row in adapter._events
        if row["event_name"] == event_name
        and isinstance(row["event_time"], datetime)
        and hour <= row["event_time"] < end
    ]


def test_spike_hour_carries_the_seeded_multiple_and_nothing_else_moves() -> None:
    plain = SyntheticAdapter(seed=7, anchor=ANCHOR)
    spiked = SyntheticAdapter(
        seed=7, anchor=ANCHOR, spike=SyntheticSpike(SPIKE_EVENT_NAME, SPIKE_HOUR)
    )

    base = len(_hour_rows(plain, SPIKE_EVENT_NAME, SPIKE_HOUR))
    assert len(_hour_rows(spiked, SPIKE_EVENT_NAME, SPIKE_HOUR)) == base * synth.SPIKE_MULTIPLIER

    # The hour before, and every other event, are exactly what they were.
    before = SPIKE_HOUR - timedelta(hours=1)
    assert _hour_rows(spiked, SPIKE_EVENT_NAME, before) == _hour_rows(
        plain, SPIKE_EVENT_NAME, before
    )
    assert [r for r in spiked._events if r["event_name"] != SPIKE_EVENT_NAME] == [
        r for r in plain._events if r["event_name"] != SPIKE_EVENT_NAME
    ]


def test_spike_excess_comes_mostly_from_ios() -> None:
    plain = SyntheticAdapter(seed=7, anchor=ANCHOR)
    spiked = SyntheticAdapter(
        seed=7, anchor=ANCHOR, spike=SyntheticSpike(SPIKE_EVENT_NAME, SPIKE_HOUR)
    )
    before = Counter(r["platform"] for r in _hour_rows(plain, SPIKE_EVENT_NAME, SPIKE_HOUR))
    after = Counter(r["platform"] for r in _hour_rows(spiked, SPIKE_EVENT_NAME, SPIKE_HOUR))
    excess = {platform: after[platform] - before[platform] for platform in after}
    total = sum(excess.values())
    # The "Why" panel's story: ~85% of the spike is iOS.
    assert excess["ios"] / total == pytest.approx(synth.SPIKE_PLATFORM_SPLIT["ios"], abs=0.03)


def test_spike_outside_the_ongoing_window_changes_nothing() -> None:
    """Only the hours a scan re-reads at full volume carry the spike."""
    old_hour = ANCHOR - timedelta(hours=synth.SYNTHETIC_ONGOING_HOURS + 3)
    plain = SyntheticAdapter(seed=7, anchor=ANCHOR)
    spiked = SyntheticAdapter(
        seed=7, anchor=ANCHOR, spike=SyntheticSpike(SPIKE_EVENT_NAME, old_hour)
    )
    assert spiked._events == plain._events


def test_dataset_stays_within_row_budget_with_the_spike_at_its_peak() -> None:
    # The same week of anchors as test_synthetic_dataset_stays_within_row_budget.
    for day in range(1, 8):
        for hour in (2, 8, 14, 20):
            anchor = datetime(2026, 3, day, hour, tzinfo=UTC)
            rows = synth._generate_events(
                synth.DEFAULT_SEED,
                anchor,
                synth.SYNTHETIC_HISTORY_DAYS,
                synth.SYNTHETIC_MAX_ROWS,
                SyntheticSpike(SPIKE_EVENT_NAME, anchor - timedelta(hours=1)),
            )
            assert len(rows) < synth.SYNTHETIC_MAX_ROWS, (anchor, len(rows))


@pytest.mark.asyncio
async def test_demo_source_serves_the_spike_it_seeded(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/projects/demo")
    assert resp.status_code == 201
    slug = resp.json()["slug"]

    async with TestSessionLocal() as session:
        project = (await session.execute(select(Project).where(Project.slug == slug))).scalar_one()
        ds = (
            await session.execute(
                select(DataSource).where(
                    DataSource.project_id == project.id, DataSource.db_type == DBType.synthetic
                )
            )
        ).scalar_one()
        # Only the main-plan event carries metrics; branch copies share its name.
        rows = await session.execute(
            select(EventMetric.bucket, EventMetric.count)
            .join(Event, Event.id == EventMetric.event_id)
            .where(Event.project_id == project.id, Event.name == SPIKE_EVENT_NAME)
        )
        stored = {bucket.replace(tzinfo=UTC): count for bucket, count in rows.all()}
        settling = (
            await session.execute(
                select(ProjectAnomalySettings.anomaly_ingestion_settling_minutes).where(
                    ProjectAnomalySettings.project_id == project.id
                )
            )
        ).scalar_one()
        adapter = build_adapter(ds)

    # The source names the hour the seeded series spikes in.
    assert ds.extra_params is not None
    spike_hour = datetime.fromisoformat(str(ds.extra_params["spike_hour"]))
    newest = max(stored)
    assert spike_hour == newest
    assert stored[newest] > 2 * stored[newest - timedelta(hours=1)]

    # ...and serves it: a collection that re-reads that hour reads the spike back.
    served = len(_hour_rows(adapter, SPIKE_EVENT_NAME, spike_hour))
    served_before = len(_hour_rows(adapter, SPIKE_EVENT_NAME, spike_hour - timedelta(hours=1)))
    adapter.close()
    assert served > 2 * served_before

    # The synthetic source has nothing to settle, so detection scores that hour.
    assert settling == 0
