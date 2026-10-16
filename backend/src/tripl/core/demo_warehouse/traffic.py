"""Expected traffic: how many rows an event sends in one hour, per platform.

``rate = base_rate x VOLUME_SCALE x daily x weekly x slow cycle x texture``,
then the calendar's incidents and the weekly promo on top. The shape is a pure
function of the absolute hour (never of a demo's creation time), so every demo
reads the same history from the shared warehouse.

The curve is stationary on purpose: a detector needs a few weeks of a stable
daily and weekly pattern to have a baseline, and a demo must never drift into
a state where everything reads as a slow decline.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from functools import lru_cache

from tripl.core.demo_warehouse import calendar
from tripl.core.demo_warehouse.roster import DEFAULT_PLATFORM_MIX, EventDef

#: Scales every event's ``base_rate``. With the roster as written the warehouse
#: holds about 1,150 rows in an average hour: ~1,500 at the evening peak, ~800
#: at the early-morning trough (UTC); ``test_demo_warehouse_model`` pins the band.
VOLUME_SCALE = 0.8
DAILY_AMPLITUDE = 0.25
#: UTC hour of the daily peak (evening in Europe, afternoon in the Americas).
DAILY_PEAK_HOUR = 18
WEEKLY_AMPLITUDE = 0.06
#: Fractional weekday of the weekly peak (Saturday afternoon), Monday = 0.
WEEKLY_PEAK_DAY = 5.5
#: A slow +/-3 % swell over four weeks, so the history is not a perfect repeat.
CYCLE_AMPLITUDE = 0.03
CYCLE_DAYS = 28
#: Per-event, per-hour-of-week wobble (+/-5 %), the same every week.
TEXTURE = 0.05

#: ``App Updated`` follows the rollout: up to this multiple of its base rate at
#: the steepest point of the adoption curve.
UPDATE_PEAK_MULTIPLIER = 5.0
UPDATE_EVENT = "App Updated"


def unit(*parts: object) -> float:
    """A deterministic number in ``[0, 1)`` for ``parts`` (stable across processes)."""
    digest = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def seed(*parts: object) -> int:
    digest = hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def shape(hour_start: datetime) -> float:
    """The shared daily x weekly x slow-cycle factor of one hour (mean ~1)."""
    hour = hour_start.hour + 0.5
    daily = 1.0 + DAILY_AMPLITUDE * math.cos(2 * math.pi * (hour - DAILY_PEAK_HOUR) / 24)
    weekday = hour_start.weekday() + hour / 24
    weekly = 1.0 + WEEKLY_AMPLITUDE * math.cos(2 * math.pi * (weekday - WEEKLY_PEAK_DAY) / 7)
    days = (hour_start - calendar.EPOCH) / calendar.DAY
    cycle = 1.0 + CYCLE_AMPLITUDE * math.sin(2 * math.pi * days / CYCLE_DAYS)
    return daily * weekly * cycle


@lru_cache(maxsize=65536)
def texture(event_name: str, hour_of_week: int) -> float:
    return 1.0 + TEXTURE * (2 * unit("texture", event_name, hour_of_week) - 1)


def _rollout_factor(hour_start: datetime) -> float:
    """``App Updated``'s multiple: highest while the newest release spreads."""
    index = calendar.release_index(hour_start)
    age = (hour_start - calendar.release_start(index)) / calendar.HOUR + 0.5
    x = (age - calendar.ROLLOUT_MIDPOINT_HOURS) / calendar.ROLLOUT_SCALE_HOURS
    # The logistic density, normalized to 1 at its peak.
    density = 4 * math.exp(-x) / (1 + math.exp(-x)) ** 2
    return 0.4 + UPDATE_PEAK_MULTIPLIER * density


def _applies(incident: calendar.Incident, event: EventDef) -> bool:
    if incident.scope == "event_type":
        return incident.target == event.event_type
    return incident.target == event.name


def platform_rates(event: EventDef, hour_start: datetime) -> dict[str, float]:
    """Expected rows of ``event`` in the hour starting at ``hour_start``, per platform.

    Before version effects: a retired event's laggard-only rows and a release's
    regressions thin these rates in :mod:`.rows`, row by row.
    """
    if event.base_rate <= 0:
        return {}
    how = hour_start.weekday() * 24 + hour_start.hour
    base = event.base_rate * VOLUME_SCALE * shape(hour_start) * texture(event.name, how)
    if event.name == UPDATE_EVENT:
        base *= _rollout_factor(hour_start)
    mix = platform_mix(event, hour_start)
    rates = {platform: base * weight for platform, weight in mix}
    hour_end = hour_start + calendar.HOUR
    for incident in calendar.incidents_between(hour_start, hour_end):
        if not _applies(incident, event):
            continue
        rates = _apply_incident(rates, incident)
    if event.name == calendar.PROMO_EVENT and calendar.promo_windows(hour_start, hour_end):
        rates = {platform: rate * calendar.PROMO_MULTIPLIER for platform, rate in rates.items()}
    return rates


def platform_mix(event: EventDef, hour_start: datetime) -> tuple[tuple[str, float], ...]:
    """The event's platform mix this hour, after any platform mix shift.

    An event with its own platform tilt (a mobile-only push) keeps it: a shift
    moves the shared mix, it does not put a push notification on the web.
    """
    if event.platforms is not None:
        return event.platforms
    mix = DEFAULT_PLATFORM_MIX
    for shift in calendar.MIX_SHIFTS:
        if shift.column != "platform" or event.event_type not in shift.event_types:
            continue
        weight = calendar.mix_shift_weight(shift, hour_start + calendar.HOUR / 2)
        mix = calendar.blend_mix(mix, shift.shifted, weight)
    return mix


def _apply_incident(rates: dict[str, float], incident: calendar.Incident) -> dict[str, float]:
    if incident.kind == "spike" and incident.platform in rates:
        total = sum(rates.values())
        extra = (incident.multiplier - 1.0) * total
        others = {p: r for p, r in rates.items() if p != incident.platform}
        other_total = sum(others.values()) or 1.0
        out = dict(rates)
        out[incident.platform] += extra * calendar.SPIKE_PLATFORM_SHARE
        for platform, rate in others.items():
            out[platform] += extra * (1 - calendar.SPIKE_PLATFORM_SHARE) * rate / other_total
        return out
    if incident.kind == "platform_shift" and incident.platform in rates:
        # Sized on the platform's DEFAULT share, so a campaign that lands inside
        # a mix shift (which may double that share) is no bigger than any other.
        base_share = dict(DEFAULT_PLATFORM_MIX).get(incident.platform, 0.0)
        extra = (incident.multiplier - 1.0) * sum(rates.values()) * base_share
        return {p: r + extra if p == incident.platform else r for p, r in rates.items()}
    return {p: r * incident.multiplier for p, r in rates.items()}


def expected_total(event: EventDef, hour_start: datetime) -> float:
    return sum(platform_rates(event, hour_start).values())
