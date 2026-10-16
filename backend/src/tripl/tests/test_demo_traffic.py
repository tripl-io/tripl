"""A legacy synthetic demo's traffic: app versions, platforms and volume over time.

Pure functions, no database: :mod:`tripl.core.adapters.synthetic_traffic`, the
model a recipe-4 demo's ``synthetic`` source still serves its traffic from.
Recipe 5 demos read the demo warehouse (``core/demo_warehouse``, pinned in
``test_demo_warehouse_model``), but a source created before it keeps running on
this model until its demo is reset, so the model stays pinned here.

What these pin: every hour carries an app-version split and a platform split
for every event, so a legacy demo's "By version" charts cover its window.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import NamedTuple

from tripl.core.adapters import synthetic
from tripl.core.adapters.synthetic_traffic import (
    BETA_LEAD,
    BETA_SHARE,
    DRIFTING_EVENT_TYPE,
    PLATFORMS,
    REFERENCE_RELEASE,
    RELEASE_CADENCE,
    TRAFFIC_ANCHOR_KEY,
    TRAFFIC_SEED_KEY,
    DemoTraffic,
    adoption,
    split_count,
    stored_traffic,
    traffic_params,
    version_label,
)
from tripl.services.demo import DEMO_SEED
from tripl.services.release_annotations import releases_to_annotate
from tripl.services.version_activation import DEFAULT_ACTIVE_SHARE_MIN

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)
_WEEK = timedelta(weeks=1)

#: The history a recipe-4 demo seeded, and the release it was built around.
_LEGACY_HISTORY_DAYS = 23
REFERENCE_VERSION = version_label(REFERENCE_RELEASE)
#: The event a recipe-4 demo spiked.
SPIKE_EVENT_NAME = "Home Screen View"

# One concrete demo: a fixed seed clock and the scenario seed.
SEED_CLOCK = datetime(2026, 10, 7, 12, tzinfo=UTC)
TRAFFIC = DemoTraffic(anchor=SEED_CLOCK, seed=DEMO_SEED)
# Every hour the seeder backfilled, oldest first; the newest is the spike hour.
SEEDED_HOURS = [SEED_CLOCK - hours * _HOUR for hours in range(_LEGACY_HISTORY_DAYS * 24, 0, -1)]


class _Spec(NamedTuple):
    name: str
    event_type: str
    base: int


# The events with traffic, as the synthetic source's own roster names them.
LIVE_SPECS = [
    _Spec(event.event_name, event.event_type, event.ongoing_base)
    for event in synthetic._EVENT_DEFS
    if not event.retired
]


def _spec(name: str) -> _Spec:
    return next(spec for spec in LIVE_SPECS if spec.name == name)


def _platform_counts(spec: _Spec, bucket: datetime) -> dict[str, int]:
    """One event's ordinary hour split across platforms."""
    return TRAFFIC.platform_counts(
        bucket,
        event_name=spec.name,
        event_type=spec.event_type,
        base=spec.base,
        count=TRAFFIC.volume(spec.base, spec.name, bucket),
    )


def _web_share(spec: _Spec, bucket: datetime) -> float:
    shares = TRAFFIC.platform_shares(bucket, event_name=spec.name, event_type=spec.event_type)
    return shares["web"]


def _leading_version(bucket: datetime) -> str:
    shares = TRAFFIC.version_shares(bucket)
    return max(shares, key=shares.__getitem__)


# ── app versions ─────────────────────────────────────────────────────────────


def test_every_seeded_hour_splits_its_traffic_across_app_versions() -> None:
    """The version charts cover the whole window, not its newest hours."""
    home = _spec(SPIKE_EVENT_NAME)
    for bucket in SEEDED_HOURS:
        shares = TRAFFIC.version_shares(bucket)
        assert len(shares) >= 4, bucket
        assert all(share > 0 for share in shares.values())
        assert math.isclose(math.fsum(shares.values()), 1.0)
        count = TRAFFIC.volume(home.base, home.name, bucket)
        counts = TRAFFIC.version_counts(bucket, event_name=home.name, count=count)
        assert sum(counts.values()) == count
        assert set(counts) == set(shares), bucket


