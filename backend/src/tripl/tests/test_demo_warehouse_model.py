"""The demo warehouse model, pinned by its invariants (no database).

Pins what the demo promises rather than numbers copied from output:

* the catalog is large, well formed and keeps the names the rest of the
  product depends on;
* the warehouse holds ~800-1,500 rows an hour;
* every daily incident is detected by the real detector under the demo's
  anomaly settings, and quiet series stay quiet;
* at ANY instant a demo could be created, an incident ended within a day;
* the weekly promo, the release regressions, the property type change, the mix
  shifts and the unplanned bursts are all in the rows;
* the in-memory engine answers what the rows say, in PostgreSQL's shapes.
"""

from __future__ import annotations

import collections
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tripl.config import settings
from tripl.core.adapters import registry
from tripl.core.adapters.demo_memory import MAX_EVENTS_WINDOW, DemoMemoryAdapter
from tripl.core.adapters.demo_postgres import DemoPostgresAdapter
from tripl.core.adapters.measure_validator import SqlDialect, dialect_for_db_type
from tripl.core.adapters.synthetic import SyntheticCapabilityError
from tripl.core.analyzers.anomaly_detector import (
    AnomalyDetectionSettings,
    SeriesPoint,
    detect_anomalies,
)
from tripl.core.analyzers.distribution_drift import compute_psi
from tripl.core.analyzers.release_regression import (
    RegressionSettings,
    detect_release_regressions_by_scope,
)
from tripl.core.demo_warehouse import calendar, roster, rows, schema, traffic
from tripl.models.data_source import DEMO_DB_TYPES, DBType
from tripl.schemas.data_source import CONNECTION_SETTINGS_MODELS
from tripl.services.demo import noise, pipeline
from tripl.services.release_annotations import releases_to_annotate

HOUR = calendar.HOUR
#: A fixed "now", mid-hour and mid-slice, so nothing depends on the wall clock.
NOW = datetime(2026, 10, 15, 12, 7, 30, tzinfo=UTC)
END = datetime(2026, 10, 15, tzinfo=UTC)
#: The demo's anomaly settings (sigma 3, at least 10 expected, no settling).
DEMO_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    min_expected_count=10,
)
_EVENT_TYPE = {event.name: event.event_type for event in roster.events()} | {
    unplanned.name: unplanned.event_type for unplanned in roster.unplanned_events()
}


@pytest.fixture(scope="module")
def hourly_counts() -> dict[datetime, dict[tuple[str, str], int]]:
    """Rows per (event, platform) for every hour of the 31 days before ``END``."""
    counts: dict[datetime, dict[tuple[str, str], int]] = {}
    hour = END - timedelta(days=31)
    while hour < END:
        counts[hour] = rows.hour_counts(hour)
        hour += HOUR
    return counts


def _series(
    counts: dict[datetime, dict[tuple[str, str], int]], scope: str, target: str
) -> list[SeriesPoint]:
    def total(by_event: dict[tuple[str, str], int]) -> int:
        if scope == "event_type":
            return sum(n for (name, _), n in by_event.items() if _EVENT_TYPE[name] == target)
        return sum(n for (name, _), n in by_event.items() if name == target)

    return [SeriesPoint(bucket=bucket, count=total(c)) for bucket, c in counts.items()]


# -- the catalog -------------------------------------------------------------


def test_the_catalog_is_large_and_well_formed() -> None:
    events = roster.events()
    names = [event.name for event in events]
    type_names = {event_type.name for event_type in roster.event_types()}

    assert 150 <= len(events) <= 200
    assert len(set(names)) == len(names)
    assert 8 <= len(type_names) <= 12
    assert {event.event_type for event in events} <= type_names
    assert 50 <= len(roster.live_events()) <= 70
    statuses = {"live", "implemented", "in_review", "ready_for_dev", "draft"}
    assert {event.status for event in events} <= statuses | {"deprecated", "archived"}
    for event in events:
        assert len(event.description) >= 40, event.name
        assert "lorem" not in event.description.lower(), event.name
        fields = {field.name for field in roster.event_type(event.event_type).fields}
        assert {name for name, _ in event.field_values} <= fields, event.name
    # A firing event is in the plan as built (or about to be); a planned one is silent.
    for event in roster.live_events():
        assert event.status not in {"draft", "ready_for_dev"}, event.name


