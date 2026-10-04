"""A cadence whose hour falls in the spring DST gap.

``core.alert_schedule`` promises a nonexistent local time folds FORWARD — a
"daily at 02:30" digest on the morning the clocks jump 02:00 -> 03:00 goes out
at 03:30 local, an hour late, because skipping it is a silent 24h outage.

``_utc_instants`` used to return BOTH of ``fold``'s answers for that wall time.
In a gap the second answer is not a second reading of the same instant the way
it is on the autumn fold; it is the post-transition offset applied to a time
that never happened, which resolves 02:30 BACKWARDS to 01:30 local. The flusher
compares fire instants in UTC and has no way to tell that one of them precedes
the wall clock it was derived from, so it claimed the 01:30 window and shipped
the digest an hour EARLY — before the window it summarises had closed, at a
time the operator's schedule never named.

The tests below pin the resolved instant, not just its ordering: the existing
DST coverage in ``test_alert_schedule.py`` probes the gap day only at noon,
where ``max()`` over both candidates hides the early one entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tripl.core.alert_schedule import next_fire_at, previous_fire_at

DAILY_0230 = "30 2 * * *"
ONE_MINUTE = timedelta(minutes=1)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def wall_clock(instant: datetime | None, zone: str) -> str:
    """The instant as the operator reads it on the project's own clock."""
    assert instant is not None
    return instant.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


# Each zone's spring-forward morning, for the one cadence whose hour lands
# inside the gap. `early` is the instant fold=1 produces — an hour before the
# configured 02:30 — and is the value none of these calls may ever return.
GAP_DAYS = [
    pytest.param(
        "America/New_York",
        # 2026-03-08: 02:00 EST -> 03:00 EDT.
        utc("2026-03-08 06:30:00"),  # early: 01:30 EST
        utc("2026-03-08 07:30:00"),  # forward: 03:30 EDT
        utc("2026-03-07 07:30:00"),  # the previous day's real 02:30 EST
        id="America/New_York",
    ),
    pytest.param(
        "Europe/Berlin",
        # 2026-03-29: 02:00 CET -> 03:00 CEST.
        utc("2026-03-29 00:30:00"),  # early: 01:30 CET
        utc("2026-03-29 01:30:00"),  # forward: 03:30 CEST
        utc("2026-03-28 01:30:00"),  # the previous day's real 02:30 CET
        id="Europe/Berlin",
    ),
]


@pytest.mark.parametrize(("zone", "early", "forward", "previous_day"), GAP_DAYS)
def test_a_cadence_in_the_spring_gap_is_not_due_an_hour_before_its_wall_time(
    zone: str, early: datetime, forward: datetime, previous_day: datetime
) -> None:
    """A minute past the early instant, the last fire is still YESTERDAY's.

    This is the tick that shipped the digest: the flusher asks "did a fire fall
    in ``(last_flushed_at, now]``", got the 01:30-local instant back, and its
    compare-and-set happily claimed a window whose wall time never existed.
    """
    at_early = previous_fire_at(DAILY_0230, tz_name=zone, now=early + ONE_MINUTE)

    assert at_early != early
    assert at_early == previous_day
    # And nothing has been lost: the gap day's digest is still ahead, not
    # skipped, which is the property the whole forward-fold rule exists for.
    assert next_fire_at(DAILY_0230, tz_name=zone, after=at_early) == forward


@pytest.mark.parametrize(("zone", "early", "forward", "previous_day"), GAP_DAYS)
def test_the_gap_day_fires_once_and_only_after_the_scheduled_time(
    zone: str, early: datetime, forward: datetime, previous_day: datetime
) -> None:
    """One digest on the gap day, at the forward instant — not two windows.

    Probed on both sides of the missing hour, because the defect is only
    visible before the forward instant arrives; after it, ``max()`` returns the
    later of the two candidates and the early one is invisible.
    """
    before_forward = previous_fire_at(DAILY_0230, tz_name=zone, now=forward - ONE_MINUTE)
    after_forward = previous_fire_at(DAILY_0230, tz_name=zone, now=forward + ONE_MINUTE)

    assert before_forward == previous_day
    assert after_forward == forward
    # 03:30, the hour the clocks jumped to. Never 01:30, which is BEHIND the
    # configured 02:30 on a clock that only ever moved forward.
    assert wall_clock(after_forward, zone).endswith("03:30")


@pytest.mark.parametrize(("zone", "early", "forward", "previous_day"), GAP_DAYS)
def test_the_next_digest_preview_on_the_gap_day_is_the_forward_instant(
    zone: str, early: datetime, forward: datetime, previous_day: datetime
) -> None:
    """The destination card renders this verbatim as "next digest at".

    Previewing 01:30 would have been wrong twice over: earlier than the 02:30
    the operator typed, and — once the worker agreed with it — a real send.
    """
    preview = next_fire_at(DAILY_0230, tz_name=zone, after=previous_day)

    assert preview == forward
    assert preview != early


def test_the_autumn_fold_still_yields_both_of_its_two_real_instants() -> None:
    """Guard: the gap collapse must not be widened into a fold collapse.

    On the fold the wall time genuinely happens twice, and both readings are
    real instants an hour apart. Collapsing them the way the gap is collapsed
    would look like the same tidy-up, and would silently drop an hour of fires
    for every sub-daily cadence, once a year. 2026-11-01 in New York has two
    01:30s: 05:30Z (EDT) and 06:30Z (EST).
    """
    daily_0130 = "30 1 * * *"
    first = previous_fire_at(daily_0130, tz_name="America/New_York", now=utc("2026-11-01 05:45"))
    second = previous_fire_at(daily_0130, tz_name="America/New_York", now=utc("2026-11-01 06:45"))

    assert first == utc("2026-11-01 05:30:00")
    assert second == utc("2026-11-01 06:30:00")
    assert first < second
    # Same wall time, two instants — the case `fold` exists for, left intact.
    assert wall_clock(first, "America/New_York") == wall_clock(second, "America/New_York")
