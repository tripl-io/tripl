"""The warehouse rows of one hour, generated deterministically from the model.

Both engines read their rows from here: the writer materializes exactly these
rows into PostgreSQL, and the in-memory engine aggregates exactly these rows in
Python. Parity between the two is therefore by construction, not by test.

Every random draw comes from a ``random.Random`` seeded with a digest of
``(SEED, absolute hour, event, platform)`` and only ``Random.random()`` is used
(its Mersenne Twister stream is stable across Python versions, unlike
``gauss`` or ``choices``). Nothing depends on a demo's id or creation time.

The hourly count is the expected rate plus normal noise of
:data:`NOISE_RATIO` times Poisson's spread, rounded stochastically (so the mean
is exact even for rates below one). Real traffic is noisier than Poisson; a
demo's is quieter on purpose: the detector's floor is Poisson's spread, so at
full Poisson noise every high-volume series throws a few 3-sigma anomalies a
week that no story explains, and they crowd out the incidents that do.

Bump :data:`MODEL_VERSION` with any change that alters a row: the writer then
rewrites the history it holds instead of mixing two models in one table.
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from itertools import accumulate
from operator import itemgetter

from tripl.core.demo_warehouse import calendar, roster, traffic
from tripl.core.demo_warehouse.roster import (
    COUNTRY_MIX,
    CURRENCY_BY_COUNTRY,
    PRICES,
    EventDef,
    PropertyDef,
    PropertyValue,
    UnplannedEventDef,
)

MODEL_VERSION = "4"
SEED = "tripl-demo-warehouse"
#: The count noise, as a share of Poisson's standard deviation.
NOISE_RATIO = 0.5

#: Pseudonymous users, partitioned by country in :data:`COUNTRY_MIX` shares so a
#: user keeps one country.
USER_POOL = 24_000
#: A session is one user's activity inside a block of this many hours.
SESSION_HOURS = 2
#: Share of a country's users active in one session block. With ~2,300 rows a
#: block that is ~375 active users, so a session holds about six events and a
#: day has ~4,500 sessions, like a real app's.
ACTIVE_USER_SHARE = 1 / 64
#: Share of ``Purchase Completed`` rows recorded at 0.00 (a 100 %-off coupon):
#: what a range contract on ``amount`` catches.
FREE_PURCHASE_SHARE = 0.02
#: On top of the shares above, every hour's first two ``Purchase Completed``
#: rows carry the two contract findings, one each, alternating by hour: one
#: recorded at 0.00 and one paid in GBP. ``Purchase Completed`` draws only a
#: handful of rows an hour (any three hours hold at least six), so random
#: shares alone leave hours with neither, and the demo's first live collection
#: — as short as three hours — would miss a finding its story promises.
CONTRACT_STORY_COUNTRY = "GB"
#: The same promise for the variable-value drift: an hour whose ``Trial
#: Started`` rows drew no ``prod_weekly`` (the plan's undocumented product) has
#: its first one carry it. A 10 % share of ~7 trials an hour misses a whole
#: three-hour window about one window in nine (81 of 717 over September 2026,
#: among them 2026-10-19 11:00-14:00 and 12:00-15:00), and that window is the
#: catalog scan's ``SCAN_LOOKBACK_HOURS``, the only reading of ``product_id``
#: the drift detector gets: a demo built then opened without its value drift.
#: Only empty hours are touched, so the share moves from 10 % to about 17 %.
VALUE_DRIFT_STORY_EVENT = roster.VARIABLE_DRIFT_EVENT
#: Share of completed orders later refunded.
REFUNDED_ORDER_SHARE = 0.04
#: The events whose rows become orders, and the status each order gets.
ORDER_EVENTS: dict[str, str] = {
    "Purchase Completed": "completed",
    "Trial Converted": "completed",
    "Subscription Renewed": "completed",
    "Purchase Failed": "failed",
}
ORDER_EVENT_NAMES: frozenset[str] = frozenset(ORDER_EVENTS)
#: Hours of ``events`` rows kept per process. One hour is ~1,150 rows (~1.3 MB),
#: so the cache holds about two days: a 1-day replay chunk is read many times.
HOUR_CACHE_SIZE = 50
#: Hours of ``orders`` rows kept per process (~10 rows an hour).
ORDER_HOUR_CACHE_SIZE = 24 * 40
#: Hours of distinct ``session_id`` sets kept per process (a few hundred ids an
#: hour), so the distinct-sessions metric of a whole history reuses the hours
#: its collection generated instead of generating them again.
SESSION_HOUR_CACHE_SIZE = 24 * 45

type Row = dict[str, object]

_USER_IDS: tuple[str, ...] = tuple(f"u{n:05d}" for n in range(USER_POOL))
_SECONDS: tuple[timedelta, ...] = tuple(timedelta(seconds=s) for s in range(3600))
_PRICE_DECIMALS: dict[str, dict[str, Decimal]] = {
    product: {currency: Decimal(price) for currency, price in by_currency.items()}
    for product, by_currency in PRICES.items()
}
_FREE = Decimal("0.00")


def _user_ranges() -> dict[str, tuple[int, int]]:
    ranges: dict[str, tuple[int, int]] = {}
    start = 0
    for index, (country, share) in enumerate(COUNTRY_MIX):
        size = USER_POOL - start if index == len(COUNTRY_MIX) - 1 else round(USER_POOL * share)
        ranges[country] = (start, size)
        start += size
    return ranges


_USER_RANGES = _user_ranges()


class _Picker:
    """A weighted choice over a fixed table, drawn with one uniform."""

    __slots__ = ("cumulative", "values")

    def __init__(self, weighted: tuple[tuple[object, float], ...]) -> None:
        total = sum(weight for _, weight in weighted) or 1.0
        self.values = tuple(value for value, _ in weighted)
        self.cumulative = tuple(accumulate(weight / total for _, weight in weighted))

    def pick(self, u: float) -> object:
        index = bisect.bisect_right(self.cumulative, u)
        return self.values[min(index, len(self.values) - 1)]


@lru_cache(maxsize=4096)
def _picker(weighted: tuple[tuple[object, float], ...]) -> _Picker:
    return _Picker(weighted)


def draw_count(rng: random.Random, lam: float) -> int:
    """A count with mean ``lam`` and :data:`NOISE_RATIO` of Poisson's spread.

    Box-Muller from ``rng.random()`` alone, then stochastic rounding: the
    fraction rounds up with its own probability, so no rate is biased.
    """
    if lam <= 0:
        return 0
    u1 = rng.random() or 1e-12
    u2 = rng.random()
    z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
    value = lam + NOISE_RATIO * math.sqrt(lam) * z
    if value <= 0:
        return 0
    whole = math.floor(value)
    return whole + (1 if rng.random() < value - whole else 0)


def _seed(*parts: object) -> int:
    return traffic.seed(SEED, *parts)


#: The writer materializes whole slices of this length.
SLICE = timedelta(minutes=5)


def floor_to_slice(at: datetime) -> datetime:
    """The start of the five-minute slice holding ``at``."""
    return at - (at - calendar.EPOCH) % SLICE


def available_through(now: datetime) -> datetime:
    """The end (exclusive) of the rows the warehouse holds at ``now``.

    The end of the CURRENT five-minute slice: the writer runs every five
    minutes and writes the slice it runs in whole, so an hour is complete in
    the warehouse before that hour ends and a collection of it, however soon
    after the hour, never reads a partly written bucket. The rows of the
    slice's remaining minutes are visible early; nothing reads them, because
    every collection window ends at or before ``now``.
    """
    return floor_to_slice(now) + SLICE


def epoch_hour(hour_start: datetime) -> int:
    return math.floor((hour_start - calendar.EPOCH) / calendar.HOUR)


def hour_start_of(index: int) -> datetime:
    return calendar.EPOCH + index * calendar.HOUR


def country_mix(event_type: str, at: datetime) -> tuple[tuple[str, float], ...]:
    """The country mix of ``event_type``'s rows at ``at``, after any country shift."""
    mix = COUNTRY_MIX
    for shift in calendar.MIX_SHIFTS:
        if shift.column == "country" and event_type in shift.event_types:
            mix = calendar.blend_mix(mix, shift.shifted, calendar.mix_shift_weight(shift, at))
    return mix


