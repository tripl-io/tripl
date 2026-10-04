"""What the digest watermark's compare-and-set actually guarantees.

Three comments used to credit the compare-and-set on ``last_flushed_at`` with
making "a repeated DST wall-clock time recompute the same value and lose here
instead of sending twice". It does no such thing and cannot: the autumn fold
resolves one wall time to two UTC instants an hour apart, the predicate is
``last_flushed_at < fire_at``, and two distinct instants both pass it. A reader
who trusted that sentence would have read the fold pair as the bug it claimed to
prevent and collapsed it in ``_utc_instants`` — deleting a real hour of fires
from every sub-daily cadence, once a year.

What the claim buys is single flight per FIRE INSTANT: every tick inside one
window recomputes the same ``fire_at``, so only the first can move the watermark
past it. Both halves are pinned below by replaying the flusher's scheduled arm
over a tick grid — the same ``previous_fire_at`` call, the same "claim only if
strictly newer than the watermark" test as the UPDATE in
``alert_flush.flush_due_alert_digests``.

Deliberately NOT re-proved here, because they are already pinned elsewhere:

* that the losing tick then sends nothing at all (the database half) —
  ``test_alert_digest_delivery.test_a_second_flush_inside_the_same_window_sends_nothing``
  and its Postgres twin in ``test_alert_digest_concurrency_pg.py``;
* that the SPRING gap resolves forward to a single instant —
  ``test_cadence_dst_gap.py``. The gap case appears below only
  as the contrast the corrected comments now draw: a gap collapses to one
  window, a fold does not collapse at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from tripl.core.alert_schedule import previous_fire_at

BERLIN = "Europe/Berlin"
DAILY_0230 = "30 2 * * *"
HOURLY = "0 * * * *"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def wall_clock(instant: datetime, zone: str) -> str:
    """The instant as the operator reads it on the project's own clock."""
    return instant.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


class Replay(NamedTuple):
    claimed: list[datetime]
    rejected: int
    ticks: int


def replay(
    cron: str,
    *,
    zone: str,
    watermark: datetime,
    start: datetime,
    end: datetime,
    step_minutes: int,
) -> Replay:
    """Every window a flusher ticking on this grid would claim.

    Mirrors the scheduled arm of ``flush_due_alert_digests``: a tick asks
    ``previous_fire_at`` for the last fire at or before ``now`` bounded by the
    watermark, and claims it only if it is strictly newer than the watermark —
    the Python spelling of ``WHERE last_flushed_at < fire_at``, with the
    watermark advanced to the claimed instant exactly as the UPDATE does.
    """
    last = watermark
    claimed: list[datetime] = []
    rejected = 0
    ticks = 0
    now = start
    while now <= end:
        ticks += 1
        fire_at = previous_fire_at(cron, tz_name=zone, now=now, not_before=last)
        if fire_at is not None:
            if fire_at > last:
                claimed.append(fire_at)
                last = fire_at
            else:
                rejected += 1
        now += timedelta(minutes=step_minutes)
    return Replay(claimed, rejected, ticks)


def test_every_tick_inside_one_window_recomputes_the_same_fire_instant() -> None:
    """The guarantee the compare-and-set really provides, on an ordinary day.

    The flusher runs once a minute and a daily cadence is due once, so 180 of
    these 181 ticks recompute a ``fire_at`` the watermark has already passed and
    lose the compare-and-set. That is the single-flight property: it is keyed on
    the instant, which is why the watermark stores the fire instant and never
    ``now`` — a ``now`` watermark is a fresh value on every tick and would
    reject nothing.
    """
    result = replay(
        DAILY_0230,
        zone=BERLIN,
        watermark=utc("2026-09-04 00:30:00"),
        start=utc("2026-09-05 00:00:00"),
        end=utc("2026-09-05 03:00:00"),
        step_minutes=1,
    )

    assert result.claimed == [utc("2026-09-05 00:30:00")]
    assert result.ticks == 181
    assert result.rejected == 180
    # Two ticks either side of the claim, spelled out: same window, same answer.
    assert previous_fire_at(DAILY_0230, tz_name=BERLIN, now=utc("2026-09-05 00:45:00")) == (
        previous_fire_at(DAILY_0230, tz_name=BERLIN, now=utc("2026-09-05 02:59:00"))
    )


def test_the_autumn_fold_claims_two_windows_and_the_watermark_admits_both() -> None:
    """The claim the old comments got backwards.

    02:30 happens twice in Berlin on 2026-10-25 — 00:30Z in CEST and 01:30Z in
    CET. Those are two instants, so ``last_flushed_at < fire_at`` passes twice
    and the day gets two windows. Nothing is sent twice: the buffer is claimed
    by deletion, so the second window carries only what arrived after the first,
    and for any cadence denser than daily the extra window is simply the extra
    hour that day really had.
    """
    result = replay(
        DAILY_0230,
        zone=BERLIN,
        watermark=utc("2026-10-24 00:30:00"),
        start=utc("2026-10-25 00:00:00"),
        end=utc("2026-10-25 03:00:00"),
        step_minutes=1,
    )

    assert result.claimed == [utc("2026-10-25 00:30:00"), utc("2026-10-25 01:30:00")]
    # One wall time, two claims — the thing the compare-and-set does NOT merge.
    assert {wall_clock(instant, BERLIN) for instant in result.claimed} == {"2026-10-25 02:30"}
    # ...while still rejecting every repeat of an instant it has already passed.
    assert result.rejected == 179


def test_the_spring_gap_collapses_to_one_window_but_the_fold_does_not() -> None:
    """The contrast the corrected comments draw, from the watermark's side.

    A nonexistent wall time resolves forward to a single instant (an earlier fix
    landed that in ``_utc_instants``), so the gap day claims ONE window at 03:30
    local. The fold is the opposite case and is left alone: the wall time really
    did happen twice. Which one a day gets is decided in the schedule module —
    the flusher's compare-and-set cannot tell the two situations apart.
    """
    gap = replay(
        DAILY_0230,
        zone=BERLIN,
        watermark=utc("2026-03-28 01:30:00"),
        start=utc("2026-03-29 00:00:00"),
        end=utc("2026-03-29 03:00:00"),
        step_minutes=1,
    )

    assert gap.claimed == [utc("2026-03-29 01:30:00")]
    assert wall_clock(gap.claimed[0], BERLIN) == "2026-03-29 03:30"


def test_collapsing_the_fold_would_cost_an_hourly_cadence_a_real_window() -> None:
    """Why "reject the repeated wall time" is the wrong fix, priced.

    The Berlin fold day is 25 hours long and an hourly digest fires 25 times on
    it, twice at 02:00 local. Resolving the ambiguous wall time to ``fold=0``
    alone — the tidy-up the old comments invited — would drop 2026-10-25 01:00Z
    outright, so alerts buffered during the repeated CET hour would wait an
    extra hour with no indication anything had been held.
    """
    result = replay(
        HOURLY,
        zone=BERLIN,
        watermark=utc("2026-10-24 21:00:00"),
        start=utc("2026-10-24 21:30:00"),
        end=utc("2026-10-25 23:00:00"),
        step_minutes=5,
    )
    berlin = ZoneInfo(BERLIN)
    fold_day = [
        instant for instant in result.claimed if instant.astimezone(berlin).date().day == 25
    ]

    assert len(fold_day) == 25
    assert utc("2026-10-25 01:00:00") in fold_day, "the window a fold=0 collapse would delete"
    assert [wall_clock(i, BERLIN) for i in fold_day].count("2026-10-25 02:00") == 2