def test_the_names_the_product_depends_on_are_kept() -> None:
    names = {event.name for event in roster.events()}
    assert set(roster.KEPT_EVENT_NAMES) <= names
    assert set(roster.KEPT_EVENT_TYPES) <= {et.name for et in roster.event_types()}
    assert roster.SHADOW_CANDIDATE_NAME not in names
    assert roster.SHADOW_CANDIDATE_NAME in {u.name for u in roster.unplanned_events()}
    assert roster.event(roster.DEAD_EVENT).base_rate == 0
    for pair in roster.DUPLICATE_PAIRS:
        assert set(pair) <= names
    assert roster.event("Promo Applied").status == "deprecated"
    assert roster.event("Promo Applied").successor in names
    assert roster.event("Legacy CTA Click").status == "archived"
    drift = roster.event(roster.VARIABLE_DRIFT_EVENT)
    assert roster.UNDOCUMENTED_PRODUCT in {product for product, _ in drift.products}
    product_variable = next(v for v in roster.variables() if v.name == "product_id")
    assert roster.UNDOCUMENTED_PRODUCT not in product_variable.allowed_values


def test_descriptions_carry_the_words_people_search_with() -> None:
    assert "money back" in roster.event("Refund Processed").description


# -- volume --------------------------------------------------------------------


def test_the_warehouse_holds_800_to_1500_rows_an_hour(
    hourly_counts: dict[datetime, dict[tuple[str, str], int]],
) -> None:
    totals = [sum(c.values()) for c in hourly_counts.values()]
    mean = sum(totals) / len(totals)
    inside = sum(800 <= total <= 1500 for total in totals) / len(totals)
    assert 1000 <= mean <= 1300
    assert inside >= 0.95
    assert min(totals) >= 700 and max(totals) <= 1700


# -- incidents -------------------------------------------------------------------


def test_one_incident_a_day_rotating_through_every_kind() -> None:
    incidents = calendar.incidents_between(END - timedelta(days=30), END)
    days = [incident.start.date() for incident in incidents]
    assert len(days) == len(set(days)) == 30
    assert {incident.kind for incident in incidents[:5]} == set(calendar.INCIDENT_KINDS)
    assert len(calendar.INCIDENT_KINDS) >= 4
    for incident in incidents:
        assert timedelta(hours=2) <= incident.end - incident.start <= timedelta(hours=3)
        assert incident.description


#: Every (kind, target) pair the calendar can put on a day.
_ALL_PAIRS = {
    (kind, target) for kind, targets in calendar.INCIDENT_TARGETS.items() for target in targets
}
_ROTATION = timedelta(days=calendar.INCIDENT_ROTATION_DAYS)


def test_a_full_rotation_brings_every_kind_to_every_target() -> None:
    rotation = calendar.incidents_between(END - _ROTATION, END)
    assert len(rotation) == calendar.INCIDENT_ROTATION_DAYS
    assert {(incident.kind, incident.target) for incident in rotation} == _ALL_PAIRS
    # ...and then the calendar repeats itself.
    previous = calendar.incidents_between(END - 2 * _ROTATION, END - _ROTATION)
    assert [(i.kind, i.target) for i in previous] == [(i.kind, i.target) for i in rotation]


def test_every_daily_incident_is_detected(
    hourly_counts: dict[datetime, dict[tuple[str, str], int]],
) -> None:
    """A whole rotation, so every (kind, target) pair is put to the detector."""
    incidents = calendar.incidents_between(END - _ROTATION, END - HOUR)
    assert {(incident.kind, incident.target) for incident in incidents} == _ALL_PAIRS
    for incident in incidents:
        points = [p for p in _series(hourly_counts, incident.scope, incident.target)]
        result = detect_anomalies(
            points,
            interval=HOUR,
            evaluation_start=incident.start,
            evaluation_end=incident.end,
            settings=DEMO_SETTINGS,
            settling_buckets=0,
        )
        direction = "drop" if incident.multiplier < 1 else "spike"
        flagged = [a for a in result.anomalies if a.direction == direction]
        assert flagged, f"{incident.description} on {incident.start:%Y-%m-%d} was not detected"
        for anomaly in flagged:
            assert anomaly.expected_count >= DEMO_SETTINGS.min_expected_count


