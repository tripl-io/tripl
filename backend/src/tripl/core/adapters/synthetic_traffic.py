"""The demo's synthetic traffic: how much of it, on which app version, on which platform.

Pure functions only — no database, no I/O, no wall clock. A generated demo writes
its traffic in three places, and they agree only because all three ask this
module instead of shaping their own:

* the seeder's backfilled history (``services.demo.builders.warehouse``);
* the runtime tick that appends an hour at a time (``worker.tasks.demo_runtime``);
* the synthetic warehouse a scheduled collection reads back, rewriting the newest
  hours from it (``core.adapters.synthetic``).

Everything is a function of an ABSOLUTE bucket and the demo's seed clock
(:attr:`DemoTraffic.anchor`), never of an offset from a caller's own "now", so an
hour reads the same whichever writer produced it and whenever it ran. Before
this, the seeded history carried no version split at all and a platform split
for one event only, the tick split that one event by its own clock, and the
warehouse drew every row's version and platform uniformly — so a demo's version
charts showed three versions at a third each, in the newest hours only.

Volume
------
:func:`hourly_volume` is the demo's hourly shape (it lived in
``services.demo.noise``, which re-exports it). The synthetic warehouse used to
approximate it; with the seed clock and seed stored on the demo's source it now
serves the very counts the seeder and the tick store, so the collection that
rewrites the newest hours rewrites them with what is already there.

Releases
--------
A release train ships ``1.<n>.0`` every :data:`RELEASE_CADENCE`; the demo's
:data:`REFERENCE_RELEASE` began its rollout :data:`REFERENCE_RELEASE_AGE` before
the seed clock. Internal testers run each build :data:`BETA_LEAD` ahead of its
rollout at :data:`BETA_SHARE` of traffic — far under the activation gate, so a
beta never reads as a release. Adoption is then an S-curve over days with a long
tail: half of the users take the update within days (a logistic), the rest
trickle in over weeks (an exponential). Users only move forward, so with
``adopted(n)`` the share on ``n`` or newer, ``share(n) = adopted(n) -
adopted(n + 1)``: the newest version grows while the one before it fades over the
same days and leaves a tail. ``1.0.0`` is the oldest build in the wild and keeps
whatever the newer ones leave.

Platforms
---------
Every event keeps a steady mix of its own — the demo's base mix tilted per event,
with a small hour-of-week texture — that repeats on the same hour of every week,
like the volume texture. The one deliberate movement is the demo's
distribution-drift story: ``screen_view`` events move towards web over the
:data:`DRIFT_SPAN_DAYS` before the seed clock (the PSI ladder the monitoring
builder seeds is computed from the same :func:`drift_platform_shares`) and keep
the new mix afterwards.

The detector scores a platform series as a share of the event's total (platform
parity), with a floor of a few percent of that share. A rare event's share moves
in whole-event steps far coarser than that, so the split is built to repeat
exactly on the same hour of the week: the hour's steady volume (the volume
without its slow upward drift) is split by a key that is itself the hour of the
week, and only the few rows the drift adds on top are dealt out by largest
remainder. Splitting each hour afresh flagged the demo's low-volume events on
every collection.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

from tripl.core.bucketing import to_utc

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)

# --- volume ------------------------------------------------------------------------
# Wall-clock length of the seeded hourly history. The seasonal (hour-of-week)
# phase baseline in the anomaly detector needs 3 full weekly cycles = 504 hourly
# buckets BEFORE the evaluation window, or ``detect_anomalies`` silently degrades
# to the seasonality-blind rolling fallback. 23 days = 552 buckets gives 504 of
# history plus a 48h evaluation window, and the newest bucket sits one hour before
# ``now`` so the signal stays inside the Wave-1 freshness horizon
# (``LATEST_SCAN_STALE_INTERVALS`` = 3 intervals).
DEMO_HISTORY_DAYS = 23
# Width of the index grid :func:`hourly_volume`'s slow drift is measured on.
HISTORY_BUCKETS = DEMO_HISTORY_DAYS * 24

# --- releases ----------------------------------------------------------------------
# A release every two weeks; release ``n`` is labelled ``1.<n>.0``.
RELEASE_CADENCE = timedelta(days=14)
# The release the demo's story is about, ``1.4.0``: its rollout began six days
# before the seed clock.
REFERENCE_RELEASE = 4
REFERENCE_RELEASE_AGE = timedelta(days=6)
# Internal testers run each build this long before its rollout, at this share of
# traffic. Long enough that the release before the reference one (rollout 20 days
# back) already has traffic in the oldest seeded hour: the worker marks a release
# only when its version is absent from the leading buckets of what it loads, so
# the seeded window holds exactly one release to mark.
BETA_LEAD = timedelta(days=5)
BETA_SHARE = 0.004
# Adoption after rollout: ``_FAST_UPDATERS`` of the users update on a logistic
# centred ``_FAST_MIDPOINT_DAYS`` in and ``_FAST_WIDTH_DAYS`` wide; the rest follow
# an exponential with a ``_SLOW_TAU_DAYS`` time constant — the long tail.
_FAST_UPDATERS = 0.5
_FAST_MIDPOINT_DAYS = 3.0
_FAST_WIDTH_DAYS = 1.0
_SLOW_TAU_DAYS = 10.0

# --- platforms ---------------------------------------------------------------------
PLATFORMS: tuple[str, ...] = ("ios", "android", "web")
# The distribution-drift story: this event type's mix moves towards web over the
# days before the seed clock.
DRIFTING_EVENT_TYPE = "screen_view"
DRIFT_SPAN_DAYS = 8
# Per-event tilt of the base mix, a log-scale factor in ``[-x, +x)`` per platform.
_EVENT_TILT = 0.12
# Hour-of-week texture per event and platform, a log-scale factor in ``[-x, +x)``.
_TEXTURE = 0.03


def derive_seed(seed: int, key: str) -> int:
    """Derive a stable 32-bit noise seed from the scenario seed and a semantic key.

    Uses SHA-256 (not builtin ``hash``) so the value is reproducible across
    processes regardless of PYTHONHASHSEED. ``key`` is a stable semantic label
    (e.g. an event name), never a random uuid, so the same event always maps to
    the same noise across reseeds.
    """
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def hourly_volume(
    base: int, bucket: datetime, idx: int, noise_seed: int, total_buckets: int
) -> int:
    """Deterministic hourly volume: daily + gentle weekly shape, a phase-consistent
    texture, and a slow upward drift.

    The texture depends only on ``(noise_seed, weekday, hour)`` — never on the week
    — so the same hour-of-week repeats identically across cycles. That keeps the
    detector's seasonal phase baseline tight (near-zero robust scale), so the
    injected spike is the only deviation that clears the sigma gate and the demo
    yields a small, reproducible set of anomalies instead of noise-driven false
    positives. The drift stays well under the detector's 15% trend-shift gate.
    """
    hour = bucket.hour
    weekday = bucket.weekday()
    daily = math.sin((hour - 2) * math.pi / 12)
    weekly = 0.08 * math.sin(weekday * math.pi / 3.5)
    texture = ((noise_seed * 31 + weekday * 7 + hour * 13) % 15 - 7) / 100.0
    drift = 0.04 * (idx / max(total_buckets - 1, 1))
    raw = base * (1 + 0.35 * daily + weekly + texture + drift)
    return max(1, round(raw))


def version_label(release: int) -> str:
    """The app version string of release ``release``: ``1.<n>.0``."""
    return f"1.{release}.0"


def _logistic(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


# The logistic's value at the rollout instant, taken off so adoption starts from
# the beta share instead of jumping at rollout.
_FAST_AT_ROLLOUT = _logistic(-_FAST_MIDPOINT_DAYS / _FAST_WIDTH_DAYS)


def adoption(days_since_rollout: float) -> float:
    """Share of users on a release or a newer one, ``days`` after its rollout began.

    Zero before the beta, :data:`BETA_SHARE` during it, then an S-curve over days
    that approaches one along a long tail. Never decreases, which is what keeps
    every version's share non-negative in :meth:`DemoTraffic.version_shares`.
    """
    if days_since_rollout < -BETA_LEAD / _DAY:
        return 0.0
    if days_since_rollout < 0:
        return BETA_SHARE
    fast = (
        _logistic((days_since_rollout - _FAST_MIDPOINT_DAYS) / _FAST_WIDTH_DAYS) - _FAST_AT_ROLLOUT
    ) / (1.0 - _FAST_AT_ROLLOUT)
    slow = 1.0 - math.exp(-days_since_rollout / _SLOW_TAU_DAYS)
    return BETA_SHARE + (1.0 - BETA_SHARE) * (_FAST_UPDATERS * fast + (1.0 - _FAST_UPDATERS) * slow)


def drift_progress(days_before_anchor: float) -> float:
    """How far the drift story has moved ``days`` before the seed clock: 0 to 1.

    0 before the drift span, rising linearly across it, 1 from the seed clock on.
    """
    return min(max((DRIFT_SPAN_DAYS - days_before_anchor) / DRIFT_SPAN_DAYS, 0.0), 1.0)


def drift_platform_shares(progress: float) -> dict[str, float]:
    """The base platform mix ``progress`` of the way through the drift story (0 to 1).

    Web rises while iOS falls, so the mix genuinely drifts.
    """
    clamped = min(max(progress, 0.0), 1.0)
    web = 0.12 + 0.24 * clamped
    ios = 0.55 - 0.18 * clamped
    android = max(0.01, 1.0 - web - ios)
    return {"ios": ios, "android": android, "web": web}


def _unit(*parts: object) -> float:
    """A stable value in ``[0, 1)`` for ``parts`` (SHA-256, never builtin ``hash``)."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0**64