def _property_value(
    prop: PropertyDef, rng: random.Random, *, broken_type: bool, no_results: bool
) -> PropertyValue:
    if prop.int_range is not None:
        low, high = prop.int_range
        number = 0 if no_results else low + int(rng.random() * (high - low + 1))
        return str(number) if broken_type else number
    value = _picker(prop.choices).pick(rng.random())
    return value  # type: ignore[return-value]


class _HourContext:
    """What every row of one hour shares: versions, regressions, mixes."""

    __slots__ = (
        "active_users",
        "country_pickers",
        "hour_start",
        "index",
        "laggard",
        "regressions",
        "session_block",
        "versions",
    )

    def __init__(self, hour_start: datetime) -> None:
        middle = hour_start + calendar.HOUR / 2
        self.hour_start = hour_start
        self.index = epoch_hour(hour_start)
        self.versions = _picker(calendar.version_shares(middle))
        self.regressions = calendar.regressions_by_version(middle)
        self.laggard = calendar.laggard_version(middle)
        self.country_pickers = {
            event_type.name: _picker(country_mix(event_type.name, middle))
            for event_type in roster.event_types()
        }
        self.session_block = self.index // SESSION_HOURS
        # Per country: (first user, pool size, offset of this block's active
        # slice, slice size). The slice rotates block to block.
        self.active_users = {
            country: (
                start,
                size,
                int(traffic.unit("users", self.session_block, country) * size),
                max(4, round(size * ACTIVE_USER_SHARE)),
            )
            for country, (start, size) in _USER_RANGES.items()
        }

    def user(self, country: str, u: float) -> str:
        """An active user of ``country`` in this session block, from one uniform."""
        start, size, offset, active = self.active_users[country]
        return _USER_IDS[start + (offset + int(u * active)) % size]