def test_series_without_a_story_stay_quiet(
    hourly_counts: dict[datetime, dict[tuple[str, str], int]],
) -> None:
    """A week of the busiest event types: anomalies only where the calendar put one."""
    start = END - timedelta(days=7)
    explained: list[tuple[datetime, datetime]] = [
        (incident.start, incident.end)
        for incident in calendar.incidents_between(start - timedelta(days=1), END)
    ] + calendar.promo_windows(start, END)
    unexplained = 0
    for event_type in ("screen_view", "app_lifecycle", "click"):
        result = detect_anomalies(
            _series(hourly_counts, "event_type", event_type),
            interval=HOUR,
            evaluation_start=start,
            evaluation_end=END,
            settings=DEMO_SETTINGS,
            settling_buckets=0,
        )
        unexplained += sum(
            not any(begin <= a.bucket < end for begin, end in explained) for a in result.anomalies
        )
    # Three scopes x 168 hours: a couple of 3-sigma flukes at most.
    assert unexplained <= 3


def _instants(span: timedelta, step: timedelta) -> Iterator[datetime]:
    at = END - span
    while at < END:
        yield at
        at += step


def test_a_demo_created_at_any_moment_has_an_incident_within_a_day() -> None:
    for at in _instants(timedelta(days=30), timedelta(minutes=5)):
        finished = [
            incident
            for incident in calendar.incidents_between(at - timedelta(days=2), at)
            if incident.start + HOUR <= at  # at least one complete anomalous bucket
        ]
        assert finished, at
        latest = finished[-1]
        assert at - min(latest.end, at) <= timedelta(hours=24), at


def test_a_demo_built_in_any_hour_alerts_on_a_detected_incident(
    hourly_counts: dict[datetime, dict[tuple[str, str], int]],
) -> None:
    """The build's scoring pass ends on an incident hour the detector flags.

    The provisioning tests (the contract's newest-incident check, the flaky
    sink's delivery) run on the wall clock, so they see the hour they happen
    to run in. This pins the same promise for every hour of a whole rotation:
    the pass ends one bucket after the earliest complete incident hour at
    most 22 hours old (23 on the hour a two-hour incident's successor starts),
    and that bucket is flagged in the incident's direction. The flaky sink's rule
    takes every event and event-type volume signal, spikes and drops alike, so
    a flagged head is a delivery on both sinks whichever incident it is.
    """
    flagged_heads: dict[tuple[str, str, datetime], bool] = {}
    # Hours whose head is 23 hours old: its signal stays open only until the
    # seed hour ends. One per two-hour incident (six in a rotation).
    edge_hours: list[datetime] = []
    for at in _instants(_ROTATION, HOUR):
        now = at + timedelta(minutes=7, seconds=30)
        _scored_from, scored_to = pipeline.scoring_window(now)
        head = scored_to - HOUR
        [incident] = [
            i for i in calendar.incidents_between(head, scored_to) if i.start <= head < i.end
        ]
        assert incident.scope in {"event", "event_type"}, incident
        assert now - head <= timedelta(hours=24), now
        if now - head > timedelta(hours=23):
            edge_hours.append(now)
        key = (incident.scope, incident.target, head)
        if key not in flagged_heads:
            points = [
                p
                for p in _series(hourly_counts, incident.scope, incident.target)
                if p.bucket < scored_to
            ]
            result = detect_anomalies(
                points,
                interval=HOUR,
                evaluation_start=head,
                evaluation_end=scored_to,
                settings=DEMO_SETTINGS,
                settling_buckets=0,
            )
            direction = "drop" if incident.multiplier < 1 else "spike"
            flagged_heads[key] = any(
                a.bucket == head and a.direction == direction for a in result.anomalies
            )
        assert flagged_heads[key], (
            f"a demo built at {now} ends on {incident.description}, unflagged"
        )
    two_hour_incidents = sum(
        1
        for incident in calendar.incidents_between(END - _ROTATION, END)
        if incident.end - incident.start == 2 * HOUR
    )
    assert len(edge_hours) <= two_hour_incidents, edge_hours


# -- the other stories -----------------------------------------------------------------


def test_the_weekly_promo_lifts_the_paywall_on_tuesdays() -> None:
    windows = calendar.promo_windows(END - timedelta(days=28), END)
    assert len(windows) == 4
    paywall = roster.event(calendar.PROMO_EVENT)
    for begin, _ in windows:
        assert begin.weekday() == calendar.PROMO_WEEKDAY
        during = traffic.expected_total(paywall, begin)
        day_before = traffic.expected_total(paywall, begin - timedelta(days=1))
        assert during > 2 * day_before


