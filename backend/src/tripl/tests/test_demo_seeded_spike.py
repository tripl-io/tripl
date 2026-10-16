"""A legacy synthetic source keeps serving the spike its demo recorded.

A recipe-4 demo seeded one spike (Home Screen View, three times its volume, the
newest full hour) into its stored series and recorded the hour on its
``synthetic`` data source (``extra_params``), so the scheduled collection that
re-reads that hour reads the same spike back instead of erasing it. Recipe 5
demos read the demo warehouse and seed nothing, but a source created before it
still runs on ``SyntheticAdapter``, so the adapter's half of that contract stays
pinned here: the spike hour carries the multiple, mostly from iOS, nothing else
moves, and the dataset stays inside its row budget.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from tripl.core.adapters import synthetic as synth
from tripl.core.adapters.base import BaseAdapter
from tripl.core.adapters.synthetic import SyntheticAdapter, SyntheticSpike

#: The event a recipe-4 demo spiked (its warehouse builder's ``SPIKE_EVENT_NAME``).
SPIKE_EVENT_NAME = "Home Screen View"
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