def _event_rows(context: _HourContext, event: EventDef, platform: str, count: int) -> list[Row]:
    rng = random.Random(_seed("rows", context.index, event.name, platform))
    rows: list[Row] = []
    country_picker = context.country_pickers[event.event_type]
    products = _picker(event.products) if event.products else None
    session_block = context.session_block
    no_results = event.name == "Search No Results"
    for _ in range(count):
        version = context.laggard if event.legacy_only else str(context.versions.pick(rng.random()))
        regression = context.regressions.get(version)
        if (
            regression is not None
            and regression.event == event.name
            and rng.random() >= regression.kept_share
        ):
            continue
        moment = context.hour_start + _SECONDS[int(rng.random() * 3600)]
        country = str(country_picker.pick(rng.random()))
        user_id = context.user(country, rng.random())
        product = str(products.pick(rng.random())) if products is not None else None
        amount: Decimal | None = None
        currency: str | None = None
        if event.priced and product is not None:
            currency = CURRENCY_BY_COUNTRY[country]
            amount = _PRICE_DECIMALS[product][currency]
            if event.name == roster.SCHEMA_DRIFT_EVENT and rng.random() < FREE_PURCHASE_SHARE:
                amount = _FREE
        broken = (
            platform == calendar.PROPERTY_TYPE_CHANGE_PLATFORM
            and calendar.property_type_broken(version)
        )
        properties = {
            prop.key: _property_value(
                prop,
                rng,
                broken_type=broken and prop.key == calendar.PROPERTY_TYPE_CHANGE_KEY,
                no_results=no_results,
            )
            for prop in event.properties
        }
        rows.append(
            {
                "event_time": moment,
                "event_type": event.event_type,
                "event_name": event.name,
                "screen_name": event.screen_name,
                "platform": platform,
                "app_version": version,
                "country": country,
                "button_id": event.button_id,
                "product_id": product,
                "amount": amount,
                "currency": currency,
                "user_id": user_id,
                "session_id": f"{user_id}-{session_block:x}",
                "properties": properties,
            }
        )
    return rows


def _unplanned_rates(unplanned: UnplannedEventDef, hour_start: datetime) -> dict[str, float]:
    hour_end = hour_start + calendar.HOUR
    covered = timedelta(0)
    for name, start, end in calendar.unplanned_bursts(hour_start, hour_end):
        if name == unplanned.name:
            covered += min(end, hour_end) - max(start, hour_start)
    if covered <= timedelta(0):
        return {}
    rate = unplanned.rate_during_burst * (covered / calendar.HOUR)
    return {platform: rate * weight for platform, weight in unplanned.platforms}


def _unplanned_rows(
    context: _HourContext, unplanned: UnplannedEventDef, platform: str, count: int
) -> list[Row]:
    rng = random.Random(_seed("rows", context.index, unplanned.name, platform))
    rows: list[Row] = []
    country_picker = context.country_pickers[unplanned.event_type]
    session_block = context.session_block
    for _ in range(count):
        version = str(context.versions.pick(rng.random()))
        moment = context.hour_start + _SECONDS[int(rng.random() * 3600)]
        country = str(country_picker.pick(rng.random()))
        user_id = context.user(country, rng.random())
        rows.append(
            {
                "event_time": moment,
                "event_type": unplanned.event_type,
                "event_name": unplanned.name,
                "screen_name": None,
                "platform": platform,
                "app_version": version,
                "country": country,
                "button_id": None,
                "product_id": None,
                "amount": None,
                "currency": None,
                "user_id": user_id,
                "session_id": f"{user_id}-{session_block:x}",
                "properties": {},
            }
        )
    return rows