@pytest.mark.parametrize("releases_back", range(6))
def test_each_release_ships_its_regression(releases_back: int) -> None:
    # Six consecutive releases, so both kinds (missing, volume_drop) and every
    # target of the rotation are exercised, not just whichever END lands on.
    release = calendar.release_index(END) - releases_back
    regression = calendar.regression_for(release)
    # Three days after release the new version runs on most devices.
    window_start = calendar.release_start(release) + timedelta(days=3)
    by_version: collections.Counter[tuple[str, bool]] = collections.Counter()
    for row in rows.rows_between("events", window_start, window_start + timedelta(days=1)):
        by_version[(str(row["event_name"]), row["app_version"] == regression.version)] += 1

    def new_to_old(event_name: str) -> float:
        return by_version[(event_name, True)] / max(by_version[(event_name, False)], 1)

    healthy = new_to_old("Home Screen View")
    assert healthy > 1
    assert by_version[(regression.event, False)] > 0
    if regression.kind == "missing":
        assert by_version[(regression.event, True)] == 0
    else:
        # It keeps 40 % of its rows on the new version: well under the others.
        assert new_to_old(regression.event) < 0.6 * healthy


def test_broken_releases_send_results_count_as_a_string_on_ios() -> None:
    # A day after a release, the new and the previous version share the users,
    # and one of each pair of consecutive releases is broken.
    start = calendar.release_start(calendar.release_index(END)) + timedelta(days=2)
    types: collections.Counter[tuple[str, str]] = collections.Counter()
    for row in rows.rows_between("events", start, start + timedelta(hours=6)):
        properties = row["properties"]
        if isinstance(properties, dict) and calendar.PROPERTY_TYPE_CHANGE_KEY in properties:
            value = properties[calendar.PROPERTY_TYPE_CHANGE_KEY]
            types[(str(row["platform"]), type(value).__name__)] += 1
    assert types[("ios", "str")] > 0
    assert types[("ios", "int")] > 0
    assert types[("android", "str")] == types[("web", "str")] == 0


def test_mix_shifts_flag_psi_where_they_start_and_end() -> None:
    windows = calendar.mix_shift_windows(END - timedelta(days=28), END)
    assert {shift.name for shift, _, _ in windows} == {s.name for s in calendar.MIX_SHIFTS}
    shift, begin, end = windows[-1]

    def mix(t0: datetime, t1: datetime) -> dict[str, int]:
        counts: collections.Counter[str] = collections.Counter()
        for row in rows.rows_between("events", t0, t1):
            if row["event_type"] in shift.event_types:
                counts[str(row[shift.column])] += 1
        return dict(counts)

    onset = begin + timedelta(hours=shift.ramp_hours)
    assert compute_psi(mix(onset - 14 * HOUR, onset), mix(onset, onset + HOUR)).band == (
        "significant"
    )
    middle = begin + (end - begin) / 2
    assert compute_psi(mix(middle - 14 * HOUR, middle), mix(middle, middle + HOUR)).band != (
        "significant"
    )


def test_unplanned_names_fire_only_in_their_bursts() -> None:
    start = END - timedelta(days=7)
    bursts = calendar.unplanned_bursts(start, END)
    assert {name for name, _, _ in bursts} == {u.name for u in roster.unplanned_events()}
    for name, begin, end in bursts[:3]:
        inside = sum(
            1 for row in rows.rows_between("events", begin, end) if row["event_name"] == name
        )
        before = sum(
            1
            for row in rows.rows_between("events", begin - 3 * HOUR, begin)
            if row["event_name"] == name
        )
        assert inside > 0 and before == 0, name


def test_the_dead_event_never_fires() -> None:
    names = {row["event_name"] for row in rows.rows_between("events", END - HOUR * 6, END)}
    assert roster.DEAD_EVENT not in names
    assert "Promo Applied" in {e.name for e in roster.live_events()}


# -- what a demo built at any hour of a release cycle shows ------------------------------

#: A whole release cycle and the next release's ramp: 6.21.0 (a ``volume_drop``)
#: ships on its first day, 6.22.0 (a ``missing``) on the fifteenth.
_CYCLE_START = calendar.release_start(calendar.release_index(END) + 1).replace(hour=0)
_CYCLE = calendar.RELEASE_INTERVAL + timedelta(days=3)