@lru_cache(maxsize=4096)
def _event_tilt(seed: int, event_name: str, platform: str) -> float:
    return _EVENT_TILT * (2.0 * _unit(seed, "platform_tilt", event_name, platform) - 1.0)


@lru_cache(maxsize=65536)
def _texture(seed: int, event_name: str, platform: str, weekday: int, hour: int) -> float:
    unit = _unit(seed, "platform_texture", event_name, platform, weekday, hour)
    return _TEXTURE * (2.0 * unit - 1.0)


def _epoch_hour(bucket: datetime) -> int:
    return int(to_utc(bucket).timestamp()) // 3600


def split_count(
    total: int, shares: Mapping[str, float], *, key: Sequence[object]
) -> dict[str, int]:
    """Split ``total`` across ``shares`` into whole numbers that sum to ``total``.

    Systematic sampling: each value gets the floor or the ceiling of its exact
    part, and an offset drawn from ``key`` decides which, so a small share shows
    up in the right fraction of buckets instead of always rounding away.
    Deterministic for a given ``key``; values that get nothing are left out.
    """
    items = [(value, share) for value, share in shares.items() if share > 0]
    weight = math.fsum(share for _value, share in items)
    if total <= 0 or weight <= 0:
        return {}
    offset = _unit("split", *key)
    counts: dict[str, int] = {}
    cumulative = 0.0
    previous = 0
    for position, (value, share) in enumerate(items):
        if position == len(items) - 1:
            # The last boundary is ``total`` exactly, whatever float error the
            # running sum picked up: ``total`` is a stored volume.
            boundary = total
        else:
            cumulative += total * share / weight
            boundary = min(math.floor(cumulative + offset), total)
        if boundary > previous:
            counts[value] = boundary - previous
            previous = boundary
    return counts