def hour_counts(hour_start: datetime) -> dict[tuple[str, str], int]:
    """Rows drawn per ``(event name, platform)`` in the hour, BEFORE version effects.

    A release regression then drops some rows of its event (see :func:`events_for_hour`);
    for every other event this is exactly its row count.
    """
    index = epoch_hour(hour_start)
    counts: dict[tuple[str, str], int] = {}
    for event in roster.live_events():
        for platform, lam in traffic.platform_rates(event, hour_start).items():
            rng = random.Random(_seed("count", index, event.name, platform))
            counts[(event.name, platform)] = draw_count(rng, lam)
    for unplanned in roster.unplanned_events():
        for platform, lam in _unplanned_rates(unplanned, hour_start).items():
            rng = random.Random(_seed("count", index, unplanned.name, platform))
            counts[(unplanned.name, platform)] = draw_count(rng, lam)
    return counts


#: Time first; the rest only breaks ties, so the order is total and stable.
_row_order = itemgetter("event_time", "event_name", "user_id")


def _generate_events(index: int, only: frozenset[str] | None = None) -> list[Row]:
    """The hour's ``events`` rows, or only those of the events named in ``only``.

    Each ``(event, platform)`` draws from its own seeded stream, so generating a
    subset yields exactly the subset of the full hour's rows.
    """
    hour_start = hour_start_of(index)
    context = _HourContext(hour_start)
    events: list[Row] = []
    story: list[Row] = []
    trials: list[Row] = []
    for event in roster.live_events():
        if only is not None and event.name not in only:
            continue
        for platform, lam in traffic.platform_rates(event, hour_start).items():
            count = draw_count(random.Random(_seed("count", index, event.name, platform)), lam)
            if count:
                generated = _event_rows(context, event, platform, count)
                if event.name == roster.SCHEMA_DRIFT_EVENT:
                    story.extend(generated)
                elif event.name == VALUE_DRIFT_STORY_EVENT:
                    trials.extend(generated)
                events.extend(generated)
    _mark_contract_story(context, story)
    _mark_value_drift_story(trials)
    for unplanned in roster.unplanned_events():
        if only is not None and unplanned.name not in only:
            continue
        for platform, lam in _unplanned_rates(unplanned, hour_start).items():
            count = draw_count(random.Random(_seed("count", index, unplanned.name, platform)), lam)
            if count:
                events.extend(_unplanned_rows(context, unplanned, platform, count))
    events.sort(key=_row_order)
    return events


def _mark_contract_story(context: _HourContext, purchases: list[Row]) -> None:
    """Make the hour's first two priced purchases a free one and a GBP one.

    ``purchases`` are the hour's ``Purchase Completed`` rows in generation
    order (platform by platform), which a subset generation reproduces, so the
    orders derived from them agree. Even hours mark the free row first, odd
    hours the GBP row, so an hour with a single purchase still alternates.
    """
    priced = [row for row in purchases if row["amount"] is not None]
    findings = ("free", "gbp") if context.index % 2 == 0 else ("gbp", "free")
    for row, finding in zip(priced, findings, strict=False):
        if finding == "free":
            row["amount"] = _FREE
            continue
        country = CONTRACT_STORY_COUNTRY
        user_id = context.user(country, traffic.unit("contract", context.index, country))
        row["country"] = country
        row["currency"] = CURRENCY_BY_COUNTRY[country]
        row["amount"] = _PRICE_DECIMALS[str(row["product_id"])][CURRENCY_BY_COUNTRY[country]]
        row["user_id"] = user_id
        row["session_id"] = f"{user_id}-{context.session_block:x}"


def _mark_value_drift_story(trials: list[Row]) -> None:
    """Make the hour's first trial ``prod_weekly`` when none of them drew it.

    ``trials`` are the hour's ``Trial Started`` rows in generation order, as
    in :func:`_mark_contract_story`. No draw is consumed, so every other value
    of the hour is the one it would have been. Trials are unpriced and never
    become orders, so the product is the only column that changes.
    """
    if trials and all(row["product_id"] != roster.UNDOCUMENTED_PRODUCT for row in trials):
        trials[0]["product_id"] = roster.UNDOCUMENTED_PRODUCT


_SESSIONS_BY_HOUR: dict[int, frozenset[str]] = {}