def _builds(step: timedelta) -> Iterator[datetime]:
    """Build instants across :data:`_CYCLE`, mid-hour, ``step`` apart."""
    at = _CYCLE_START + timedelta(minutes=7, seconds=30)
    while at < _CYCLE_START + _CYCLE:
        yield at
        at += step


@pytest.fixture
def _full_history(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped history; the suite's two days start on the hour.
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)


@pytest.mark.usefixtures("_full_history")
def test_every_build_hour_scores_three_promo_sends_from_their_first_hour() -> None:
    """The weekly suggestion needs three *expected* verdicts, one a week.

    A send is flagged only while the hour-of-week baseline has fewer than three
    weeks of sends behind it, so the verdicts come from the history's first 21
    days. For every build hour of a week: those days hold three sends, none in
    the series' first ``min_history_buckets`` hours (no baseline at all), and
    the build's one detection pass scores every one of them.
    """
    for hour in range(24 * 7):
        now = _CYCLE_START + timedelta(hours=hour, minutes=7, seconds=30)
        start = noise.history_start(now)
        sends = calendar.promo_windows(start, start + timedelta(days=21))
        assert len(sends) == 3, now
        assert all(begin >= start for begin, _ in sends), now
        unscored = timedelta(hours=noise.DEMO_ANOMALY_SETTINGS.min_history_buckets)
        assert min(begin for begin, _ in sends) - start >= unscored, now
        scored_from, scored_to = pipeline.scoring_window(now)
        assert scored_from == start, now
        assert max(end for _, end in sends) <= scored_to, now


@pytest.fixture(scope="module")
def cycle_paywall_counts() -> dict[datetime, int]:
    """The promo event's rows an hour, for every history a cycle's builds replay."""
    hour = _CYCLE_START - timedelta(days=noise.DEMO_HISTORY_DAYS + 1)
    counts: dict[datetime, int] = {}
    while hour < _CYCLE_START + _CYCLE:
        counts[hour] = sum(
            n for (name, _), n in rows.hour_counts(hour).items() if name == calendar.PROMO_EVENT
        )
        hour += HOUR
    return counts


@pytest.mark.usefixtures("_full_history")
def test_a_demo_built_at_any_hour_flags_three_weekly_promo_sends(
    cycle_paywall_counts: dict[datetime, int],
) -> None:
    """The pipeline's verdicts, built every 17 hours across a cycle (each hour
    of the day comes round once): the scoring pass flags the first hour of a send in
    three distinct weeks, so the verdicts make one Tuesday 10:00 run.

    Pinned because both ways it broke were the calendar's: a send in the
    series' first hours (the history started on the hour), and an incident on
    the promo event that bent the trend a week later until the trend-shift row
    swallowed the next send's spike.
    """
    promo_hour = calendar.PROMO_START // HOUR
    for now in _builds(timedelta(hours=17)):
        start = noise.history_start(now)
        scored_from, scored_to = pipeline.scoring_window(now)
        points = [
            SeriesPoint(bucket=bucket, count=count)
            for bucket, count in cycle_paywall_counts.items()
            if start <= bucket < scored_to
        ]
        result = detect_anomalies(
            points,
            interval=HOUR,
            evaluation_start=scored_from,
            evaluation_end=scored_to,
            settings=noise.DEMO_ANOMALY_SETTINGS,
            settling_buckets=0,
        )
        spikes = sorted(a.bucket for a in result.anomalies if a.direction == "spike")
        firsts = [
            next(bucket for bucket in spikes if begin <= bucket < end)
            for begin, end in calendar.promo_windows(start, now)
            if any(begin <= bucket < end for bucket in spikes)
        ]
        weeks = {
            first.isocalendar()[:2]
            for first in firsts
            if (first.weekday(), first.hour) == (calendar.PROMO_WEEKDAY, promo_hour)
        }
        assert len(weeks) >= 3, (now, firsts)


def test_no_incident_lands_on_the_promo_event() -> None:
    for _kind, _scope, targets, *_rest in calendar._INCIDENT_KINDS:
        assert calendar.PROMO_EVENT not in targets


@pytest.fixture(scope="module")
def cycle_version_counts() -> dict[datetime, dict[tuple[str, str, str], int]]:
    """Rows an hour per ``(event, event type, app version)``, across the cycle's histories."""
    t0 = _CYCLE_START - timedelta(days=noise.DEMO_HISTORY_DAYS + 1)
    out: dict[datetime, dict[tuple[str, str, str], int]] = {}
    for hour, hour_rows in rows.hours_between("events", t0, _CYCLE_START + _CYCLE):
        out[hour] = dict(
            collections.Counter(
                (str(row["event_name"]), str(row["event_type"]), str(row["app_version"]))
                for row in hour_rows
            )
        )
    return out