def test_the_release_train_moves_across_the_window() -> None:
    """1.2.0 leads the window's first days, 1.3.0 its middle, 1.4.0 its end."""
    assert _leading_version(SEEDED_HOURS[0]) == "1.2.0"
    assert _leading_version(SEEDED_HOURS[len(SEEDED_HOURS) // 2]) == "1.3.0"
    assert _leading_version(SEEDED_HOURS[-1]) == REFERENCE_VERSION
    # A long tail: every older build still has users at the seed clock.
    final = TRAFFIC.version_shares(SEEDED_HOURS[-1])
    assert list(final) == ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
    assert final["1.2.0"] > 0.03
    assert 0.6 < final[REFERENCE_VERSION] < 0.8


def test_version_shares_sum_to_one_long_after_the_seed() -> None:
    """The model answers for as long as a legacy source is read."""
    for hours in range(0, 24 * 120, 7):
        shares = TRAFFIC.version_shares(SEED_CLOCK + hours * _HOUR)
        assert all(share > 0 for share in shares.values())
        assert math.isclose(math.fsum(shares.values()), 1.0)


def test_adoption_is_an_s_curve_with_a_long_tail() -> None:
    curve = [adoption(hour / 24) for hour in range(-24 * 10, 24 * 120)]
    assert all(later >= earlier for earlier, later in pairwise(curve))
    # Nobody before the beta, the testers during it, and no jump at rollout.
    assert adoption(-BETA_LEAD / _DAY - 0.01) == 0.0
    assert adoption(-1.0) == BETA_SHARE == adoption(0.0)
    # S-shaped: the daily gain grows over the first days, peaks, then shrinks.
    gains = [adoption(day + 1.0) - adoption(float(day)) for day in range(10)]
    peak = gains.index(max(gains))
    assert 2 <= peak <= 3
    assert gains[0] < gains[peak]
    assert gains[-1] < gains[0]
    # About half of the users within four days...
    assert adoption(3.0) < 0.5 < adoption(4.0)
    # ...and a long tail: a few percent still have not updated four weeks on.
    assert 0.02 < 1.0 - adoption(28.0) < 0.05
    assert adoption(120.0) > 0.999


def test_the_previous_release_fades_as_the_next_one_rolls_out() -> None:
    rollout = TRAFFIC.rollout_start(REFERENCE_RELEASE)
    hours = [rollout + k * _HOUR for k in range(int((SEED_CLOCK - rollout) / _HOUR))]
    previous_version = version_label(REFERENCE_RELEASE - 1)
    newest = [TRAFFIC.version_shares(bucket)[REFERENCE_VERSION] for bucket in hours]
    previous = [TRAFFIC.version_shares(bucket)[previous_version] for bucket in hours]
    assert all(later > earlier for earlier, later in pairwise(newest))
    assert all(later < earlier for earlier, later in pairwise(previous))
    # It keeps a tail rather than vanishing.
    assert previous[-1] > 0.15


def test_a_beta_stays_under_the_activation_gate() -> None:
    """Testers run a build for days on a sliver of traffic; only its rollout is a release."""
    assert BETA_SHARE < DEFAULT_ACTIVE_SHARE_MIN
    rollout = TRAFFIC.rollout_start(REFERENCE_RELEASE)
    assert REFERENCE_VERSION not in TRAFFIC.version_shares(rollout - BETA_LEAD - _HOUR)
    beta = [rollout - BETA_LEAD + k * _HOUR for k in range(int(BETA_LEAD / _HOUR))]
    assert {TRAFFIC.version_shares(bucket)[REFERENCE_VERSION] for bucket in beta} == {BETA_SHARE}


def test_the_release_train_ships_every_two_weeks() -> None:
    assert timedelta(days=14) == RELEASE_CADENCE
    assert version_label(REFERENCE_RELEASE) == REFERENCE_VERSION == "1.4.0"
    assert TRAFFIC.rollout_start(REFERENCE_RELEASE) == SEED_CLOCK - timedelta(days=6)
    # And the model keeps shipping them: in beta ahead of the rollout, on most
    # devices a week into it.
    for release in range(REFERENCE_RELEASE + 1, REFERENCE_RELEASE + 4):
        rollout = TRAFFIC.rollout_start(release)
        assert rollout - TRAFFIC.rollout_start(release - 1) == RELEASE_CADENCE
        label = version_label(release)
        assert label not in TRAFFIC.version_shares(rollout - BETA_LEAD - _HOUR)
        assert TRAFFIC.version_shares(rollout - _HOUR)[label] == BETA_SHARE
        assert TRAFFIC.version_shares(rollout + _WEEK)[label] > 0.7


def test_only_the_reference_release_ships_inside_the_seeded_window() -> None:
    """The worker's release rule over the seeded history marks 1.4.0 alone.

    At the hour it crossed the activation gate, on its first day of rollout. The
    older versions already carry traffic in the first seeded hours, so a scan of
    a fresh demo has no release of its own to add.
    """
    by_version: dict[str, dict[datetime, int]] = {}
    totals: dict[datetime, int] = {}
    for bucket in SEEDED_HOURS:
        for spec in LIVE_SPECS:
            count = TRAFFIC.volume(spec.base, spec.name, bucket)
            totals[bucket] = totals.get(bucket, 0) + count
            for version, part in TRAFFIC.version_counts(
                bucket, event_name=spec.name, count=count
            ).items():
                series = by_version.setdefault(version, {})
                series[bucket] = series.get(bucket, 0) + part

    releases = releases_to_annotate(by_version, totals)

    assert list(releases) == [REFERENCE_VERSION]
    rollout = TRAFFIC.rollout_start(REFERENCE_RELEASE)
    marked = releases[REFERENCE_VERSION]
    assert rollout < marked < rollout + _DAY
    assert by_version[REFERENCE_VERSION][marked] >= DEFAULT_ACTIVE_SHARE_MIN * totals[marked]


# ── platforms ────────────────────────────────────────────────────────────────


def test_each_event_keeps_a_steady_platform_mix() -> None:
    for spec in LIVE_SPECS:
        for bucket in SEEDED_HOURS:
            shares = TRAFFIC.platform_shares(
                bucket, event_name=spec.name, event_type=spec.event_type
            )
            assert set(shares) == set(PLATFORMS)
            assert math.isclose(math.fsum(shares.values()), 1.0)
        if spec.event_type == DRIFTING_EVENT_TYPE:
            continue
        ios = [
            TRAFFIC.platform_shares(bucket, event_name=spec.name, event_type=spec.event_type)["ios"]
            for bucket in SEEDED_HOURS
        ]
        assert max(ios) - min(ios) < 0.05, spec.name
        # The same hour of every week has the same mix.
        for bucket in SEEDED_HOURS[: -24 * 7]:
            assert TRAFFIC.platform_shares(
                bucket, event_name=spec.name, event_type=spec.event_type
            ) == TRAFFIC.platform_shares(
                bucket + _WEEK, event_name=spec.name, event_type=spec.event_type
            )


def test_screen_views_drift_towards_web_and_keep_the_new_mix() -> None:
    """The distribution-drift story a legacy demo's PSI ladder told, in its split."""
    drifting = [spec for spec in LIVE_SPECS if spec.event_type == DRIFTING_EVENT_TYPE]
    assert drifting
    for spec in drifting:
        assert max(_web_share(spec, bucket) for bucket in SEEDED_HOURS[:24]) < 0.15
        assert min(_web_share(spec, bucket) for bucket in SEEDED_HOURS[-24:]) > 0.25
        # Steady until the drift span, and steady again from the seed clock on.
        for bucket in SEEDED_HOURS[: 24 * 7]:
            assert _web_share(spec, bucket) == _web_share(spec, bucket + _WEEK)
        for hours in range(24 * 7):
            bucket = SEED_CLOCK + hours * _HOUR
            assert _web_share(spec, bucket) == _web_share(spec, bucket + _WEEK)


def test_split_count_is_exact_and_deterministic() -> None:
    shares = {"a": 0.5, "b": 0.3, "c": 0.2, "none": 0.0}
    for total in (0, 1, 2, 7, 99, 12_345):
        for key in range(50):
            counts = split_count(total, shares, key=("test", key))
            assert sum(counts.values()) == total
            assert "none" not in counts
            # Each value gets the floor or the ceiling of its exact part.
            for value, share in shares.items():
                assert abs(counts.get(value, 0) - total * share) < 1
            assert counts == split_count(total, shares, key=("test", key))
    # A small share shows up in its fraction of hours instead of rounding away:
    # a beta on 25 events an hour has a row in about one hour in ten.
    with_beta = sum(
        1
        for key in range(1000)
        if split_count(25, {"stable": 1 - BETA_SHARE, "beta": BETA_SHARE}, key=("beta", key)).get(
            "beta"
        )
    )
    assert 50 < with_beta < 150


def test_a_rare_event_keeps_its_platform_split_week_over_week() -> None:
    """Platform parity scores a share of a handful of events an hour; a split dealt
    afresh each hour moved it by whole events and flagged the event on every
    collection. Week over week, only the rows the slow volume drift adds move."""
    for name in ("Refund Processed", "Purchase Failed", "Legacy CTA Click"):
        spec = _spec(name)
        for bucket in SEEDED_HOURS[: -24 * 7]:
            this_week = _platform_counts(spec, bucket)
            next_week = _platform_counts(spec, bucket + _WEEK)
            moved = sum(abs(this_week.get(p, 0) - next_week.get(p, 0)) for p in PLATFORMS)
            assert moved == abs(sum(next_week.values()) - sum(this_week.values())), bucket


def test_rows_the_drift_adds_go_to_the_leading_platform() -> None:
    spec = _spec("Refund Processed")
    bucket = SEEDED_HOURS[100]
    steady = TRAFFIC.steady_volume(spec.base, spec.name, bucket)
    shares = TRAFFIC.platform_shares(bucket, event_name=spec.name, event_type=spec.event_type)
    leading = max(shares, key=shares.__getitem__)

    def split(count: int) -> dict[str, int]:
        return TRAFFIC.platform_counts(
            bucket, event_name=spec.name, event_type=spec.event_type, base=spec.base, count=count
        )

    assert split(steady + 1) == {**split(steady), leading: split(steady).get(leading, 0) + 1}


def test_the_spike_excess_follows_the_spike_split() -> None:
    """The "Why it changed" story: almost all of the spike comes from iOS."""
    spec = _spec(SPIKE_EVENT_NAME)
    bucket = SEEDED_HOURS[-1]
    usual = TRAFFIC.volume(spec.base, spec.name, bucket)
    excess = usual * (synthetic.SPIKE_MULTIPLIER - 1)
    ordinary = _platform_counts(spec, bucket)
    spiked = TRAFFIC.platform_counts(
        bucket,
        event_name=spec.name,
        event_type=spec.event_type,
        base=spec.base,
        count=usual,
        excess=excess,
        excess_shares=synthetic.SPIKE_PLATFORM_SPLIT,
    )
    assert sum(spiked.values()) == usual + excess
    for platform, share in synthetic.SPIKE_PLATFORM_SPLIT.items():
        extra = spiked.get(platform, 0) - ordinary.get(platform, 0)
        assert abs(extra - share * excess) < 1, platform


# ── the synthetic source's stored model ────────────────────────────────────────────────────────


def test_a_long_running_demo_keeps_the_newest_whole_hours() -> None:
    """Months on, the stored hours outgrow the row budget: the oldest go, whole."""
    hours = [
        [{"hour": hour, "row": row} for row in range(size)] for hour, size in enumerate((3, 2, 4))
    ]
    newest_two = hours[1] + hours[2]
    assert synthetic._newest_hours_within(hours, 100) == hours[0] + newest_two
    assert synthetic._newest_hours_within(hours, 6) == newest_two
    assert synthetic._newest_hours_within(hours, 5) == hours[2]
    assert synthetic._newest_hours_within(hours, 3) == []


def test_stored_traffic_reads_back_only_what_the_seeder_wrote() -> None:
    params = traffic_params(TRAFFIC)
    assert stored_traffic(params) == TRAFFIC
    assert stored_traffic({synthetic.SPIKE_EVENT_KEY: SPIKE_EVENT_NAME, **params}) == TRAFFIC
    # Any instant names the same anchor, on the hour grid in UTC.
    assert (
        stored_traffic(
            {TRAFFIC_ANCHOR_KEY: "2026-10-07T14:00:00+02:00", TRAFFIC_SEED_KEY: DEMO_SEED}
        )
        == TRAFFIC
    )
    assert DemoTraffic(anchor=datetime(2026, 10, 7, 12, 34, 56), seed=1).anchor == SEED_CLOCK
    # Any other synthetic source has no model, and keeps the dataset it always had.
    for malformed in (
        None,
        [],
        {},
        {TRAFFIC_ANCHOR_KEY: params[TRAFFIC_ANCHOR_KEY]},
        {TRAFFIC_SEED_KEY: DEMO_SEED},
        {**params, TRAFFIC_SEED_KEY: True},
        {**params, TRAFFIC_SEED_KEY: "20240711"},
        {**params, TRAFFIC_ANCHOR_KEY: 1_759_838_400},
        {**params, TRAFFIC_ANCHOR_KEY: "yesterday"},
    ):
        assert stored_traffic(malformed) is None, malformed
