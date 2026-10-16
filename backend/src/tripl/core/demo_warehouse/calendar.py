"""The demo warehouse's clock: incidents, releases and promos on GLOBAL UTC time.

Every demo project reads the same warehouse, so nothing here depends on when a
demo was created. Instead the calendar is dense enough that a demo opened at any
instant finds something to look at:

* one detectable **incident every day**, starting 13:00 UTC and lasting 2-3 hours,
  rotating through five kinds (:data:`INCIDENT_KINDS`) and several targets, so
  the last 24 hours always hold an anomalous bucket (``test_demo_warehouse_model``
  checks this for every hour of a long span);
* a **weekly promo**: every Tuesday 10:00-12:00 UTC the paywall gets a burst of
  traffic from the "promo Tuesday" email. A team plans it, so it is the demo's
  example of an expected spike;
* a **release train**: a new app version every 14 days, adopted along a logistic
  curve over ~5 days after a 5-day beta. Each release ships one regression (an
  event that goes missing, or drops by more than half, in that version), and
  every other release sends one JSON property with the wrong type on iOS;
* **unplanned events**: names no plan documents, fired in short bursts.

Pure functions of time. No I/O and no wall clock: callers pass instants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: Day 0 of the calendar: a Monday. Releases are numbered from here.
EPOCH = datetime(2024, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

# -- releases ----------------------------------------------------------------

RELEASE_INTERVAL = timedelta(days=14)
#: Releases reach the stores at 10:00 UTC on their day.
RELEASE_OFFSET = timedelta(hours=10)
#: Logistic adoption: half the users have updated 60 hours after release.
ROLLOUT_MIDPOINT_HOURS = 60.0
ROLLOUT_SCALE_HOURS = 14.0
#: Users who never update promptly; they stay one version behind the previous.
LAGGARD_SHARE = 0.05
#: Testers run the next version for this long before release, at this share.
BETA_PERIOD = timedelta(days=5)
BETA_SHARE = 0.004

#: Events a release breaks, in rotation: (event name, kind, share kept).
#: ``missing`` keeps nothing in the new version; ``volume_drop`` keeps 40 %.
REGRESSION_ROTATION: tuple[tuple[str, str, float], ...] = (
    ("Share Button Click", "missing", 0.0),
    ("Filter Applied Click", "volume_drop", 0.4),
    ("Video Play Click", "missing", 0.0),
    ("Save Recipe Click", "volume_drop", 0.4),
    ("Plan Toggle Click", "missing", 0.0),
    ("Add to Shopping List Click", "volume_drop", 0.4),
)
#: The JSON property a broken release sends as a string instead of a number,
#: on iOS only: ``properties.results_count`` of the search events.
PROPERTY_TYPE_CHANGE_KEY = "results_count"
PROPERTY_TYPE_CHANGE_PLATFORM = "ios"


def release_index(at: datetime) -> int:
    """The newest release published at or before ``at``."""
    return math.floor((at - EPOCH - RELEASE_OFFSET) / RELEASE_INTERVAL)


def release_start(index: int) -> datetime:
    return EPOCH + RELEASE_OFFSET + index * RELEASE_INTERVAL


def version_label(index: int) -> str:
    """``major.minor.0``: 26 minors a year under one major, semver-ordered."""
    return f"{4 + index // 26}.{index % 26}.0"


def _adoption(index: int, at: datetime) -> float:
    """Share of users on release ``index`` or newer (laggards excluded)."""
    age_hours = (at - release_start(index)) / HOUR
    if age_hours < 0:
        return 0.0
    logistic = 1.0 / (1.0 + math.exp(-(age_hours - ROLLOUT_MIDPOINT_HOURS) / ROLLOUT_SCALE_HOURS))
    return (1.0 - LAGGARD_SHARE) * logistic


def version_shares(at: datetime) -> tuple[tuple[str, float], ...]:
    """The app versions in use at ``at`` and their shares (summing to 1).

    The newest release, the previous one, the laggards' version two behind,
    and, during the beta period, the next release at a tester's share.
    """
    newest = release_index(at)
    adopted_newest = _adoption(newest, at)
    adopted_previous = _adoption(newest - 1, at)
    shares: list[tuple[str, float]] = [
        (version_label(newest), adopted_newest),
        (version_label(newest - 1), adopted_previous - adopted_newest),
        (version_label(newest - 2), 1.0 - adopted_previous),
    ]
    if release_start(newest + 1) - at <= BETA_PERIOD:
        # Testers come out of the newest version's users.
        label, share = shares[0]
        taken = min(BETA_SHARE, share)
        shares[0] = (label, share - taken)
        shares.insert(0, (version_label(newest + 1), taken))
    return tuple((label, share) for label, share in shares if share > 1e-9)


def laggard_version(at: datetime) -> str:
    """The version only laggards run: where retired events still fire."""
    return version_label(release_index(at) - 2)


@dataclass(frozen=True)
class Regression:
    """What release ``version`` broke: ``event`` keeps ``kept_share`` of its rows."""

    version: str
    release_index: int
    event: str
    kind: str  # "missing" | "volume_drop"
    kept_share: float


def regression_for(index: int) -> Regression:
    event, kind, kept = REGRESSION_ROTATION[index % len(REGRESSION_ROTATION)]
    return Regression(version_label(index), index, event, kind, kept)


def regressions_by_version(at: datetime) -> dict[str, Regression]:
    """Regressions of every version that can be in use at ``at``, by label."""
    newest = release_index(at)
    return {regression_for(i).version: regression_for(i) for i in range(newest - 2, newest + 2)}


def property_type_broken(version: str) -> bool:
    """Whether ``version`` sends ``results_count`` as a string on iOS."""
    major, minor, _ = version.split(".")
    index = (int(major) - 4) * 26 + int(minor)
    return index % 2 == 0


def releases_between(t0: datetime, t1: datetime) -> list[tuple[str, datetime]]:
    """Releases published in ``[t0, t1)``, as ``(version, published_at)``."""
    out: list[tuple[str, datetime]] = []
    index = release_index(t0)
    while release_start(index) < t1:
        if release_start(index) >= t0:
            out.append((version_label(index), release_start(index)))
        index += 1
    return out


# -- daily incidents -----------------------------------------------------------

INCIDENT_START = timedelta(hours=13)

#: The rotation, one per day: (kind, scope, targets, duration hours, multiplier,
#: platform the change concentrates on).
#:
#: Magnitudes are sized for the detector as much as for the story. Its phase
#: baseline re-levels each expectation to the mean of the trailing day, so an
#: incident's extra (or missing) rows lift (or sink) the expectation of every
#: hour of the next day by ``(multiplier - 1) x hours / 24``. On a high-volume
#: event that reads as a day of false drops after a spike. Each incident is
#: therefore strong for its hours (z well above 5) yet small over a day: the
#: big spikes go to mid-volume events, and the home screen's web campaign
#: stays near +8 % of the day.
_INCIDENT_KINDS: tuple[tuple[str, str, tuple[str, ...], int, float, str | None], ...] = (
    # A traffic burst (a feature on the store front page), mostly on iOS.
    ("spike", "event", ("Search Performed", "Meal Planner View", "Recipe Saved"), 2, 3.0, "ios"),
    # A broken screen: the event falls to 15 % for three hours.
    (
        "drop",
        "event",
        ("Recipe Detail View", "Search Results View", "Paywall View"),
        3,
        0.15,
        None,
    ),
    # A backend outage: errors multiply.
    ("error_burst", "event", ("API Error",), 2, 5.0, None),
    # A degraded backend: a whole event type falls to 35 %.
    ("type_dip", "event_type", ("search", "meal_plan", "content"), 3, 0.35, None),
    # A web campaign: the home screen gets four times its web traffic.
    ("platform_shift", "event", ("Home Screen View",), 3, 4.0, "web"),
)
INCIDENT_KINDS: tuple[str, ...] = tuple(kind[0] for kind in _INCIDENT_KINDS)
#: The targets each kind takes in turn.
INCIDENT_TARGETS: dict[str, tuple[str, ...]] = {kind[0]: kind[2] for kind in _INCIDENT_KINDS}
#: The calendar's period: after this many days every (kind, target) pair has
#: come round and the sequence repeats.
INCIDENT_ROTATION_DAYS = len(_INCIDENT_KINDS) * math.lcm(
    *(len(kind[2]) for kind in _INCIDENT_KINDS)
)
#: Share of a spike's extra traffic that lands on its platform.
SPIKE_PLATFORM_SHARE = 0.85


@dataclass(frozen=True)
class Incident:
    """One calendar incident: ``target`` (an event, or an event type when
    ``scope`` is ``event_type``) is multiplied by ``multiplier`` in
    ``[start, end)``; ``platform`` names where the change concentrates."""

    kind: str
    scope: str
    target: str
    start: datetime
    end: datetime
    multiplier: float
    platform: str | None
    description: str


def _describe(kind: str, target: str, multiplier: float, platform: str | None) -> str:
    if kind == "spike":
        return f"{target} spikes to x{multiplier:g}, mostly on {platform}"
    if kind == "drop":
        return f"{target} drops to {round(multiplier * 100)}% (a broken screen)"
    if kind == "error_burst":
        return f"{target} bursts to x{multiplier:g} (a backend outage)"
    if kind == "type_dip":
        return f"every {target} event dips to {round(multiplier * 100)}% (a degraded backend)"
    return f"{target} gets x{multiplier:g} its {platform} traffic (a web campaign)"


def incident_on(day: int) -> Incident:
    """The incident of calendar day ``day`` (days since :data:`EPOCH`)."""
    kind, scope, targets, hours, multiplier, platform = _INCIDENT_KINDS[day % len(_INCIDENT_KINDS)]
    target = targets[(day // len(_INCIDENT_KINDS)) % len(targets)]
    start = EPOCH + day * DAY + INCIDENT_START
    return Incident(
        kind=kind,
        scope=scope,
        target=target,
        start=start,
        end=start + hours * HOUR,
        multiplier=multiplier,
        platform=platform,
        description=_describe(kind, target, multiplier, platform),
    )


def _day(at: datetime) -> int:
    return math.floor((at - EPOCH) / DAY)


def incidents_between(t0: datetime, t1: datetime) -> list[Incident]:
    """Incidents overlapping ``[t0, t1)``, oldest first."""
    out: list[Incident] = []
    for day in range(_day(t0) - 1, _day(t1) + 1):
        incident = incident_on(day)
        if incident.start < t1 and incident.end > t0:
            out.append(incident)
    return out


def latest_incident(at: datetime) -> Incident:
    """The most recent incident that has started by ``at``."""
    day = _day(at)
    incident = incident_on(day)
    return incident if incident.start <= at else incident_on(day - 1)


# -- weekly promo ----------------------------------------------------------------

PROMO_EVENT = "Paywall View"
PROMO_WEEKDAY = 1  # Tuesday
PROMO_START = timedelta(hours=10)
PROMO_HOURS = 2
PROMO_MULTIPLIER = 2.5
PROMO_TITLE = "Weekly promo email"


def promo_windows(t0: datetime, t1: datetime) -> list[tuple[datetime, datetime]]:
    """Weekly promo windows overlapping ``[t0, t1)``."""
    out: list[tuple[datetime, datetime]] = []
    for day in range(_day(t0) - 1, _day(t1) + 1):
        start = EPOCH + day * DAY
        if start.weekday() != PROMO_WEEKDAY:
            continue
        begin = start + PROMO_START
        end = begin + PROMO_HOURS * HOUR
        if begin < t1 and end > t0:
            out.append((begin, end))
    return out


# -- mix shifts (distribution drift) ---------------------------------------------


@dataclass(frozen=True)
class MixShift:
    """A recurring change in the MIX of one column, holding volume steady.

    For ``days`` days of every ``cycle_days``-day cycle (from ``offset_days``),
    the rows of ``event_types`` take ``shifted`` as their ``column`` mix, after a
    ``ramp_hours`` ramp in and out. The step is abrupt next to any baseline a
    drift check compares against (hours or days), so PSI flags it at the start
    and again at the end of every plateau.
    """

    name: str
    column: str
    event_types: tuple[str, ...]
    shifted: tuple[tuple[str, float], ...]
    cycle_days: int
    offset_days: int
    days: int
    ramp_hours: int
    description: str


MIX_SHIFTS: tuple[MixShift, ...] = (
    # A web redesign test sends more of the screen views to the web app.
    MixShift(
        name="web_redesign_test",
        column="platform",
        event_types=("screen_view",),
        shifted=(("ios", 0.25), ("android", 0.25), ("web", 0.50)),
        cycle_days=14,
        offset_days=6,
        days=4,
        ramp_hours=3,
        description="the web redesign test moves screen views to the web app",
    ),
    # A TV feature in Brazil: sign-ups and app opens skew to BR for three days.
    MixShift(
        name="brazil_tv_feature",
        column="country",
        event_types=("account", "app_lifecycle"),
        shifted=(
            ("US", 0.22),
            ("GB", 0.08),
            ("DE", 0.06),
            ("FR", 0.05),
            ("BR", 0.42),
            ("IN", 0.08),
            ("JP", 0.03),
            ("CA", 0.06),
        ),
        cycle_days=14,
        offset_days=1,
        days=3,
        ramp_hours=2,
        description="a TV feature in Brazil skews sign-ups and app opens to BR",
    ),
)


def mix_shift_weight(shift: MixShift, at: datetime) -> float:
    """How far ``shift`` has moved the mix at ``at``: 0 (base) to 1 (shifted)."""
    hours_into_cycle = ((at - EPOCH) / HOUR) % (shift.cycle_days * 24)
    start = shift.offset_days * 24
    end = start + shift.days * 24
    if hours_into_cycle < start or hours_into_cycle >= end:
        return 0.0
    ramp = max(shift.ramp_hours, 1)
    return min(1.0, (hours_into_cycle - start) / ramp, (end - hours_into_cycle) / ramp)


def mix_shift_windows(t0: datetime, t1: datetime) -> list[tuple[MixShift, datetime, datetime]]:
    """Mix-shift plateaus overlapping ``[t0, t1)``: ``(shift, start, end)``."""
    out: list[tuple[MixShift, datetime, datetime]] = []
    for shift in MIX_SHIFTS:
        cycle = timedelta(days=shift.cycle_days)
        first = math.floor((t0 - EPOCH) / cycle) - 1
        last = math.floor((t1 - EPOCH) / cycle) + 1
        for index in range(first, last + 1):
            begin = EPOCH + index * cycle + timedelta(days=shift.offset_days)
            end = begin + timedelta(days=shift.days)
            if begin < t1 and end > t0:
                out.append((shift, begin, end))
    out.sort(key=lambda item: item[1])
    return out


def blend_mix(
    base: tuple[tuple[str, float], ...], shifted: tuple[tuple[str, float], ...], weight: float
) -> tuple[tuple[str, float], ...]:
    """``base`` moved ``weight`` of the way to ``shifted`` (values of both, renormalized)."""
    if weight <= 0:
        return base
    base_map = dict(base)
    shifted_map = dict(shifted)
    keys = list(dict.fromkeys([*base_map, *shifted_map]))
    mixed = [
        (key, (1 - weight) * base_map.get(key, 0.0) + weight * shifted_map.get(key, 0.0))
        for key in keys
    ]
    total = sum(share for _, share in mixed) or 1.0
    return tuple((key, share / total) for key, share in mixed)


# -- unplanned bursts ------------------------------------------------------------

#: (event name, every N days, offset day, start hour, hours)
_UNPLANNED_SCHEDULE: tuple[tuple[str, int, int, int, int], ...] = (
    ("app_heartbeat_v1", 3, 1, 6, 2),
    ("legacy_deeplink_open", 7, 4, 20, 1),
)


def unplanned_bursts(t0: datetime, t1: datetime) -> list[tuple[str, datetime, datetime]]:
    """Unplanned-event bursts overlapping ``[t0, t1)``: ``(name, start, end)``."""
    out: list[tuple[str, datetime, datetime]] = []
    for day in range(_day(t0) - 1, _day(t1) + 1):
        for name, every, offset, hour, hours in _UNPLANNED_SCHEDULE:
            if day % every != offset:
                continue
            begin = EPOCH + day * DAY + timedelta(hours=hour)
            end = begin + hours * HOUR
            if begin < t1 and end > t0:
                out.append((name, begin, end))
    return out