@pytest.mark.usefixtures("_full_history")
def test_a_demo_built_at_any_hour_marks_only_releases_it_saw_ship(
    cycle_version_counts: dict[datetime, dict[tuple[str, str, str], int]],
) -> None:
    """The release markers each scoring pass draws, every 3 hours of a cycle.

    A marker goes on a version absent from the slice's first hours that then
    activates, so a version must never come back once it has emptied: the
    first build's two days and the history fill's whole history both start
    wherever a build puts them, at night as well.
    """
    for now in _builds(timedelta(hours=3)):
        latest = now.replace(minute=0, second=0, microsecond=0) - HOUR
        for start in (pipeline.first_build_start(now), noise.history_start(now)):
            by_version: dict[str, dict[datetime, int]] = {}
            all_traffic: dict[datetime, int] = {}
            for hour, counts in cycle_version_counts.items():
                if not start <= hour <= latest:
                    continue
                for (_name, _type, version), n in counts.items():
                    all_traffic[hour] = all_traffic.get(hour, 0) + n
                    by_version.setdefault(version, {})[hour] = (
                        by_version.get(version, {}).get(hour, 0) + n
                    )
            marked = releases_to_annotate(by_version, all_traffic)
            shipped = {version for version, _at in calendar.releases_between(start, now)}
            assert set(marked) <= shipped, (now, start, marked)


@pytest.mark.usefixtures("_full_history")
def test_the_by_version_tab_shows_a_release_regression_at_every_hour_of_a_cycle(
    cycle_version_counts: dict[datetime, dict[tuple[str, str, str], int]],
) -> None:
    """What the scheduler's collection stores, judged every 3 hours of a cycle.

    For about a day after each release the new version has carried too little
    to judge (its regression needs ~30 expected rows); the analyzer then keeps
    the previous release's regression, so the tab is never empty, and it holds
    the regression the rotation put in the newest release or the one before.
    A rotation event is only ever found broken the way the rotation breaks it.
    """
    broken = {(event, kind) for event, kind, _kept in calendar.REGRESSION_ROTATION}
    for now in _builds(timedelta(hours=3)):
        latest = now.replace(minute=0, second=0, microsecond=0) - HOUR
        release_total: dict[str, dict[datetime, int]] = {}
        all_traffic: dict[datetime, int] = {}
        by_event: dict[str, dict[str, dict[datetime, int]]] = {}
        by_type: dict[str, dict[str, dict[datetime, int]]] = {}
        for hour, counts in cycle_version_counts.items():
            if not noise.history_start(now) <= hour <= latest:
                continue
            for (name, event_type, version), n in counts.items():
                all_traffic[hour] = all_traffic.get(hour, 0) + n
                release_total.setdefault(version, {})[hour] = (
                    release_total.get(version, {}).get(hour, 0) + n
                )
                by_event.setdefault(name, {}).setdefault(version, {})[hour] = n
                type_series = by_type.setdefault(event_type, {}).setdefault(version, {})
                type_series[hour] = type_series.get(hour, 0) + n
        report = detect_release_regressions_by_scope(
            release_total_by_bucket=release_total,
            all_traffic_by_bucket=all_traffic,
            scope_counts_by_scope_type={"event": by_event, "event_type": by_type},
            latest_bucket=latest,
            settings=RegressionSettings(sigma=noise.DEMO_ANOMALY_SETTINGS.sigma_threshold),
        )["event"]
        assert report.results, (now, report)
        story = calendar.regression_for(calendar.release_index(now))
        story_before = calendar.regression_for(story.release_index - 1)
        shown = {(r.scope_ref, r.kind, r.version) for r in report.results}
        assert shown & {(s.event, s.kind, s.version) for s in (story, story_before)}, (now, shown)
        assert {(r.scope_ref, r.kind) for r in report.results if r.scope_ref in dict(broken)} <= (
            broken
        )


# -- determinism ---------------------------------------------------------------------