def _largest_remainder(total: int, shares: Mapping[str, float]) -> dict[str, int]:
    """``total`` split by largest remainder: a single row goes to the leading share."""
    positive = {value: share for value, share in shares.items() if share > 0}
    weight = math.fsum(positive.values())
    if total <= 0 or weight <= 0:
        return {}
    exact = {value: total * share / weight for value, share in positive.items()}
    counts = {value: math.floor(part) for value, part in exact.items()}
    left = total - sum(counts.values())
    # ``sorted`` is stable, so a tie keeps the order of ``shares``.
    for value in sorted(exact, key=lambda item: counts[item] - exact[item])[:left]:
        counts[value] += 1
    return {value: count for value, count in counts.items() if count > 0}


def _merged(*parts: Mapping[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for part in parts:
        for value, count in part.items():
            merged[value] = merged.get(value, 0) + count
    return merged


@dataclass(frozen=True)
class DemoTraffic:
    """One demo's synthetic traffic: its volume, version and platform mix.

    ``anchor`` is the demo's seed clock — its seeded history ends the hour before
    — and ``seed`` the scenario seed. Both are stored on the demo's synthetic
    source (:func:`traffic_params`), so the warehouse rebuilds the same traffic.
    """

    anchor: datetime
    seed: int

    def __post_init__(self) -> None:
        # Hour-aligned UTC, so every derived instant sits on the bucket grid.
        aligned = to_utc(self.anchor).replace(minute=0, second=0, microsecond=0)
        object.__setattr__(self, "anchor", aligned)

    # -- volume -----------------------------------------------------------------

    @property
    def grid_start(self) -> datetime:
        """The oldest seeded bucket: index 0 of :func:`hourly_volume`'s drift grid."""
        return self.anchor - timedelta(days=DEMO_HISTORY_DAYS)

    def noise_seed(self, event_name: str) -> int:
        return derive_seed(self.seed, event_name) % 997

    def volume(self, base: int, event_name: str, bucket: datetime) -> int:
        """The event's ordinary volume in ``bucket``, before anything injected on top."""
        index = round((to_utc(bucket) - self.grid_start) / _HOUR)
        return hourly_volume(base, bucket, index, self.noise_seed(event_name), HISTORY_BUCKETS)

    def steady_volume(self, base: int, event_name: str, bucket: datetime) -> int:
        """:meth:`volume` without its slow drift: the same on that hour of every week."""
        return hourly_volume(base, bucket, 0, self.noise_seed(event_name), HISTORY_BUCKETS)

    # -- versions ---------------------------------------------------------------

    def rollout_start(self, release: int) -> datetime:
        """When release ``release`` began rolling out to users (its beta is earlier)."""
        offset = (release - REFERENCE_RELEASE) * RELEASE_CADENCE
        return self.anchor - REFERENCE_RELEASE_AGE + offset

    def version_shares(self, bucket: datetime) -> dict[str, float]:
        """Share of the bucket's traffic on each app version, oldest first; sums to 1.

        Versions without traffic in the bucket are left out.
        """
        cadence_days = RELEASE_CADENCE / _DAY
        since_reference = (to_utc(bucket) - self.rollout_start(REFERENCE_RELEASE)) / _DAY
        newest = REFERENCE_RELEASE + math.floor((since_reference + BETA_LEAD / _DAY) / cadence_days)
        newest = max(newest, 0)
        # ``adopted[n]``: the share on release ``n`` or newer. Everyone runs
        # ``1.0.0`` or newer, and nobody runs a build newer than ``newest`` yet.
        adopted = [1.0]
        adopted.extend(
            adoption(since_reference - (release - REFERENCE_RELEASE) * cadence_days)
            for release in range(1, newest + 1)
        )
        adopted.append(0.0)
        shares: dict[str, float] = {}
        for release in range(newest + 1):
            share = adopted[release] - adopted[release + 1]
            if share > 0.0:
                shares[version_label(release)] = share
        return shares

    def version_counts(self, bucket: datetime, *, event_name: str, count: int) -> dict[str, int]:
        """One event's ``count`` rows in ``bucket`` split across app versions."""
        return split_count(
            count,
            self.version_shares(bucket),
            key=(self.seed, event_name, "app_version", _epoch_hour(bucket)),
        )

    # -- platforms --------------------------------------------------------------

    def drift_progress(self, bucket: datetime) -> float:
        """How far the drift story has moved by ``bucket``: 0 before it, 1 from the anchor."""
        return drift_progress((self.anchor - to_utc(bucket)) / _DAY)

    def platform_shares(
        self, bucket: datetime, *, event_name: str, event_type: str
    ) -> dict[str, float]:
        """Share of one event's traffic in ``bucket`` on each platform; sums to 1."""
        bucket = to_utc(bucket)
        progress = self.drift_progress(bucket) if event_type == DRIFTING_EVENT_TYPE else 0.0
        weights = {
            platform: share
            * math.exp(
                _event_tilt(self.seed, event_name, platform)
                + _texture(self.seed, event_name, platform, bucket.weekday(), bucket.hour)
            )
            for platform, share in drift_platform_shares(progress).items()
        }
        total = math.fsum(weights.values())
        return {platform: weight / total for platform, weight in weights.items()}

    def platform_counts(
        self,
        bucket: datetime,
        *,
        event_name: str,
        event_type: str,
        base: int,
        count: int,
        excess: int = 0,
        excess_shares: Mapping[str, float] | None = None,
    ) -> dict[str, int]:
        """One event's rows in ``bucket`` split across platforms.

        ``count`` is the event's ordinary volume (:meth:`volume`); ``excess`` rows
        injected on top of it — a spike, a promo send — are split by
        ``excess_shares``, or by the bucket's own mix when that is ``None``.

        The steady part of ``count`` is split by a key that is the hour of the
        week, so it repeats exactly every week; the few rows the slow drift adds
        go by largest remainder. See the module docstring for why.
        """
        bucket = to_utc(bucket)
        shares = self.platform_shares(bucket, event_name=event_name, event_type=event_type)
        steady = min(self.steady_volume(base, event_name, bucket), count)
        weekly = split_count(
            steady,
            shares,
            key=(self.seed, event_name, "platform", bucket.weekday(), bucket.hour),
        )
        drift = _largest_remainder(count - steady, shares)
        injected = split_count(
            excess,
            excess_shares if excess_shares is not None else shares,
            key=(self.seed, event_name, "platform_excess", _epoch_hour(bucket)),
        )
        return _merged(weekly, drift, injected)


# Seeder-only keys on a demo's synthetic data source, beside the spike's
# (``synthetic.SPIKE_EVENT_KEY``): not connection settings anyone can set, so they
# stay out of ``SyntheticSettings`` and the API schema.
TRAFFIC_ANCHOR_KEY = "traffic_anchor"
TRAFFIC_SEED_KEY = "traffic_seed"


def traffic_params(traffic: DemoTraffic) -> dict[str, object]:
    """The data-source ``extra_params`` entries :func:`stored_traffic` reads back."""
    return {TRAFFIC_ANCHOR_KEY: traffic.anchor.isoformat(), TRAFFIC_SEED_KEY: traffic.seed}


def stored_traffic(extra_params: object) -> DemoTraffic | None:
    """The traffic a demo stored on its synthetic source, or ``None`` (any other source)."""
    if not isinstance(extra_params, dict):
        return None
    anchor = extra_params.get(TRAFFIC_ANCHOR_KEY)
    seed = extra_params.get(TRAFFIC_SEED_KEY)
    if not isinstance(anchor, str) or isinstance(seed, bool) or not isinstance(seed, int):
        return None
    try:
        parsed = datetime.fromisoformat(anchor)
    except ValueError:
        return None
    return DemoTraffic(anchor=parsed, seed=seed)
