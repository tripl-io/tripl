import re
from datetime import datetime

import pytest

from tripl.core.analyzers.release_regression import (
    KIND_MISSING,
    KIND_VOLUME_DROP,
    REASON_BASELINE_NO_VOLUME,
    REASON_COMPARABLE,
    REASON_NO_BASELINE,
    REASON_POPULATION_MISMATCH,
    RegressionSettings,
    detect_release_regressions,
    detect_release_regressions_by_scope,
)

PREV = "2.0.0"
NEW = "2.1.0"
DAYS = [datetime(2026, 1, d) for d in range(1, 11)]  # 10 daily buckets
NEW_DAYS = DAYS[6:]  # release ships on day 7, ramps through day 10


def _scenario(*, new_login: int, new_daily_total: int):
    """Mature prev release across all days; new release over the last 4 days.

    Prev: 1000 events/day total, 100 login/day (10% share).
    New: ``new_daily_total`` events/day, ``new_login`` login events over the
    whole window (spread evenly across the 4 new-release days).
    """
    release_total = {
        PREV: {d: 1000 for d in DAYS},
        NEW: {d: new_daily_total for d in NEW_DAYS},
    }
    all_traffic = {d: 1000 + (new_daily_total if d in NEW_DAYS else 0) for d in DAYS}
    scope_counts = {
        "login": {
            PREV: {d: 100 for d in DAYS},
            NEW: {d: new_login // len(NEW_DAYS) for d in NEW_DAYS},
        },
    }
    return release_total, all_traffic, scope_counts


def _report(release_total, all_traffic, scope_counts, settings=None):
    return detect_release_regressions(
        release_total_by_bucket=release_total,
        all_traffic_by_bucket=all_traffic,
        scope_counts=scope_counts,
        latest_bucket=DAYS[-1],
        settings=settings or RegressionSettings(),
    )


def _run(release_total, all_traffic, scope_counts, settings=None):
    """The detected rows alone — what most of these tests assert on."""
    return _report(release_total, all_traffic, scope_counts, settings).results


def test_missing_event_in_new_release() -> None:
    # New release takes ~33% of traffic (active), but login never fires.
    results = _run(*_scenario(new_login=0, new_daily_total=500))
    assert len(results) == 1
    r = results[0]
    assert r.scope_ref == "login"
    assert r.version == NEW
    assert r.previous_version == PREV
    assert r.kind == KIND_MISSING
    assert r.observed_count == 0
    assert r.expected_count == 200.0  # total_new(2000) * share_prev(0.10)
    assert r.ratio < 0.05


def test_volume_drop_in_new_release() -> None:
    # login fires at ~40% of the expected rate -> significant drop.
    results = _run(*_scenario(new_login=80, new_daily_total=500))
    assert len(results) == 1
    r = results[0]
    assert r.kind == KIND_VOLUME_DROP
    assert r.observed_count == 80
    assert r.expected_count == 200.0
    assert 0.05 <= r.ratio <= 0.5


def test_healthy_release_has_no_regression() -> None:
    # login keeps its 10% composition share -> ratio ~1, nothing flagged.
    results = _run(*_scenario(new_login=200, new_daily_total=500))
    assert results == []


def test_build_phase_release_is_not_active() -> None:
    # New release only has dev/tester traffic (~1% share): never activates, so
    # even a totally missing event produces no signal.
    results = _run(*_scenario(new_login=0, new_daily_total=10))
    assert results == []


def test_single_active_release_yields_nothing() -> None:
    release_total = {PREV: {d: 1000 for d in DAYS}}
    all_traffic = {d: 1000 for d in DAYS}
    scope_counts = {"login": {PREV: {d: 100 for d in DAYS}}}
    assert _run(release_total, all_traffic, scope_counts) == []


def test_event_absent_in_previous_release_is_not_flagged() -> None:
    # "newthing" only exists in the new release -> share_prev 0 -> skipped,
    # while the genuinely-missing "login" is still flagged.
    release_total, all_traffic, scope_counts = _scenario(new_login=0, new_daily_total=500)
    scope_counts["newthing"] = {NEW: {d: 50 for d in NEW_DAYS}}
    results = _run(release_total, all_traffic, scope_counts)
    assert {r.scope_ref for r in results} == {"login"}


def test_below_floor_previous_release_is_not_used_as_baseline() -> None:
    # Three active-by-share releases; the middle SemVer one (2.1.0) clears the
    # share gate but its windowed volume (320) is under the raised floor (400),
    # so it must be skipped as baseline. v_prev falls back to the older 2.0.0.
    old, mid, new = "2.0.0", "2.1.0", "2.2.0"
    release_total = {
        old: {d: 500 for d in NEW_DAYS},
        mid: {d: 80 for d in NEW_DAYS},  # 320 windowed -> below the 400 floor
        new: {d: 500 for d in NEW_DAYS},
    }
    all_traffic = {d: (1080 if d in NEW_DAYS else 0) for d in DAYS}
    scope_counts = {
        "login": {
            old: {d: 50 for d in NEW_DAYS},  # 10% share in the baseline
            mid: {d: 8 for d in NEW_DAYS},
            new: {d: 0 for d in NEW_DAYS},  # gone in the newest release
        }
    }
    settings = RegressionSettings(min_release_volume=400)
    results = _run(release_total, all_traffic, scope_counts, settings)
    assert len(results) == 1
    r = results[0]
    assert r.version == new
    assert r.previous_version == old  # NOT the below-floor 2.1.0
    assert r.kind == KIND_MISSING


def test_below_floor_newest_release_falls_through_to_next_pair() -> None:
    # The newest active-by-share release (2.2.0) is under the raised floor, so
    # instead of returning nothing the analyzer drops it and evaluates the next
    # active pair: subject 2.1.0 vs baseline 2.0.0.
    old, mid, new = "2.0.0", "2.1.0", "2.2.0"
    release_total = {
        old: {d: 500 for d in NEW_DAYS},
        mid: {d: 500 for d in NEW_DAYS},
        new: {d: 80 for d in NEW_DAYS},  # 320 windowed -> below the 400 floor
    }
    all_traffic = {d: (1080 if d in NEW_DAYS else 0) for d in DAYS}
    scope_counts = {
        "login": {
            old: {d: 50 for d in NEW_DAYS},  # 10% share in the baseline
            mid: {d: 0 for d in NEW_DAYS},  # gone in the subject release
            new: {d: 8 for d in NEW_DAYS},
        }
    }
    settings = RegressionSettings(min_release_volume=400)
    results = _run(release_total, all_traffic, scope_counts, settings)
    assert len(results) == 1
    r = results[0]
    assert r.version == mid  # 2.2.0 was skipped for failing the floor
    assert r.previous_version == old
    assert r.kind == KIND_MISSING


def test_latest_two_releases_chosen_by_semver_not_lexical() -> None:
    # 2.10.0 > 2.9.0 > 2.2.0 by SemVer (lexical order would disagree).
    versions = ["2.2.0", "2.9.0", "2.10.0"]
    release_total = {v: {d: 400 for d in NEW_DAYS} for v in versions}
    all_traffic = {d: (1200 if d in NEW_DAYS else 0) for d in DAYS}
    scope_counts = {
        "login": {
            "2.2.0": {d: 40 for d in NEW_DAYS},
            "2.9.0": {d: 40 for d in NEW_DAYS},  # present in the baseline
            "2.10.0": {d: 0 for d in NEW_DAYS},  # gone in the latest
        }
    }
    results = _run(release_total, all_traffic, scope_counts)
    assert len(results) == 1
    assert results[0].version == "2.10.0"
    assert results[0].previous_version == "2.9.0"
    assert results[0].kind == KIND_MISSING


# --- comparability gate -----------------------------------------
#
# Proportions below are taken from the acme-ios 15.7.4 incident, where nine
# scopes were reported as regressions seven hours into a rollout while the app
# was healthy: 66.0% of the new release's pageviews were onboarding screens
# against 3.8% for the baseline.

# Steady-state mix: five real screens, eight onboarding screens nobody revisits,
# and a long tail. Sums to 1000/bucket.
_STEADY = {
    "main": 414,
    "spot/main": 241,
    "purchase/main": 128,
    "map/main": 108,
    "search/all": 37,
    **{f"onboarding/{i}": 5 for i in range(8)},
    "tail": 32,
}
# First hours of a rollout: the same screens exist, but the population is fresh
# installs, so onboarding carries two thirds of the volume. Sums to 300/bucket.
_FRESH_INSTALLS = {
    "main": 24,
    "spot/main": 23,
    "purchase/main": 25,
    "map/main": 18,
    "search/all": 5,
    **{f"onboarding/{i}": 25 for i in range(8)},
    "tail": 5,
}


def _mix(prev_mix: dict[str, int], new_mix: dict[str, int]):
    release_total = {
        PREV: {d: sum(prev_mix.values()) for d in DAYS},
        NEW: {d: sum(new_mix.values()) for d in NEW_DAYS},
    }
    all_traffic = {
        d: sum(prev_mix.values()) + (sum(new_mix.values()) if d in NEW_DAYS else 0) for d in DAYS
    }
    scope_counts = {
        name: {
            PREV: {d: prev_mix.get(name, 0) for d in DAYS},
            NEW: {d: new_mix.get(name, 0) for d in NEW_DAYS},
        }
        for name in set(prev_mix) | set(new_mix)
    }
    return release_total, all_traffic, scope_counts


def test_a_rollout_still_full_of_fresh_installs_is_not_judged_at_all() -> None:
    """The acme-ios 15.7.4 case: every steady-state screen looks halved."""
    report = _report(*_mix(_STEADY, _FRESH_INSTALLS))

    assert report.comparable is False
    assert report.reason == REASON_POPULATION_MISMATCH
    assert report.results == []
    # Two thirds of the new release's volume sits in scopes the baseline barely
    # visited, which is the whole signal.
    assert report.emerging_share > 0.4
    assert report.version == NEW and report.previous_version == PREV


def test_the_gate_does_not_hide_a_release_that_really_stopped_emitting_an_event() -> None:
    """The failure mode the gate must not have.

    A release that drops `main` moves the mix by main's entire 41% — a plain
    distribution-distance gate would suppress precisely the alert this analyzer
    exists to raise. Volume is destroyed rather than moved here, so the
    survivors only renormalize upward and no minor scope becomes material.
    """
    broken = {name: (0 if name == "main" else count) for name, count in _STEADY.items()}
    report = _report(*_mix(_STEADY, broken))

    assert report.comparable is True, "a real regression must still be judged"
    assert report.emerging_share == 0.0
    assert [r.scope_ref for r in report.results] == ["main"]
    assert report.results[0].kind == KIND_MISSING


# Half the traffic in scopes too small to reason about is normal for a big catalog.
_FAT_TAIL = {f"evt/{i}": 5 for i in range(100)} | {"main": 500}


def test_a_long_tail_of_small_scopes_does_not_trip_the_gate() -> None:
    report = _report(*_mix(_FAT_TAIL, _FAT_TAIL))

    assert report.comparable is True
    assert report.emerging_share == 0.0
    assert report.results == []


def test_a_fat_long_tail_does_not_hide_a_release_that_stopped_emitting_an_event() -> None:
    """The two conditions above, together — which is where the first cut broke.

    Scoring each tail scope's rise and summing it made the veto grow WITH the
    outage: half the catalog's mass sitting in tiny scopes scored 0.500 on
    `main` going silent, so the bigger the incident the more certain the
    suppression. The statistic now subtracts the common renormalization every
    survivor gets, so a destroyed scope lifts the tail without scoring at all.
    """
    report = _report(*_mix(_FAT_TAIL, {**_FAT_TAIL, "main": 0}))

    assert report.comparable is True
    assert report.emerging_share == 0.0
    assert [r.scope_ref for r in report.results] == ["main"]


def test_the_verdict_does_not_depend_on_how_finely_the_catalog_is_split() -> None:
    """Keying on "was minor before" made the answer an artefact of partitioning:
    the same incident scored 0.63 with onboarding as eight screens and 0.00 with
    it as four, which let three false rows through. Materiality is now measured
    in the NEW release, where merging scopes only makes them more material.
    """

    def split(count: int):
        prev = {n: v for n, v in _STEADY.items() if not n.startswith("onboarding/")}
        new = {n: v for n, v in _FRESH_INSTALLS.items() if not n.startswith("onboarding/")}
        prev_mass = sum(v for n, v in _STEADY.items() if n.startswith("onboarding/"))
        new_mass = sum(v for n, v in _FRESH_INSTALLS.items() if n.startswith("onboarding/"))
        for i in range(count):
            prev[f"ob/{i}"] = prev_mass // count
            new[f"ob/{i}"] = new_mass // count
        return _mix(prev, new)

    verdicts = {n: _report(*split(n)) for n in (1, 2, 4, 8)}
    assert all(not r.comparable for r in verdicts.values()), {
        n: (r.comparable, round(r.emerging_share, 3)) for n, r in verdicts.items()
    }


def test_a_thin_new_release_spread_over_a_wide_catalog_is_still_judged() -> None:
    """600 scopes each carrying a single event is granularity, not a population
    change. Summing their share rises used to reach 0.28 and veto the release."""
    wide_prev = {f"n/{i}": 2 for i in range(600)} | {"main": 1000}
    wide_new = {f"n/{i}": 1 for i in range(600)} | {"main": 0}
    report = _report(*_mix(wide_prev, wide_new))

    assert report.comparable is True
    assert [r.scope_ref for r in report.results] == ["main"]


# --- one verdict across scope partitions -------------------------
#
# The recalculation layer judges the same release twice, once per scope
# partition, and event types are a coarsening of events. Nothing used to hold
# the two passes to the same comparability answer.


def _two_partitions(per_type: dict[str, tuple[int, int, int]]):
    """An event-level partition and its exact event-type coarsening.

    ``per_type`` maps a type ref to ``(events in the type, prev count per
    bucket per event, new count per bucket per event)``. Both partitions
    describe the same traffic and therefore share one
    ``release_total_by_bucket``, which is what makes any disagreement between
    them a disagreement about the composition statistic and nothing else.
    """
    events_prev, events_new, types_prev, types_new = {}, {}, {}, {}
    for type_ref, (count, prev, new) in per_type.items():
        types_prev[type_ref] = count * prev
        types_new[type_ref] = count * new
        for i in range(count):
            events_prev[f"{type_ref}/{i}"] = prev
            events_new[f"{type_ref}/{i}"] = new
    release_total, all_traffic, event_counts = _mix(events_prev, events_new)
    _, _, type_counts = _mix(types_prev, types_new)
    return release_total, all_traffic, event_counts, type_counts


# The acme-ios population change on a catalog fine enough to hide it: a rollout
# of fresh installs pours two thirds of its volume into onboarding, but that
# volume is spread over 1000 events, so no single event is seen even 30 times in
# the window and the event-scope statistic has nothing to stand on.
_FINE_CATALOG = {
    "type/steady": (1400, 7, 1),
    "type/onboarding": (1000, 0, 4),
    "type/purchase": (200, 1, 3),
}


def _by_scope(release_total, all_traffic, partitions, settings=None):
    return detect_release_regressions_by_scope(
        release_total_by_bucket=release_total,
        all_traffic_by_bucket=all_traffic,
        scope_counts_by_scope_type=partitions,
        latest_bucket=DAYS[-1],
        settings=settings or RegressionSettings(),
    )


def test_the_two_scope_partitions_are_held_to_a_single_verdict() -> None:
    """Judged apart the two partitions contradict each other on the same release.

    Comparability is a property of the RELEASE, so one of those answers has to
    win for both. It has to be the partition that SAW the change: a scope below
    the volume floor contributes nothing, so every partition understates
    emergence and the sparser one understates it most.
    """
    release_total, all_traffic, event_counts, type_counts = _two_partitions(_FINE_CATALOG)

    apart = {
        "event": _report(release_total, all_traffic, event_counts),
        "event_type": _report(release_total, all_traffic, type_counts),
    }
    assert apart["event"].comparable is True
    assert apart["event"].emerging_share == 0.0, "no event clears the 30-event floor"
    assert apart["event_type"].comparable is False
    assert apart["event_type"].emerging_share > 0.6

    together = _by_scope(
        release_total, all_traffic, {"event": event_counts, "event_type": type_counts}
    )
    assert {r.comparable for r in together.values()} == {False}
    assert {r.reason for r in together.values()} == {REASON_POPULATION_MISMATCH}
    # And on the number that decided it, so a reader is never shown
    # ``comparable=False`` next to a share under the bound.
    assert {r.emerging_share for r in together.values()} == {apart["event_type"].emerging_share}


def test_the_sparser_partition_cannot_wave_through_the_row_the_denser_one_vetoed() -> None:
    """Why "decide once on the finest partition available" is the wrong rule.

    The event scope returns comparable here only because it measured nothing at
    all. Letting that verdict govern the type pass would persist a volume_drop
    on the steady-state type of a healthy app — the acme-ios false alarm, one
    partition up from where it was fixed.
    """
    release_total, all_traffic, event_counts, type_counts = _two_partitions(_FINE_CATALOG)

    ungated = _report(
        release_total, all_traffic, type_counts, RegressionSettings(max_emerging_share=1.0)
    )
    assert [(r.scope_ref, r.kind) for r in ungated.results] == [("type/steady", KIND_VOLUME_DROP)]

    together = _by_scope(
        release_total, all_traffic, {"event": event_counts, "event_type": type_counts}
    )
    assert together["event_type"].results == []


def test_a_partition_with_nothing_to_measure_cannot_veto_a_comparable_release() -> None:
    """The other direction, which a naive "all partitions must agree" would break.

    A scan whose event-type scope has no breakdown rows of its own is ordinary.
    Absent evidence has to stay absent evidence rather than become a veto, and
    taking the maximum gives that for free — an empty partition scores 0.0 and
    cannot move a maximum.
    """
    release_total, all_traffic, scope_counts = _mix(_STEADY, {**_STEADY, "main": 0})

    together = _by_scope(release_total, all_traffic, {"event": scope_counts, "event_type": {}})
    assert {r.comparable for r in together.values()} == {True}
    assert {r.reason for r in together.values()} == {REASON_COMPARABLE}
    assert [r.scope_ref for r in together["event"].results] == ["main"]
    assert together["event_type"].results == []


def test_a_shared_suppression_still_reports_silent_scopes_at_every_partition() -> None:
    """Suppression withholds composition-normalized findings, not silent events.

    A scope that went to zero is not something a different mix of users
    explains, and the shared verdict must not quietly widen what gets dropped.
    """
    silent = _FINE_CATALOG | {"type/legacy": (1, 100, 0)}
    release_total, all_traffic, event_counts, type_counts = _two_partitions(silent)

    together = _by_scope(
        release_total, all_traffic, {"event": event_counts, "event_type": type_counts}
    )
    assert {r.comparable for r in together.values()} == {False}
    assert [(r.scope_ref, r.kind) for r in together["event_type"].results] == [
        ("type/legacy", KIND_MISSING)
    ]
    assert [(r.scope_ref, r.kind) for r in together["event"].results] == [
        ("type/legacy/0", KIND_MISSING)
    ]


def test_a_release_with_no_baseline_aborts_identically_at_every_partition() -> None:
    """The two pre-comparison exits are decided from release volumes alone, so
    they were never able to disagree. Pinned so that stays true."""
    release_total = {NEW: {d: 1000 for d in NEW_DAYS}}
    all_traffic = {d: 1000 if d in NEW_DAYS else 0 for d in DAYS}

    together = _by_scope(release_total, all_traffic, {"event": {}, "event_type": {}})
    assert {r.comparable for r in together.values()} == {False}
    assert {r.reason for r in together.values()} == {REASON_NO_BASELINE}
    # Frozen report, mutable ``results``: the two must not share one list.
    assert together["event"].results is not together["event_type"].results


def test_an_event_that_went_completely_silent_survives_the_suppression() -> None:
    """A different mix of users cannot manufacture a silent event, and that
    finding is too expensive to swallow — so `missing` rows are reported even
    when the composition-normalized ones are withheld. Every row in the
    acme-ios false alarm was a `volume_drop`, so this costs nothing there.
    """
    report = _report(*_mix(_STEADY, {**_FRESH_INSTALLS, "main": 0}))

    assert report.comparable is False, "the population is still incomparable"
    assert [(r.scope_ref, r.kind) for r in report.results] == [("main", KIND_MISSING)]


def test_a_release_that_loses_most_of_its_volume_is_still_reported() -> None:
    """The case the gate's own logic gets wrong on its face: losing 90% of the
    volume lifts each survivor tenfold, past the slack, so the comparison is
    ruled incomparable (emerging ~0.66). The silent events are reported anyway.
    """
    collapsed = {name: 0 for name in _STEADY} | {"search/all": 37, "tail": 32}
    report = _report(*_mix(_STEADY, collapsed))

    assert report.comparable is False
    assert {r.scope_ref for r in report.results} == {"main", "spot/main", "purchase/main"}
    assert all(r.kind == KIND_MISSING for r in report.results)


def test_without_the_gate_this_scenario_is_the_incident_itself() -> None:
    """Pins what the gate is worth: the same inputs, bound relaxed, reproduce
    the acme-ios failure — a fistful of healthy screens all reported as
    regressions at once. That also proves the suppression above is the gate's
    doing and not some other filter quietly swallowing the rows.
    """
    args = _mix(_STEADY, _FRESH_INSTALLS)
    assert _report(*args).comparable is False

    relaxed = _report(*args, RegressionSettings(max_emerging_share=0.99))
    assert relaxed.comparable is True
    flagged = {r.scope_ref for r in relaxed.results}
    assert len(flagged) >= 3, f"expected the multi-screen false alarm, got {flagged}"
    assert "main" in flagged and "spot/main" in flagged


def test_a_fine_grained_catalog_is_protected_too() -> None:
    """The shape that still alerted after the gate shipped.

    "Snowplow Events (iOS)" carries 2488 events, so no single one reaches 1% of
    a release. A share-based materiality floor is therefore a statement about
    catalog granularity rather than about evidence, and it scored 0.0000 on the
    same population change it scores 0.4667 on for the 92-event pageviews scan.
    The floor counts events now, so both shapes are covered.
    """
    steady = {f"evt/{i}": 100 for i in range(800)} | {f"ob/{i}": 2 for i in range(200)}
    fresh = {f"evt/{i}": 10 for i in range(800)} | {f"ob/{i}": 40 for i in range(200)}
    report = _report(*_mix(steady, fresh))

    largest = max(fresh.values()) / sum(fresh.values())
    assert largest < 0.01, "the point of the fixture is that nothing is 1% of the release"
    assert report.comparable is False
    assert report.emerging_share > 0.25


# --- evidence gate: counts, not share of the baseline ------------------------


def test_a_scope_far_below_a_percent_of_the_baseline_is_still_evidence() -> None:
    """The shape a share floor on the BASELINE cannot see.

    ``:open:detailed_forecast`` fired 65 times per bucket in the baseline of the
    2488-event "Snowplow Events (iOS)" scan and went silent in the next release.
    At that catalog size it is 0.00065 of the release, so the 0.001 share floor
    this analyzer used to carry dropped it — along with 145 of the 264 scopes
    that had the evidence to be judged, being ~496x stricter than ``min_expected``
    already was. Evidence is a count: 65 per bucket against a rollout of the same
    volume is an expected 260, far past the floor that decides now.
    """
    per_bucket = 100_000
    release_total = {
        PREV: {d: per_bucket for d in DAYS},
        NEW: {d: per_bucket for d in NEW_DAYS},
    }
    all_traffic = {d: per_bucket + (per_bucket if d in NEW_DAYS else 0) for d in DAYS}
    scope_counts = {
        "open:detailed_forecast": {
            PREV: {d: 65 for d in DAYS},
            NEW: {d: 0 for d in NEW_DAYS},
        },
        # Everything else, so the catalog has somewhere to keep its mass.
        "main": {
            PREV: {d: per_bucket - 65 for d in DAYS},
            NEW: {d: per_bucket for d in NEW_DAYS},
        },
    }
    report = _report(release_total, all_traffic, scope_counts)

    assert 65 / per_bucket < 0.001, "the fixture is only interesting sub-0.1%"
    assert report.comparable is True
    assert [(r.scope_ref, r.kind) for r in report.results] == [
        ("open:detailed_forecast", KIND_MISSING)
    ]
    assert report.results[0].expected_count == pytest.approx(260.0)


def _lopsided(prev_sightings: int):
    """A release out-trafficking its baseline 24x, as live acme-ios does.

    Adjacent active releases there measured 35,380,595 against 1,475,687 events
    in the same window — the baseline decays out of the 14-day window while the
    rollout runs at full volume. ``prev_sightings`` is how many times the scope
    under test was seen in that whole baseline; it fires zero times in the new
    release.
    """
    # Counts are summed over the ACTIVATION-ANCHORED window, not the whole
    # series, so both sides of the ratio and every sighting have to sit inside
    # the rollout overlap — the tests below assert that they do.
    seen_on = NEW_DAYS[0]
    prev_per_bucket = 375_000
    new_per_bucket = 9_000_000
    release_total = {
        PREV: {d: prev_per_bucket for d in DAYS},
        NEW: {d: new_per_bucket for d in NEW_DAYS},
    }
    all_traffic = {d: prev_per_bucket + (new_per_bucket if d in NEW_DAYS else 0) for d in DAYS}
    scope_counts = {
        "debug:ping": {
            PREV: {seen_on: prev_sightings},
            NEW: {d: 0 for d in NEW_DAYS},
        },
        # Everything else, so the catalog's mass has somewhere to live.
        "main": {
            PREV: {d: prev_per_bucket - (prev_sightings if d == seen_on else 0) for d in DAYS},
            NEW: {d: new_per_bucket for d in NEW_DAYS},
        },
    }
    total_prev = prev_per_bucket * len(NEW_DAYS)
    total_new = new_per_bucket * len(NEW_DAYS)
    return (release_total, all_traffic, scope_counts), total_prev, total_new, seen_on


def test_two_sightings_in_a_thin_baseline_are_not_evidence() -> None:
    """``expected`` is not a floor on evidence when traffic is lopsided.

    It is ``total_new × prev_count / total_prev``, so it inflates with the
    traffic ratio: at the 24x measured between two live adjacent releases, a
    scope seen TWICE in the entire baseline window implies an expectation of 48
    and clears a floor of 30 on those two sightings. The floor loosens exactly
    as the baseline decays, which is the wrong direction.

    ``prev_count`` — how often the scope was actually seen — is what Poisson
    noise is a function of, so it carries its own floor. This matters more than
    the arithmetic suggests: suppression deliberately passes ``missing`` rows
    through, and every persisted row becomes an alert candidate.
    """
    fixture, total_prev, total_new, _seen_on = _lopsided(prev_sightings=2)
    report = _report(*fixture)

    # The number that would have let this through, stated rather than implied.
    assert total_new / total_prev == pytest.approx(24.0)
    assert total_new * 2 / total_prev == pytest.approx(48.0)
    assert report.comparable is True
    assert report.results == []


def test_a_baseline_seen_often_enough_is_still_reported_when_it_goes_silent() -> None:
    """The floor is on evidence, not on thinness — the same lopsided release
    still reports a scope that was genuinely established in the baseline.

    Also pins the fixture itself: the sighting has to fall inside the
    activation-anchored window, or both of these tests would pass for the
    uninteresting reason that nothing was counted at all.
    """
    fixture, _total_prev, _total_new, seen_on = _lopsided(prev_sightings=30)
    report = _report(*fixture)

    assert [(r.scope_ref, r.kind) for r in report.results] == [("debug:ping", KIND_MISSING)]
    row = report.results[0]
    assert row.window_from <= seen_on <= row.window_to
    assert row.expected_count == pytest.approx(720.0)


# --- a withheld verdict must stay distinguishable from a clean one -----------


def test_a_lone_active_release_is_not_a_clean_bill_of_health() -> None:
    """Nothing was compared, so nothing can be affirmed. This path used to
    inherit the dataclass default ``comparable=True``, which is what made a
    suppressed payload and a healthy payload identical."""
    release_total = {PREV: {d: 1000 for d in DAYS}}
    all_traffic = {d: 1000 for d in DAYS}
    scope_counts = {"login": {PREV: {d: 100 for d in DAYS}}}
    report = _report(release_total, all_traffic, scope_counts)

    assert report.results == []
    assert report.comparable is False
    assert report.reason == REASON_NO_BASELINE
    assert report.version is None and report.previous_version is None


def test_a_baseline_with_no_volume_in_the_window_is_not_a_clean_bill_of_health() -> None:
    """Both releases activated, but the baseline had already gone quiet by the
    time the subject's activation anchored the comparison window, so there is no
    composition to normalize against — the other path that used to return the
    affirmative default."""
    release_total = {
        PREV: {d: 1000 for d in DAYS[:5]},
        NEW: {d: 1000 for d in NEW_DAYS},
    }
    all_traffic = {d: 1000 for d in DAYS}
    scope_counts = {
        "login": {
            PREV: {d: 100 for d in DAYS[:5]},
            NEW: {d: 0 for d in NEW_DAYS},
        }
    }
    report = _report(release_total, all_traffic, scope_counts)

    assert report.results == []
    assert report.comparable is False
    assert report.reason == REASON_BASELINE_NO_VOLUME
    assert report.version == NEW and report.previous_version == PREV


def test_a_healthy_comparison_says_so_explicitly() -> None:
    report = _report(*_scenario(new_login=200, new_daily_total=500))

    assert report.results == []
    assert report.comparable is True
    assert report.reason == REASON_COMPARABLE


# --- prerelease builds are ineligible as subject AND as baseline -------------


def _three_way(counts_by_version: dict[str, dict[str, int]], per_bucket: int):
    """Releases running side by side over the last four buckets, each at
    ``per_bucket`` events per bucket with the given per-scope split."""
    release_total = {v: {d: per_bucket for d in NEW_DAYS} for v in counts_by_version}
    all_traffic = {d: (per_bucket * len(counts_by_version) if d in NEW_DAYS else 0) for d in DAYS}
    scopes = {name for mix in counts_by_version.values() for name in mix}
    scope_counts = {
        name: {
            version: {d: mix.get(name, 0) for d in NEW_DAYS}
            for version, mix in counts_by_version.items()
        }
        for name in scopes
    }
    return release_total, all_traffic, scope_counts


def test_a_prerelease_is_never_the_release_under_test() -> None:
    """A TestFlight build that takes real traffic clears the share gate, so
    maturity alone cannot exclude it: the app-version chart named 15.7.4 as the
    latest active release while this analyzer judged 15.8.0-beta.1 against it
    and emitted a missing row for an event the beta simply never sent.
    """
    args = _three_way(
        {
            "15.7.3": {"login": 40, "main": 360},
            "15.7.4": {"login": 40, "main": 360},
            "15.8.0-beta.1": {"login": 0, "main": 400},
        },
        per_bucket=400,
    )
    report = _report(*args)

    assert report.version == "15.7.4"
    assert report.previous_version == "15.7.3"
    assert report.results == []


def test_a_prerelease_is_never_the_baseline() -> None:
    """The other half, and the more expensive one: a tester build's composition
    is not the app's. Judged against 15.7.4-beta.1 the diagnostics screen looks
    like a 62% volume drop, and the comparability gate scores 0.0 on it because
    no scope in the shipped release gained on the beta — nothing else catches it.
    """
    beta_heavy = {"diagnostics": 800} | {f"screen/{i}": 40 for i in range(5)}
    shipped = {"diagnostics": 300} | {f"screen/{i}": 140 for i in range(5)}
    report = _report(
        *_three_way(
            {"15.7.3": shipped, "15.7.4-beta.1": beta_heavy, "15.7.4": shipped},
            per_bucket=1000,
        )
    )

    assert report.version == "15.7.4"
    assert report.previous_version == "15.7.3"
    assert report.results == []

    # The same numbers with that baseline relabelled as a shipped release. The
    # row the beta produced is real arithmetic that only eligibility withholds,
    # not something another gate was quietly swallowing — emerging_share is 0.0,
    # so the comparability gate had nothing to say about it.
    as_if_released = _report(
        *_three_way({"15.7.2": beta_heavy, "15.7.4": shipped}, per_bucket=1000)
    )
    assert as_if_released.emerging_share == 0.0
    assert [(r.scope_ref, r.kind) for r in as_if_released.results] == [
        ("diagnostics", KIND_VOLUME_DROP)
    ]


def test_the_scans_own_prerelease_pattern_is_honoured() -> None:
    """``ScanConfig.app_version_prerelease_pattern`` exists for builds SemVer
    calls released — a ``+internal`` build tag carries no prerelease component —
    and was a no-op here until the pattern was threaded through."""
    args = _three_way(
        {
            "15.7.3": {"login": 40, "main": 360},
            "15.7.4": {"login": 40, "main": 360},
            "15.9.0+internal": {"login": 0, "main": 400},
        },
        per_bucket=400,
    )

    assert _report(*args).version == "15.9.0+internal", "SemVer alone sees a released build"

    report = _report(*args, RegressionSettings(prerelease_pattern=re.compile(r"\+internal$")))
    assert report.version == "15.7.4"
    assert report.previous_version == "15.7.3"
    assert report.results == []