def test_rows_depend_on_the_hour_alone() -> None:
    a, b = END - timedelta(days=3), END - timedelta(days=2)
    rows.clear_cache()
    first = (rows.events_for_hour(a), rows.events_for_hour(b))
    rows.clear_cache()
    second = (rows.events_for_hour(b), rows.events_for_hour(a))
    assert first == (second[1], second[0])
    # Orders come from a subset of the hour's generation and agree with the whole.
    index = rows.epoch_hour(a)
    assert rows.orders_for_hour(a) == tuple(rows._orders(index, rows._generate_events(index)))


def test_row_counts_are_the_hour_counts_outside_regressions() -> None:
    hour = END - timedelta(days=1)
    regressed = {r.event for r in calendar.regressions_by_version(hour).values()}
    counted = collections.Counter(
        (str(row["event_name"]), str(row["platform"])) for row in rows.events_for_hour(hour)
    )
    for key, expected in rows.hour_counts(hour).items():
        if key[0] in regressed:
            assert counted[key] <= expected
        else:
            assert counted[key] == expected, key


# -- the in-memory engine -------------------------------------------------------------


@pytest.fixture
def engine() -> DemoMemoryAdapter:
    return DemoMemoryAdapter(now=NOW)


def test_the_engine_counts_what_the_rows_hold(engine: DemoMemoryAdapter) -> None:
    t0, t1 = END - timedelta(hours=6), END
    names, value_names, out = engine.get_time_bucketed_counts(
        schema.EVENTS_SCAN_SQL,
        "event_time",
        "1h",
        ["event_name", "platform"],
        ["properties"],
        {"properties": ["source"]},
        t0,
        t1,
    )
    assert names == ["event_name", "platform", "properties"]
    assert value_names == ["properties.source"]
    got: collections.Counter[tuple[object, ...]] = collections.Counter()
    for bucket, event_name, platform, keys, source, count in out:
        assert isinstance(keys, list) and keys == sorted(keys)
        assert isinstance(count, int)
        got[(bucket, event_name, platform)] += count
        assert source is None or str(source).startswith('"')
    expected: collections.Counter[tuple[object, ...]] = collections.Counter()
    for row in rows.rows_between("events", t0, t1):
        moment = row["event_time"]
        assert isinstance(moment, datetime)
        bucket = moment.replace(minute=0, second=0, microsecond=0)
        expected[(bucket, row["event_name"], row["platform"])] += 1
    assert got == expected


def test_property_breakdowns_read_the_unquoted_value(engine: DemoMemoryAdapter) -> None:
    t0, t1 = END - timedelta(hours=3), END
    _, _, out = engine.get_time_bucketed_breakdown_counts(
        schema.EVENTS_SCAN_SQL,
        "event_time",
        "1h",
        "properties.source",
        ["event_type"],
        ["properties"],
        None,
        t0,
        t1,
    )
    values = {row[1] for row in out if row[3] == "search"}
    assert {"typed", "voice"} <= values
    assert all(not str(value).startswith('"') for value in values)


def test_the_engine_describes_the_postgres_tables(engine: DemoMemoryAdapter) -> None:
    columns = {c.name: c.type_name for c in engine.get_columns(schema.EVENTS_SCAN_SQL)}
    assert columns["event_time"] == "timestamptz"
    assert columns["amount"] == "numeric"
    assert columns["properties"] == "jsonb"
    assert "user_id" not in columns  # the scan query never groups by it
    assert [t.name for t in engine.get_schema_tables()] == ["events", "orders"]


def test_the_engine_answers_the_active_sessions_metric(engine: DemoMemoryAdapter) -> None:
    day = END - timedelta(days=2)
    names, out = engine.get_preview_rows(
        schema.ACTIVE_SESSIONS_SQL, 10, time_column="ts", time_from=day, time_to=day + HOUR
    )
    sessions = {row["session_id"] for row in rows.rows_between("events", day, day + timedelta(1))}
    assert names == ["ts", "value"]
    assert out == [(day, len(sessions))]
    # Sessions hold several events each, like a real app's.
    assert 2_000 <= len(sessions) <= 10_000


def test_the_engine_is_bounded(engine: DemoMemoryAdapter) -> None:
    with pytest.raises(SyntheticCapabilityError):
        engine.get_time_bucketed_counts(
            schema.EVENTS_SCAN_SQL,
            "event_time",
            "1h",
            ["event_name"],
            [],
            None,
            END - MAX_EVENTS_WINDOW - timedelta(days=1),
            END,
        )
    _, preview = engine.get_preview_rows(schema.EVENTS_SCAN_SQL, 100_000)
    times = [row[0] for row in preview if isinstance(row[0], datetime)]
    assert len(times) == len(preview)
    assert max(times) < rows.available_through(NOW)
    assert min(times) >= rows.available_through(NOW) - timedelta(days=1)