def _remember_sessions(index: int, rows: tuple[Row, ...]) -> None:
    _SESSIONS_BY_HOUR[index] = frozenset(str(row["session_id"]) for row in rows)
    while len(_SESSIONS_BY_HOUR) > SESSION_HOUR_CACHE_SIZE:
        del _SESSIONS_BY_HOUR[next(iter(_SESSIONS_BY_HOUR))]


@lru_cache(maxsize=HOUR_CACHE_SIZE)
def _events_hour(index: int) -> tuple[Row, ...]:
    rows = tuple(_generate_events(index))
    _remember_sessions(index, rows)
    return rows


def sessions_in_hour(hour_start: datetime) -> frozenset[str]:
    """The distinct ``session_id`` of the hour starting at ``hour_start``.

    Kept apart from the rows (and longer): generating an hour remembers its
    sessions, so a day's distinct sessions is the union of its hours' sets.
    """
    index = epoch_hour(hour_start)
    found = _SESSIONS_BY_HOUR.get(index)
    if found is None:
        _remember_sessions(index, _events_hour(index))
        found = _SESSIONS_BY_HOUR[index]
    return found


@lru_cache(maxsize=ORDER_HOUR_CACHE_SIZE)
def _orders_hour(index: int) -> tuple[Row, ...]:
    return tuple(_orders(index, _generate_events(index, ORDER_EVENT_NAMES)))


def _orders(index: int, events: list[Row]) -> list[Row]:
    rng = random.Random(_seed("orders", index))
    orders: list[Row] = []
    for row in events:
        status = ORDER_EVENTS.get(str(row["event_name"]))
        if status is None:
            continue
        refunded = rng.random() < REFUNDED_ORDER_SHARE
        if status == "completed" and refunded:
            status = "refunded"
        product = str(row["product_id"])
        country = str(row["country"])
        currency = row["currency"] or CURRENCY_BY_COUNTRY[country]
        amount = row["amount"]
        if amount is None:
            amount = _PRICE_DECIMALS[product][str(currency)]
        properties = row["properties"]
        method = properties.get("payment_method") if isinstance(properties, dict) else None
        orders.append(
            {
                "created_at": row["event_time"],
                "order_id": f"ord-{index:06d}-{len(orders):04d}",
                "amount": amount,
                "currency": currency,
                "user_id": row["user_id"],
                "country": country,
                "status": status,
                "payment_method": method or "card",
            }
        )
    return orders


def events_for_hour(hour_start: datetime) -> tuple[Row, ...]:
    """Every ``events`` row of the hour starting at ``hour_start``, in time order.

    Cached per process. The rows are shared: callers must not mutate them.
    """
    return _events_hour(epoch_hour(hour_start))


def orders_for_hour(hour_start: datetime) -> tuple[Row, ...]:
    """Every ``orders`` row of the hour starting at ``hour_start``, in time order."""
    return _orders_hour(epoch_hour(hour_start))


def table_rows_for_hour(table: str, hour_start: datetime) -> tuple[Row, ...]:
    return orders_for_hour(hour_start) if table == "orders" else events_for_hour(hour_start)


def hours_between(
    table: str, t0: datetime, t1: datetime
) -> Iterator[tuple[datetime, tuple[Row, ...] | list[Row]]]:
    """``table``'s rows with time in ``[t0, t1)``, one ``(hour start, rows)`` per hour.

    An hour the window holds whole comes back as the cached tuple itself; an
    hour it cuts, as the list of its rows inside the window.
    """
    if t1 <= t0:
        return
    time_column = "created_at" if table == "orders" else "event_time"
    index = epoch_hour(t0)
    last = epoch_hour(t1 - timedelta(microseconds=1))
    while index <= last:
        hour_start = hour_start_of(index)
        rows = table_rows_for_hour(table, hour_start)
        if hour_start >= t0 and hour_start + calendar.HOUR <= t1:
            yield hour_start, rows
        else:
            yield hour_start, [row for row in rows if t0 <= row[time_column] < t1]  # type: ignore[operator]
        index += 1


def rows_between(table: str, t0: datetime, t1: datetime) -> Iterator[Row]:
    """``table``'s rows with time in ``[t0, t1)``, streamed an hour at a time."""
    for _, rows in hours_between(table, t0, t1):
        yield from rows


def clear_cache() -> None:
    """Drop the cached hours (tests, and a worker after a model change)."""
    _events_hour.cache_clear()
    _orders_hour.cache_clear()
    _SESSIONS_BY_HOUR.clear()
