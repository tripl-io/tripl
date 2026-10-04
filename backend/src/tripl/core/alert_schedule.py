"""Wall-clock cron schedules for alert digest delivery.

An alert destination may hold its alerts back and deliver them on a cadence
instead of after every metrics collection. The cadence is a standard 5-field
cron expression (``minute hour day-of-month month day-of-week``) evaluated in
the owning project's IANA timezone; the UI's friendly presets ("daily at
09:00") are just cron strings the frontend generates, so there is exactly ONE
encoding of a cadence in the system and one implementation that reads it.

Parsing rides on :mod:`celery.schedules`' ``crontab_parser``, which is already
a runtime dependency and already understands every field form worth
supporting (``*/15``, ``9,17``, ``mon-fri``). What celery cannot do for us is
answer "when did this last fire", because ``celery_app.conf.timezone`` is
pinned to UTC and beat owns the only schedule it evaluates — so the walk over
fire instants lives here.

Two properties the rest of the system depends on:

* **Instants are compared in UTC, never in wall time.** Local wall clock is
  not monotonic: on the autumn DST fold a wall time occurs twice, so "is
  02:30 still in the future?" has no answer in local terms. Every candidate is
  converted to UTC and compared there. Getting this wrong silently skips one
  digest per year. The flip side is deliberate: a cadence whose hour falls
  inside the fold hour fires TWICE that day, because 02:30 CEST and 02:30 CET
  are an hour apart and each opens a window of its own. Nothing downstream
  collapses that pair — the flusher claims windows by UTC instant, so its
  compare-and-set rejects a repeat of the same instant and has no way to
  recognise, or any reason to reject, a repeated wall time.
* **A nonexistent local time folds FORWARD.** On the spring gap, ``02:30``
  simply does not happen, and :class:`zoneinfo.ZoneInfo` will answer for it
  two ways: ``fold=0`` resolves it to 03:30 local (an hour late) while
  ``fold=1`` resolves it to 01:30 local (an hour EARLY). Only the forward
  answer is kept, deliberately. A skipped digest is a silent 24h outage and
  firing an hour late is the strictly better failure — but firing an hour
  BEFORE the wall time an operator configured is not a better failure at
  all: it ships a digest while the window it claims to summarise is still
  open, and it does so at a time the schedule never named.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from celery.schedules import ParseException, crontab_parser

__all__ = [
    "MAX_CRON_EXPRESSION_LENGTH",
    "CronSpec",
    "next_fire_at",
    "parse_cron",
    "previous_fire_at",
    "resolve_timezone",
    "validate_timezone",
]

# Long enough for any legitimate 5-field expression and short enough that the
# column (String(120)) and the parser can never be used as a memory amplifier.
MAX_CRON_EXPRESSION_LENGTH = 120

# Bounds the backwards walk when a watermark is very old or a cron is very
# sparse (``0 0 1 1 *`` fires once a year). Past this we report "no fire",
# which the flusher treats as "nothing due" rather than as an error.
_MAX_LOOKBACK_DAYS = 400
_MAX_LOOKAHEAD_DAYS = 400

_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


@dataclass(frozen=True)
class CronSpec:
    """A parsed 5-field cron expression.

    ``dom_restricted``/``dow_restricted`` record whether the day-of-month and
    day-of-week fields were literally ``*``. Standard cron day matching is not
    a plain conjunction: when BOTH day fields are restricted a day matches if
    EITHER matches, which is why the raw sets are not enough on their own.
    """

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool


def _parse_field(parser: crontab_parser, raw: str, field_name: str) -> frozenset[int]:
    try:
        return frozenset(parser.parse(raw))
    except (ParseException, ValueError, KeyError) as exc:
        # celery reports a bad minute as "Invalid weekday literal 'bogus'",
        # which is actively misleading in a 422. Name the field ourselves.
        raise ValueError(f"Invalid {field_name} field {raw!r}: {exc}") from exc


def parse_cron(expression: str) -> CronSpec:
    """Parse a 5-field cron expression, or raise ``ValueError``.

    The error message always names the offending field, because this doubles
    as the API-level validator for a user-typed string.
    """
    if len(expression) > MAX_CRON_EXPRESSION_LENGTH:
        raise ValueError(
            f"Cron expression must be at most {MAX_CRON_EXPRESSION_LENGTH} characters, "
            f"got {len(expression)}"
        )
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(
            f"Cron expression must have 5 fields "
            f"({' '.join(_FIELD_NAMES)}), got {len(fields)}: {expression!r}"
        )
    minute, hour, day_of_month, month, day_of_week = fields
    # Standard cron accepts 7 as Sunday; celery's parser caps day-of-week at 6
    # and rejects it with "Invalid end range: 7 > 6". So the field is parsed
    # over 0..7 and 7 is folded onto 0 AFTERWARDS.
    #
    # Rewriting the string before parsing (the obvious approach) only reaches
    # bare literals: `7` and `1,7` would work while `1-7` — Mon-Sun, the most
    # common way to write "every day" — still hit the parser's range check and
    # 422'd, along with `0-7` and `6-7`. Folding the parsed set covers every
    # form at once: literals, lists, ranges, steps and weekday names. `8` is
    # still rejected, because 8 is not a day.
    days_of_week = frozenset(
        0 if day == 7 else day
        for day in _parse_field(crontab_parser(8), day_of_week, "day-of-week")
    )
    return CronSpec(
        expression=expression,
        minutes=_parse_field(crontab_parser(60), minute, "minute"),
        hours=_parse_field(crontab_parser(24), hour, "hour"),
        days_of_month=_parse_field(crontab_parser(31, 1), day_of_month, "day-of-month"),
        months=_parse_field(crontab_parser(12, 1), month, "month"),
        days_of_week=days_of_week,
        dom_restricted=day_of_month.strip() != "*",
        dow_restricted=day_of_week.strip() != "*",
    )


def validate_timezone(name: str) -> str:
    """Return ``name`` if it is a resolvable IANA zone, else raise ``ValueError``."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {name!r}") from exc
    return name


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Resolve a project timezone, falling back to UTC for an unusable value.

    The flusher must never let one project's bad timezone string stop every
    other project's digest, so resolution degrades instead of raising.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def _day_matches(spec: CronSpec, day: _dt.date) -> bool:
    if day.month not in spec.months:
        return False
    dom_hit = day.day in spec.days_of_month
    # cron weekdays are 0=Sunday; isoweekday() is 1=Monday..7=Sunday.
    dow_hit = (day.isoweekday() % 7) in spec.days_of_week
    if spec.dom_restricted and spec.dow_restricted:
        return dom_hit or dow_hit
    return dom_hit and dow_hit