def test_the_engine_ranks_a_long_window_as_a_stream(engine: DemoMemoryAdapter) -> None:
    long_start = END - timedelta(days=12)
    with engine.top_n_ranking_window(long_start, END):
        _, _, out = engine.get_time_bucketed_breakdown_counts(
            schema.EVENTS_SCAN_SQL,
            "event_time",
            "1h",
            "country",
            ["event_type"],
            [],
            None,
            END - HOUR,
            END,
            values_limit=3,
        )
    assert {row[1] for row in out} <= {"US", "IN", "Other", "GB", "BR"}
    assert any(row[2] == 1 for row in out)


# -- the data source type -------------------------------------------------------------


def test_demo_sources_are_wired_as_a_postgres_dialect_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tripl.config import settings

    assert DBType.demo in DEMO_DB_TYPES
    assert registry.adapter_class("demo") is DemoPostgresAdapter
    assert dialect_for_db_type("demo") is SqlDialect.postgres
    assert DBType.demo.value in CONNECTION_SETTINGS_MODELS
    monkeypatch.setattr(settings, "demo_warehouse_mode", "memory")
    source = SimpleNamespace(db_type="demo", password_encrypted="", timeout_seconds=None)
    adapter = registry.build_adapter(source)  # type: ignore[arg-type]
    assert isinstance(adapter, DemoMemoryAdapter)
    assert adapter.test_connection()


def test_six_consecutive_releases_cover_both_regression_kinds() -> None:
    latest = calendar.release_index(END)
    kinds = {calendar.regression_for(latest - back).kind for back in range(6)}
    assert kinds == {"missing", "volume_drop"}


def test_every_three_hours_hold_both_purchase_contract_findings() -> None:
    """A free purchase and a GBP purchase in ANY three consecutive hours of a week.

    The demo's first live collection can be as short as three hours, and it is
    the only collection that checks field contracts: whatever hour a demo is
    created, that window must show both of the story's contract findings.
    """
    start = datetime(2026, 10, 5, tzinfo=UTC)  # a Monday 00:00
    free_hours: list[bool] = []
    gbp_hours: list[bool] = []
    for offset in range(168 + 2):
        hour = start + timedelta(hours=offset)
        purchases = [
            row
            for row in rows.events_for_hour(hour)
            if row["event_name"] == roster.SCHEMA_DRIFT_EVENT
        ]
        free_hours.append(any(row["amount"] == 0 for row in purchases))
        gbp_hours.append(any(row["currency"] == "GBP" for row in purchases))
    for first in range(168):
        assert any(free_hours[first : first + 3]), start + timedelta(hours=first)
        assert any(gbp_hours[first : first + 3]), start + timedelta(hours=first)


def test_every_hour_holds_the_undocumented_product_on_trials() -> None:
    """``prod_weekly`` on ``Trial Started`` in EVERY hour of a week.

    The catalog scan reads ``product_id`` over its three-hour lookback alone,
    so the value drift the demo opens with is only as certain as this. Every
    hour, not every three: a window of whole hours then holds it however short
    it is. The week includes 2026-10-19 11:00-15:00, whose trials drew no
    ``prod_weekly`` at all before the hour's story row.
    """
    start = datetime(2026, 10, 15, tzinfo=UTC)
    for offset in range(168):
        hour = start + timedelta(hours=offset)
        products = [
            row["product_id"]
            for row in rows.events_for_hour(hour)
            if row["event_name"] == roster.VARIABLE_DRIFT_EVENT
        ]
        assert roster.UNDOCUMENTED_PRODUCT in products, hour


def test_contract_findings_reach_the_orders_table() -> None:
    hour = datetime(2026, 10, 6, 14, tzinfo=UTC)
    purchases = [
        row for row in rows.events_for_hour(hour) if row["event_name"] == roster.SCHEMA_DRIFT_EVENT
    ]
    orders = rows.orders_for_hour(hour)
    for purchase in purchases:
        match = [
            order
            for order in orders
            if order["created_at"] == purchase["event_time"]
            and order["user_id"] == purchase["user_id"]
        ]
        assert match, purchase["event_time"]
        assert match[0]["currency"] == purchase["currency"]
        assert match[0]["amount"] == purchase["amount"]