def _utc_instants(day: _dt.date, hour: int, minute: int, tz: ZoneInfo) -> list[_dt.datetime]:
    """Every distinct UTC instant the given local wall time maps to.

    Normally one. On the autumn fold a wall time is ambiguous and maps to two
    real instants an hour apart; BOTH are returned and both are fire instants,
    ordered by the caller in UTC because the wall clock is not monotonic there.
    A cadence inside the fold hour therefore gets two windows that day, and the
    flusher neither collapses them nor mistakes them for a double send: its
    compare-and-set is keyed on the instant, and the two differ. Collapsing the
    pair here is the tempting tidy-up and it is wrong — an hourly cadence has
    25 real fires on that local day, and keeping only ``fold=0`` drops one.

    On the spring gap it maps to NEITHER of ``fold``'s two answers, because it
    does not occur: ``fold`` stops choosing between two real readings of one
    instant and starts choosing which side of the jump to land on. ``fold=0``
    keeps the pre-transition offset and lands 02:30 on 03:30 local; ``fold=1``
    keeps the post-transition offset and lands it on 01:30 local, an hour
    BEFORE the configured time. Returning the pair there would hand the
    flusher two windows for a wall time that happened zero times, and the
    earlier of the two ships the digest ahead of its own schedule
     — so the gap keeps only the forward instant.

    The gap is recognised by converting ``first`` back into the zone: a wall
    time that exists always comes back as itself, and only a nonexistent one
    comes back as a different wall time.
    """
    naive = _dt.datetime(day.year, day.month, day.day, hour, minute)
    first = naive.replace(tzinfo=tz, fold=0).astimezone(_dt.UTC)
    second = naive.replace(tzinfo=tz, fold=1).astimezone(_dt.UTC)
    if first == second:
        return [first]
    if first.astimezone(tz).replace(tzinfo=None) != naive:
        # Spring gap, not autumn fold: ``second`` is the backwards resolution.
        return [first]
    return [first, second]


def _matching_days(
    spec: CronSpec,
    *,
    start: _dt.date,
    horizon: int,
    backwards: bool,
) -> list[_dt.date]:
    step = -1 if backwards else 1
    return [
        day
        for day in (start + _dt.timedelta(days=step * offset) for offset in range(horizon))
        if _day_matches(spec, day)
    ]


def previous_fire_at(
    expression: str,
    *,
    tz_name: str | None,
    now: _dt.datetime,
    not_before: _dt.datetime | None = None,
) -> _dt.datetime | None:
    """The most recent fire instant at or before ``now``, as aware UTC.

    ``not_before`` bounds the backwards walk. The only question the flusher
    asks is "did a fire fall in ``(last_flushed_at, now]``", so there is no
    point walking past the watermark — and without that bound a sparse cron
    such as ``0 0 1 1 *`` would need a year-long scan on every tick.
    """
    spec = parse_cron(expression)
    tz = resolve_timezone(tz_name)
    now_utc = now.astimezone(_dt.UTC)

    horizon = _MAX_LOOKBACK_DAYS
    if not_before is not None:
        elapsed_days = (now_utc - not_before.astimezone(_dt.UTC)).days
        horizon = min(_MAX_LOOKBACK_DAYS, max(2, elapsed_days + 2))

    # Start one day ahead: a zone east of UTC can already be on tomorrow's
    # local date while ``now`` in UTC is still on today's.
    start = (now_utc.astimezone(tz) + _dt.timedelta(days=1)).date()
    for day in _matching_days(spec, start=start, horizon=horizon + 1, backwards=True):
        candidates = [
            instant
            for hour in spec.hours
            for minute in spec.minutes
            for instant in _utc_instants(day, hour, minute, tz)
            if instant <= now_utc
        ]
        if candidates:
            return max(candidates)
    return None


def next_fire_at(
    expression: str,
    *,
    tz_name: str | None,
    after: _dt.datetime,
) -> _dt.datetime | None:
    """The first fire instant strictly after ``after``, as aware UTC.

    Used for the "next digest at" preview in the API and to evaluate a rule's
    cooldown against the moment a held alert will actually be delivered rather
    than the moment it was buffered.
    """
    spec = parse_cron(expression)
    tz = resolve_timezone(tz_name)
    after_utc = after.astimezone(_dt.UTC)

    # Start one day back for the same reason previous_fire_at starts one ahead.
    start = (after_utc.astimezone(tz) - _dt.timedelta(days=1)).date()
    for day in _matching_days(spec, start=start, horizon=_MAX_LOOKAHEAD_DAYS, backwards=False):
        candidates = [
            instant
            for hour in spec.hours
            for minute in spec.minutes
            for instant in _utc_instants(day, hour, minute, tz)
            if instant > after_utc
        ]
        if candidates:
            return min(candidates)
    return None
