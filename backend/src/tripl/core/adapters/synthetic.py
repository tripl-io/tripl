"""Local, in-memory synthetic warehouse adapter.

The synthetic adapter replaces the never-queried fake ClickHouse source that
generated demo projects used to carry. It exercises the *normal* warehouse-facing
code paths (preview, schema introspection, time-bucketed counts / breakdowns /
aggregates, sql-metric collection) against a bounded, deterministic dataset that
is generated entirely in memory — it NEVER opens a socket or touches the
filesystem.

Design
------
* Two tables, ``events`` and ``orders``, with fixed schemas matching the demo
  scenario. Rows are generated deterministically from a fixed seed via SHA-256
  (never the salted builtin ``hash()``), and every digest is keyed on ABSOLUTE
  time (the UTC epoch hour / date ordinal of the row's bucket), so the same seed
  yields the same rows for the same instant whichever anchor generated them —
  AS LONG AS both anchors put that instant in the same regime (see the next
  bullet). That is stronger than "same seed and anchor are byte-for-byte
  identical" and it has to be: ``registry._build_synthetic`` rebuilds the adapter
  on every scan with ``anchor=None``, i.e. with a MOVING anchor, so
  anchor-relative keys made the same absolute hour hold different rows on every
  scan. The total row count is capped.
* Cross-anchor identity is NOT guaranteed across the ongoing/sampled boundary,
  and the gap is not a rounding error. Which hours are "ongoing" is measured back
  from the anchor (``_generate_events``: ``ongoing_start_hour = total_hours -
  SYNTHETIC_ONGOING_HOURS``), so an hour that has aged out of that window between
  two scans is regenerated at the sampled scale — a different sample, not a
  thinned copy of the earlier one. Measured at ``DEFAULT_SEED``: one hour held
  ~7.8k rows while it was ongoing and 3 once it had aged out, and those 3 were
  not a subset of the earlier rows (``test_synthetic_warehouse_numbers`` pins both halves at
  its own seed). Since the anchor moves on every scan, that boundary sweeps the
  dataset continuously, and a re-collection whose window reaches further back
  than ``SYNTHETIC_ONGOING_HOURS`` will not reproduce what an earlier read of the
  same hours returned.
* The most-recent ``SYNTHETIC_ONGOING_HOURS`` hours are generated at each event's
  seeded *base* volume (a believable daily/weekly shape with mild noise), so a
  live scan's current window continues the demo's seeded baseline instead of
  reading a near-empty warehouse and stamping a spurious drop.
  Older hours stay at a small "sample" scale so the 30-day dataset (preview,
  active-sessions history) stays comfortably within the row budget.
* A generated demo's source also carries the demo's own traffic model
  (:func:`~tripl.core.adapters.synthetic_traffic.stored_traffic`). Built with
  it, the ongoing hours serve EXACTLY what the demo stored for them — each
  event's volume and its platform and app-version split — and every row's
  platform and version follow the model's mix, so the scheduled collection that
  rewrites the newest hours writes back what is already there. Without one (any
  other synthetic source, every test that builds the adapter directly) the
  dataset is exactly what it always was.
* Every abstract method aggregates the in-memory rows in Python according to the
  STRUCTURED params it receives (time window, regular/breakdown columns,
  aggregation + measure, ``AggregateSpec`` list, top-N ``values_limit``). It does
  not parse SQL beyond three things: which table ``base_query`` selects (a query
  that mentions ``orders`` reads the orders table, otherwise the events table),
  which columns it projects (a bare ``SELECT a, b, c FROM ...`` list narrows the
  columns the caller sees; anything else means "every column"), and its top-level
  ``WHERE`` predicate, which is evaluated row by row. The predicate matters
  because the per-metric fact collector delivers a row filter by WRAPPING the
  source — ``SELECT * FROM (<source>) AS _filtered WHERE <combined>`` — so an
  adapter that ignored it answered the UNFILTERED question and disagreed with the
  batched path, whose filter arrives as ``AggregateSpec.filter_sql``.
* Filter fragments are read in the dialect this source DECLARES.
  ``measure_validator._DB_TYPE_DIALECT`` maps ``"synthetic"`` to ClickHouse, so
  everything the collector compiles for it arrives back-tick quoted and
  backslash escaped; the evaluator accepts that spelling (and PostgreSQL's, since
  named / free-text filters are user text) instead of silently matching nothing.
* The sql-metric path funnels through ``get_preview_rows`` with the metric SQL as
  ``base_query``. The seeded aggregate statements (distinct sessions per day) are
  recognized by EXACT match and computed from the dataset; a plain table scan
  returns rows; any other SQL raises :class:`SyntheticCapabilityError` rather than
  fabricating data.
* Read-only only. Row/time limits are honored and the dataset is held to a
  row-count budget at construction. ``test_connection`` is an honest LOCAL check
  (the dataset is present) — it never claims a real warehouse connection.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from tripl.core.adapters.base import (
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    SchemaColumn,
    SchemaTable,
)
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import coerce_aggregation, requires_measure
from tripl.core.adapters.synthetic_traffic import DemoTraffic
from tripl.core.bucketing import floor_to_bucket, to_utc
from tripl.json_paths import is_property_field
from tripl.models.domain_enums import MetricAggregation

# Default deterministic seed for the synthetic dataset. Overridable per adapter so
# two sources can carry distinct-but-stable data. hashlib (not builtin hash()) is
# used for every derivation so the shape is reproducible across processes.
DEFAULT_SEED = 20260711

# Bounds. The dataset is intentionally small: ~30 days of hourly events and daily
# orders. The hard cap is a defence-in-depth guard so no code path can generate
# an unbounded dataset regardless of the requested window.
SYNTHETIC_HISTORY_DAYS = 30
# Sized so the ONGOING window (``SYNTHETIC_ONGOING_HOURS`` hours at the summed
# per-event base of the full 18-event roster, at the seasonal shape's peak) plus
# the small older sampled tail fit with headroom. ``_generate_events`` fills the
# window oldest-first, so a cap that bites would silently truncate the NEWEST
# hours — exactly the buckets a live scan reads — hence the headroom.
# ``test_synthetic_dataset_stays_within_row_budget`` pins the margin.
SYNTHETIC_MAX_ROWS = 65000

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
# ``SELECT <projection> FROM ...`` — the only projection shape recognized.
_SELECT_LIST_RE = re.compile(r"^\s*select\s+(.+?)\s+from\b", re.IGNORECASE | re.DOTALL)
# Aggregate / join / function tokens that make a query more than a plain table
# scan. Any such token in a query that is NOT the recognized sql-metric shape is a
# capability boundary — we refuse rather than guess.
_NON_SCAN_RE = re.compile(
    r"\b(group\s+by|having|join|union|distinct)\b"
    r"|\b(count|sum|avg|min|max|uniq|tostartof\w*)\s*\(",
    re.IGNORECASE,
)

# The interval code the seeded ``active_sessions`` sql metric buckets by.
_DAY_INTERVAL = "1d"


class SyntheticCapabilityError(WarehouseCapabilityError):
    """The synthetic adapter was asked for something it cannot honestly compute.

    Raised for unrecognized SQL and unsupported filter expressions. The adapter
    NEVER fabricates a result for an unsupported request — the caller gets a
    clear capability error instead.

    It subclasses :class:`~tripl.core.adapters.errors.WarehouseCapabilityError`
    (and through it ``ValueError``) because these messages are exactly what that
    class exists for: text *we* wrote about a request the operator can change,
    carrying no host, port or credential. Being a plain ``RuntimeError`` meant
    the sentence in ``_reject_unsupported_scan`` — written for a user — reached a
    demo user as "Scan failed due to an internal error.", because both sanitisers
    key on the BASE class and this was outside it:
    ``metric_preview_service._warehouse_error_message`` surfaces a
    ``WarehouseCapabilityError`` verbatim, and the worker's ``user_facing_error``
    does the same for every member of ``worker/tasks/_errors._CURATED_ERRORS``.
    Inheriting is the right shape rather than naming this class at each sanitiser:
    the PostgreSQL and BigQuery adapters raise the base class too, so one entry
    covers three adapters.
    """


# Ordered (name, type) column definitions per table. ``type`` uses warehouse-ish
# names so type-based logic downstream (numeric measure detection) behaves.
_EVENTS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("event_time", "DateTime"),
    ("event_type", "String"),
    ("event_name", "String"),
    ("screen_name", "String"),
    ("platform", "String"),
    ("button_id", "String"),
    ("product_id", "String"),
    ("amount", "Float64"),
    ("currency", "String"),
    ("app_version", "String"),
    ("user_id", "String"),
    ("session_id", "String"),
)
_ORDERS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("created_at", "DateTime"),
    ("amount", "Float64"),
    ("currency", "String"),
    ("user_id", "String"),
    ("country", "String"),
    ("status", "String"),
)
_EVENTS_NULLABLE = frozenset({"button_id", "product_id", "amount", "currency"})

# The column allowlist per table, in ONE place. Both gates that hold a caller to
# a real column answer to it — ``_validate_column`` for the STRUCTURED params and
# ``_filter_column`` for a name parsed out of a WHERE fragment — so the two
# cannot drift into disagreeing about what the tables hold.
_TABLE_COLUMN_NAMES: dict[str, frozenset[str]] = {
    "events": frozenset(name for name, _ in _EVENTS_COLUMNS),
    "orders": frozenset(name for name, _ in _ORDERS_COLUMNS),
}


def _table_columns(table: str) -> frozenset[str]:
    """Every column name ``table`` has (``events`` is the fallback table)."""
    return _TABLE_COLUMN_NAMES.get(table, _TABLE_COLUMN_NAMES["events"])


class SyntheticEventDef(NamedTuple):
    """One synthetic event identity and the column values its rows carry."""

    event_type: str
    event_name: str
    screen_name: str | None
    button_id: str | None
    product_id: str | None
    amount: float | None
    currency: str | None
    ongoing_base: int
    # A retired identity stays on the roster (it keeps its fold rule and its
    # authored ``ongoing_base``, which the roster test pins to the plan) but emits
    # no rows at all. The demo's planted dead event is one: its volume dried up
    # 45 days ago, and an hourly emission meant the first collection bumped its
    # ``last_seen_at`` and promoted it back to live. Mirrors
    # ``services.demo.builders.warehouse.DEAD_EVENT_NAME``.
    retired: bool = False


# The synthetic warehouse's event roster. It MIRRORS
# ``services.demo.builders.plan.event_specs`` — the demo's single source of truth
# for the authored catalog — EXHAUSTIVELY: one row per seeded event, with the
# same ``ongoing_base`` volume and the same per-event column values the plan
# documents as field values. ``ongoing_base`` is the hourly volume generated for
# the *most-recent* hours (the ongoing window a live scan reads back), so a
# rescan continues the seeded baseline rather than dropping.
#
# Exhaustiveness is the whole point: this used to list only the 7 highest-volume
# identities, so an hourly metrics collection rewrote the window with counts for
# 7 of 18 events and the detector read the other 11 as "dropped to zero" within
# an hour of a demo's creation. The values are duplicated
# rather than imported from the plan: ``core/`` importing ``services/`` is a
# direction this repo takes once and deliberately
# (``core/analyzers/release_regression.py``), not one Python or a lint rule
# forbids, and a second exception here would pull the demo seeder's import graph
# into every synthetic adapter build. So the copy stands and
# ``test_synthetic_event_defs_cover_every_seeded_event_spec`` pins the two rosters
# together in BOTH directions.
_EVENT_DEFS: tuple[SyntheticEventDef, ...] = (
    # screen_view — the plan documents ``screen_name`` (+ ``${platform}``).
    SyntheticEventDef("screen_view", "Home Screen View", "home", None, None, None, None, 1800),
    SyntheticEventDef("screen_view", "Paywall View", "paywall", None, None, None, None, 600),
    SyntheticEventDef(
        "screen_view", "Onboarding Step 1 View", "onboarding_step1", None, None, None, None, 900
    ),
    SyntheticEventDef(
        "screen_view", "Onboarding Step 2 View", "onboarding_step2", None, None, None, None, 700
    ),
    SyntheticEventDef("screen_view", "Profile Screen View", "profile", None, None, None, None, 400),
    SyntheticEventDef(
        "screen_view", "Settings Screen View", "settings", None, None, None, None, 200
    ),
    # click — the plan documents ``button_id`` (+ ``screen_name`` where authored).
    SyntheticEventDef("click", "Buy Button Click", "paywall", "buy_now", None, None, None, 300),
    SyntheticEventDef(
        "click",
        "Skip Onboarding Click",
        "onboarding_step1",
        "skip_onboarding",
        None,
        None,
        None,
        180,
    ),
    SyntheticEventDef(
        "click", "Restore Purchase Click", "paywall", "restore_purchase", None, None, None, 40
    ),
    SyntheticEventDef("click", "Share Button Click", None, "share", None, None, None, 120),
    SyntheticEventDef("click", "Legacy CTA Click", None, "old_cta", None, None, None, 20),
    # purchase — ``product_id`` values stay inside the demo's DOCUMENTED value
    # list (see ``services.demo.builders.variables``) so a rescan cannot invent an
    # undocumented SKU; ``amount``/``currency`` are emitted only for the events
    # whose plan spec documents them.
    SyntheticEventDef(
        "purchase", "Purchase Completed", "paywall", None, "prod_monthly", 9.99, "USD", 120
    ),
    SyntheticEventDef(
        "purchase", "Purchase Failed", "paywall", None, "prod_monthly", None, "USD", 15
    ),
    SyntheticEventDef("purchase", "Trial Started", "paywall", None, "prod_annual", None, None, 250),
    SyntheticEventDef(
        "purchase", "Subscription Renewed", "paywall", None, "prod_annual", 9.99, "USD", 80
    ),
    SyntheticEventDef(
        "purchase", "Refund Processed", "paywall", None, "prod_lifetime", 9.99, None, 8
    ),
    SyntheticEventDef("purchase", "Promo Applied", "paywall", None, "prod_monthly", None, None, 35),
    SyntheticEventDef(
        "purchase",
        "Subscription Cancelled",
        "paywall",
        None,
        "prod_annual",
        None,
        None,
        22,
        retired=True,
    ),
)

# Distinct ``event_name`` values the synthetic adapter can emit — the single
# source of truth for callers that must enumerate every identity in the dataset.
# The demo scan's ``event_group_rules`` are generated from this tuple so a rescan
# folds each synthetic identity back into its curated catalog event (0 new
# events); adding a new ``_EVENT_DEFS`` row automatically extends the rule set,
# so a new synthetic name can never silently reintroduce raw pipe-named drafts.
# ``dict.fromkeys`` de-dupes while preserving first-seen order.
SYNTHETIC_EVENT_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(event_def.event_name for event_def in _EVENT_DEFS)
)

_PLATFORMS = ("ios", "android", "web")
_APP_VERSIONS = ("1.2.0", "1.3.0", "1.4.0")
_CURRENCIES = ("USD", "EUR")
_COUNTRIES = ("US", "GB", "DE", "FR", "CA", "JP")
# Weighted so ``completed`` dominates (mirrors a healthy store).
_ORDER_STATUSES = (
    "completed",
    "completed",
    "completed",
    "completed",
    "pending",
    "refunded",
    "failed",
)

# --- ongoing (live-scan) volume ----------------------------------------------
# Number of most-recent hours generated at the seeded ``ongoing_base`` scale. A
# live scan only reads its current window (the newest hours), so only those need
# to continue the demo's baseline; older hours stay at the small "sample" scale
# below. Sized so ``SYNTHETIC_ONGOING_HOURS`` hours at the summed base volume plus
# the small older tail stay under ``SYNTHETIC_MAX_ROWS`` for any anchor.
SYNTHETIC_ONGOING_HOURS = 6
# Seasonal-shape constants MIRROR ``services.demo.noise.hourly_volume`` (which
# builds the seeded EventMetric baseline) so the ongoing counts land inside the
# anomaly detector's per-bucket tolerance band. daily peaks ~08:00 UTC; weekly is
# a gentle hump; the constant drift matches the seeded baseline's ~+4% level at its
# most-recent bucket (``hourly_volume`` ramps its drift 0 -> 0.04 across history,
# and the ongoing window is always the most-recent hours).
_ONGOING_DAILY_AMPLITUDE = 0.35
_ONGOING_WEEKLY_AMPLITUDE = 0.08
_ONGOING_RECENT_DRIFT = 0.04
# Mild deterministic texture (+/- this percent) so the series is not a bare
# sinusoid; kept well under the seeded +/-7% texture so |ongoing - seeded| stays
# inside the detector's drop band (worst-case |z| ~= 2.65 vs the 3-sigma gate)
# even at the low-volume trough of a mid-volume event.
_ONGOING_TEXTURE_PCT = 1

# --- the demo's seeded spike ---------------------------------------------------
# A generated demo seeds one spike: one event at this multiple of its volume for
# one hour, with almost all of the excess from iOS so the signal's "Why" panel has
# a story to tell. The demo's warehouse builder writes it into the stored series
# and the synthetic source is told the hour (``stored_spike`` below), so
# the scheduled collection that re-reads that hour reads the same spike back
# instead of overwriting it with an ordinary hour.
SPIKE_MULTIPLIER = 3
SPIKE_PLATFORM_SPLIT: dict[str, float] = {"ios": 0.85, "android": 0.10, "web": 0.05}


@dataclass(frozen=True)
class SyntheticSpike:
    """The hour and event a demo seeded its spike into."""

    event_name: str
    hour: datetime


# Where the demo seeder records the spike on its data source's ``extra_params``.
# Seeder-only keys, read here and nowhere else: they are not connection settings
# a user can set, so they stay out of ``SyntheticSettings`` and the API schema.
SPIKE_EVENT_KEY = "spike_event"
SPIKE_HOUR_KEY = "spike_hour"


def stored_spike(extra_params: object) -> SyntheticSpike | None:
    """The spike a demo seeded into this source, or ``None`` (any other source)."""
    if not isinstance(extra_params, dict):
        return None
    event_name = extra_params.get(SPIKE_EVENT_KEY)
    hour = extra_params.get(SPIKE_HOUR_KEY)
    if not isinstance(event_name, str) or not isinstance(hour, str):
        return None
    try:
        return SyntheticSpike(event_name=event_name, hour=to_utc(datetime.fromisoformat(hour)))
    except ValueError:
        return None


def _projection_columns(base_query: str) -> tuple[str, ...] | None:
    """The explicit column list a query projects, or ``None`` for "everything".

    A real warehouse only ever hands a scan the columns its query selects, so the
    catalog pipeline only sees those. This adapter used to answer ``get_columns``
    with the whole table no matter what the query asked for, which made a demo
    scan observe warehouse-internal columns the curated plan does not model —
    and the hourly catalog sync then auto-created junk ``FieldDefinition`` rows
    (with raw sample values like ``s29_5``) for them.

    Only a bare comma-separated identifier list is recognized; ``*``, expressions
    and aliases fall back to the full table, so the adapter still never guesses.
    """
    match = _SELECT_LIST_RE.match(base_query)
    if match is None:
        return None
    items = [item.strip() for item in match.group(1).split(",")]
    if not items or any(not _IDENT_RE.match(item) for item in items):
        return None
    return tuple(items)


# --- reading SQL text: string literals, identifiers, operators, WHERE ---------
#
# This adapter has to READ fragments it did not write.
# ``measure_validator._DB_TYPE_DIALECT`` declares ``"synthetic"`` to be a
# ClickHouse dialect, so ``_fact_conditions._resolve_condition_fragment`` compiles
# a structured condition to ``` `status` = 'completed' ``` — back-tick quoted,
# backslash escaped. Until it was fixed the evaluator here understood only
# bare identifiers and did a chain of ``.strip("'")`` / ``.replace("''", "'")`` on
# the literal, so ``` `status` = 'completed' ``` matched NOTHING (every bucket
# collected NULL) and ``` `status` != 'completed' ``` matched EVERY row. The
# declaration is the contract and this is the half that must honour it; the fix
# is NOT to stop quoting on the compile side, where quoting is what makes a
# reserved column name work on PostgreSQL and BigQuery.
#
# Both dialect spellings are accepted deliberately. Named and free-text row
# filters are explicitly dialect-specific USER text (see
# ``_fact_conditions._resolve_combined_filter``), so someone who typed ``"status"``
# or ``'o''brien'`` against a PostgreSQL habit must not get a wrong number either.

_LITERAL_MASK_CHAR = "\x00"

#: The escape sequences ``measure_validator.quote_sql_string_literal`` emits for
#: the backslash dialects. Anything else is refused rather than decoded: mapping
#: an unknown ``\x`` to a bare ``x`` would silently mis-read ``'C:\temp'``, which
#: ClickHouse reads as ``C:<TAB>emp``, and this module never answers a filter it
#: cannot evaluate faithfully.
_BACKSLASH_ESCAPES: dict[str, str] = {"\\": "\\", "'": "'", "n": "\n", "r": "\r"}

#: Comparison operators, longest match first at a given position so ``!=`` can
#: never be split as ``=`` and ``<=`` never as ``<``.
_FILTER_OPERATORS: tuple[str, ...] = ("!=", "<>", ">=", "<=", "=", ">", "<")

_WHERE_KEYWORD = "where"


def _string_literal_end(text: str, start: int) -> int:
    """Index just past the single-quoted literal that begins at ``text[start]``.

    Both escape conventions are honoured: ``\\'`` (what
    ``quote_sql_string_literal`` emits for ClickHouse / BigQuery) and ``''``
    (PostgreSQL, and what a hand-written free-text filter may carry). Accepting
    both means ``'a''b'`` and ``'a\\'b'`` denote the same value here, which is the
    intended trade for reading user text of unknown provenance.
    """
    index = start + 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "'":
            if index + 1 < length and text[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    msg = "Unterminated string literal in SQL text"
    raise SyntheticCapabilityError(msg)


def _decode_string_literal(token: str) -> str:
    """Decode ONE complete single-quoted SQL literal to the value it denotes.

    A single left-to-right pass, not a chain of ``.replace()``: chaining decodes
    a value ending in a backslash wrongly, which is the same ordering hazard
    ``quote_sql_string_literal`` calls out on the compile side. The
    ``.strip("'")`` this replaces was worse still — it strips ALL leading and
    trailing quotes, so ``''`` (the empty string) and any value that legitimately
    begins or ends with a quote were already read as something else.
    """
    if not token.startswith("'") or _string_literal_end(token, 0) != len(token):
        msg = f"Unsupported filter literal: {token!r}"
        raise SyntheticCapabilityError(msg)
    decoded: list[str] = []
    index = 1
    end = len(token) - 1
    while index < end:
        char = token[index]
        if char == "\\":
            # ``_string_literal_end`` consumed this pair, so index + 1 <= end.
            escape = _BACKSLASH_ESCAPES.get(token[index + 1])
            if escape is None:
                msg = f"Unsupported escape sequence in filter literal: {token!r}"
                raise SyntheticCapabilityError(msg)
            decoded.append(escape)
            index += 2
            continue
        if char == "'":
            # The scanner proved the only unpaired quote is the final character,
            # so a quote here is the first half of a ``''`` pair.
            decoded.append("'")
            index += 2
            continue
        decoded.append(char)
        index += 1
    return "".join(decoded)


def _mask_string_literals(text: str) -> str:
    """A same-length copy of ``text`` with every string literal blanked out.

    Position-preserving, so a caller finds parentheses, operators and keywords on
    the MASK and slices the ORIGINAL at the same indices. One scanner decides
    where a literal starts and ends, which is what stops the three text-splitting
    helpers below from each re-deriving it and each getting it wrong: ``AND``
    inside ``country = 'Trinidad and Tobago'`` was split as a boolean connective,
    and ``<=`` inside ``status = 'a<=b'`` was split as an operator.

    This is an EVALUATOR's masker, not a read-only safety gate's: it honours the
    backslash escapes ``quote_sql_string_literal`` emits, and an unterminated
    literal is an error here rather than something to scan raw and keep
    rejecting. A gate that only has to decide "is this safe to send" can be
    conservative about a quote that never closes; a reader that has to produce a
    NUMBER cannot.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "'":
            out.append(text[index])
            index += 1
            continue
        end = _string_literal_end(text, index)
        out.append(_LITERAL_MASK_CHAR * (end - index))
        index = end
    return "".join(out)


def _has_word_boundaries(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is not glued to an identifier character."""
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _filter_identifier(raw: str) -> str:
    """Strip ONE layer of dialect identifier quoting from a filter operand.

    Back-ticks (ClickHouse / BigQuery) or double quotes (PostgreSQL), matched as
    a pair. The result is NOT checked against the identifier regex here — the
    caller checks membership in the table's column allowlist, which is strictly
    stronger, and it is the error message the caller should own. A dot-qualified
    ``` `t`.`col` ``` therefore survives as the single name ``` t`.`col ```, which
    no table has, and is refused: the synthetic tables carry no alias, so there is
    nothing such a name could faithfully resolve to.
    """
    text = raw.strip()
    for quote in ("`", '"'):
        if len(text) >= 2 and text[0] == quote and text[-1] == quote:
            return text[1:-1]
    return text


def _split_comparison(atom: str) -> tuple[str, str, str]:
    """Split one filter atom into ``(left, operator, right)``.

    The search runs over the literal mask, so an operator character inside a
    value is invisible and the first operator at quote depth zero wins. A
    parenthesis outside a literal means the atom is not a plain column/literal
    comparison — a subquery, or the ``parseDateTime64BestEffort(...)`` a
    timestamp condition compiles to — and is refused here rather than guessed at.
    """
    masked = _mask_string_literals(atom)
    if "(" in masked or ")" in masked:
        msg = f"Unsupported filter expression: {atom!r}"
        raise SyntheticCapabilityError(msg)
    for index in range(len(masked)):
        for operator in _FILTER_OPERATORS:
            if masked.startswith(operator, index):
                right = atom[index + len(operator) :]
                return atom[:index].strip(), operator, right.strip()
    msg = f"Unsupported filter expression: {atom!r}"
    raise SyntheticCapabilityError(msg)


#: Clause keywords that may follow a top-level ``WHERE`` and end its predicate.
#: Without them the whole tail was read as the predicate, so ``WHERE status =
#: 'completed' ORDER BY created_at`` failed every collection on the source with
#: ``Unsupported filter literal: "'completed' ORDER BY created_at"`` — naming a
#: "literal" that is not one.
#:
#: IGNORED clauses change neither which rows exist nor what any of them holds as
#: far as a table-scan method is concerned: this adapter imposes its own order
#: (by time column) and its own cap (the ``limit`` argument) on every answer,
#: and it already ignores the same clauses on a query with no ``WHERE`` at all.
#: ``OFFSET`` / ``FETCH`` ride with ``LIMIT`` because they are its other spelling.
_TRAILING_CLAUSES_IGNORED: frozenset[str] = frozenset({"order", "limit", "offset", "fetch"})

#: REFUSED clauses reshape the row set — grouping, filtering groups, or combining
#: statements — so evaluating the predicate and dropping them would answer a
#: different question. They are refused BY NAME, so the message says what to
#: change.
_TRAILING_CLAUSES_REFUSED: frozenset[str] = frozenset(
    {"group", "having", "window", "qualify", "union", "intersect", "except"}
)
_TRAILING_CLAUSES: frozenset[str] = _TRAILING_CLAUSES_IGNORED | _TRAILING_CLAUSES_REFUSED
_CLAUSE_NAMES: dict[str, str] = {"group": "GROUP BY"}

#: One SQL word, matched on the lower-cased literal mask.
_SQL_WORD_RE = re.compile(r"(?<![a-z0-9_])[a-z_][a-z0-9_]*")

#: ``SELECT <projection> FROM (`` at the very start of a statement — the shape
#: ``_fact_conditions._resolve_fact_operand_query`` wraps a fact source in, and
#: the one every warehouse adapter wraps ``base_query`` in too. Matched on the
#: literal mask, lower-cased; the projection may contain neither a parenthesis
#: nor a second ``FROM``, so a function call or an earlier statement (``… UNION
#: SELECT * FROM (``) is not mistaken for this shape.
_DERIVED_SOURCE_RE = re.compile(r"\s*select\s+(?:(?!\bfrom\b)[^()])*\bfrom\s*\(")

#: ``WITH <name> AS (`` at the very start of a statement: a single CTE whose body
#: is then read by a plain ``SELECT … FROM <name>``.
_CTE_SOURCE_RE = re.compile(r"\s*with\s+([a-z_][a-z0-9_]*)\s+as\s*\(")

#: What may follow a derived source's closing parenthesis (or the CTE's
#: ``FROM <name>``): an optional alias, then the end of the statement or a
#: clause this module reads. Anything else — a JOIN, a comma, a second source —
#: is not the single-source shape and falls through to the refusal below.
_SOURCE_TAIL_RE = re.compile(
    r"\s*(?:as\s+)?(?:[a-z_][a-z0-9_]*)?\s*"
    r"(?:\Z|(?=(?:where|order|limit|offset|fetch|group|having|window|qualify)\b))"
)


def _matching_paren(masked: str, open_index: int) -> int | None:
    """Index of the ``)`` closing the ``(`` at ``masked[open_index]``, or ``None``."""
    depth = 0
    for index in range(open_index, len(masked)):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _single_source_span(masked: str) -> tuple[int, int] | None:
    """The ``[start, end)`` span of the ONE inner statement ``masked`` reads from.

    Two shapes, both of which really reach this adapter: ``SELECT * FROM
    (<inner>) AS alias [WHERE …]`` (the per-metric fact wrapper, and every
    ``_src`` wrapper) and ``WITH name AS (<inner>) SELECT * FROM name [WHERE …]``
    (a CTE-backed fact table). ``None`` for anything else, including a CTE list
    of more than one entry.
    """
    lowered = masked.lower()
    derived = _DERIVED_SOURCE_RE.match(lowered)
    if derived is not None:
        open_index = derived.end() - 1
        close = _matching_paren(masked, open_index)
        if close is not None and _SOURCE_TAIL_RE.match(lowered, close + 1):
            return open_index + 1, close
        return None
    cte = _CTE_SOURCE_RE.match(lowered)
    if cte is None:
        return None
    open_index = cte.end() - 1
    close = _matching_paren(masked, open_index)
    if close is None:
        return None
    reader = rf"\s*select\s+(?:(?!\bfrom\b)[^()])*\bfrom\s+{re.escape(cte.group(1))}\b"
    body = re.compile(reader).match(lowered, close + 1)
    if body is None or not _SOURCE_TAIL_RE.match(lowered, body.end()):
        return None
    return open_index + 1, close


def _where_predicates(base_query: str) -> list[str]:
    """Every ``WHERE`` predicate that narrows the rows ``base_query`` reads.

    The outer statement's own top-level predicate, ANDed (by the caller) with
    the predicates of the single inner statement it reads from, recursively. The
    recursion is what makes the per-metric fact path agree with the batched one:
    ``_fact_conditions._resolve_fact_operand_query`` wraps the fact table's
    stored SQL as ``SELECT * FROM (<fact sql>) AS _filtered WHERE <combined>``,
    which puts the fact table's OWN ``WHERE`` at paren depth 1. Reading depth 0
    only, as this module used to, applied ``<combined>`` alone and silently
    dropped the fact table's predicate — on the demo's ``… FROM orders WHERE
    currency = 'USD'`` with a ``status = 'completed'`` filter the per-metric sum
    was 7303.86 against the batched (and correct) 4908.56, and a CTE-backed fact
    source (``WITH completed AS (… WHERE status = 'completed') SELECT * FROM
    completed``) scanned the whole table on both paths (the
    shape of an earlier bug).

    A ``WHERE`` at depth >= 1 that is NOT inside that one recognised inner
    statement — a subquery in the projection, a join, a CTE list — is REFUSED:
    returning the table without it would answer a different question silently,
    which this module never does.
    """
    masked = _mask_string_literals(base_query)
    span = _single_source_span(masked)
    predicates: list[str] = []
    if span is not None:
        predicates.extend(_where_predicates(base_query[span[0] : span[1]]))
    lowered = masked.lower()
    depth = 0
    for index, char in enumerate(masked):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif (
            depth > 0
            and (span is None or not span[0] <= index < span[1])
            and lowered.startswith(_WHERE_KEYWORD, index)
            and _has_word_boundaries(masked, index, index + len(_WHERE_KEYWORD))
        ):
            msg = (
                "The synthetic warehouse cannot evaluate a WHERE clause nested inside a "
                "subquery it does not read from; use a plain table scan, a single "
                "SELECT * FROM (...) wrapper, or a single CTE"
            )
            raise SyntheticCapabilityError(msg)
    own = _trailing_where_predicate(base_query)
    if own is not None:
        predicates.append(own)
    return predicates


def _trailing_where_predicate(base_query: str) -> str | None:
    """The top-level ``WHERE`` predicate of ``base_query``, or ``None``.

    Depth-aware over parentheses and blind to string literals: a ``WHERE`` at
    depth >= 1 is not this one (``_where_predicates`` reads the inner statement
    separately, or refuses it).

    More than one ``WHERE`` at depth 0 is REFUSED rather than resolved by taking
    the last (the obvious alternative). Two top-level ``WHERE`` clauses mean a
    set operation this adapter does not model, and applying the second one to
    every row would answer a different question silently — which is the whole
    failure mode this function exists to end.

    The predicate ends at the first top-level clause keyword after it (see
    ``_TRAILING_CLAUSES_IGNORED`` / ``_TRAILING_CLAUSES_REFUSED``) rather than at
    the end of the string.
    """
    masked = _mask_string_literals(base_query)
    lowered = masked.lower()
    depth_at: list[int] = []
    depth = 0
    for char in masked:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        depth_at.append(depth)
    positions: list[int] = []
    clause_after: tuple[int, str] | None = None
    # Whole words only, found on the mask, so neither a value (``'order'``) nor
    # a longer identifier (``order_id``) can end the predicate.
    for match in _SQL_WORD_RE.finditer(lowered):
        index = match.start()
        if depth_at[index] != 0:
            continue
        word = match.group(0)
        if word == _WHERE_KEYWORD:
            positions.append(index)
        elif positions and clause_after is None and word in _TRAILING_CLAUSES:
            clause_after = (index, word)
    if not positions:
        return None
    if len(positions) > 1:
        msg = "The synthetic warehouse cannot evaluate a query with two top-level WHERE clauses"
        raise SyntheticCapabilityError(msg)
    predicate_end = len(base_query)
    if clause_after is not None:
        clause_index, clause = clause_after
        if clause in _TRAILING_CLAUSES_REFUSED:
            msg = (
                "The synthetic warehouse cannot evaluate a query with a "
                f"{_CLAUSE_NAMES.get(clause, clause.upper())} clause after its WHERE; it "
                "only reads plain table scans"
            )
            raise SyntheticCapabilityError(msg)
        predicate_end = clause_index
    predicate = base_query[positions[0] + len(_WHERE_KEYWORD) : predicate_end].strip()
    if not predicate:
        msg = "The synthetic warehouse cannot evaluate an empty WHERE clause"
        raise SyntheticCapabilityError(msg)
    return predicate


def _normalize_sql(statement: str) -> str:
    """Lower-cased, whitespace-collapsed, ``;``-free form of a statement.

    Matches what the sql-metric collector actually hands the adapter:
    ``validate_select_sql`` trims the statement and strips a single trailing
    semicolon, so the recognized set below must be normalized the same way or an
    otherwise-identical statement would miss it over punctuation.
    """
    return re.sub(r"\s+", " ", statement.strip().rstrip(";").strip()).lower()


# The EXACT sql-metric statements this adapter can compute, normalized by
# ``_normalize_sql``. MEMBERSHIP, deliberately — not the substring probe this
# replaces, which tested for ``tostartofday(event_time)``,
# ``count(distinct session_id)`` and ``from events`` appearing ANYWHERE in the
# query. Under that probe an edited metric SQL that added ``WHERE platform =
# 'ios'``, divided the count by 2, or read ``events_archive`` (``from events`` is
# a substring of it) still matched, and the adapter answered with the unfiltered
# whole-dataset series — a different question, answered confidently, in the one
# module whose stated contract is to refuse rather than fabricate.
# A demo is editable by its creator and by any owner
# (``project_service`` permits both), so that input is reachable.
#
# Exact matching is brittle BY DESIGN: reformatting the seeded statement breaks
# the demo's Active Sessions metric loudly at collection time instead of quietly
# computing something else. Add the new spelling to this set in the same commit —
# do not soften it back into a substring probe.
_ACTIVE_SESSIONS_STATEMENTS: frozenset[str] = frozenset(
    {
        # Current: the statement ``services.demo.builders.catalog`` seeds today.
        # ``test_synthetic_warehouse_numbers`` imports that constant and asserts it is a
        # member, so the seeder cannot change the text without this set noticing.
        _normalize_sql(
            "SELECT toStartOfDay(event_time) AS ts, "
            "count(DISTINCT session_id) AS value FROM events GROUP BY ts"
        ),
        # Legacy: the GROUP BY-less statement every demo created before
        # that fix still carries in ``MetricDefinition.config``. Real
        # ClickHouse rejects it ("not under aggregate function and not in GROUP
        # BY"), which is why the seeder stopped writing it — but an existing demo
        # must keep collecting, and one frozenset entry is a far smaller change
        # than an Alembic data migration over a user-editable JSON config.
        _normalize_sql(
            "SELECT toStartOfDay(event_time) AS ts, count(DISTINCT session_id) AS value FROM events"
        ),
    }
)


def _digest_int(*parts: object) -> int:
    """Stable non-negative int from ``parts`` via SHA-256 (never builtin hash)."""
    key = "|".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")


def _pick(seq: tuple[object, ...], *parts: object) -> object:
    return seq[_digest_int(*parts) % len(seq)]


def _bval(value: object) -> str:
    """String form of a breakdown value, mirroring ``ifNull(toString(x), '')``."""
    return "" if value is None else str(value)


def _ongoing_hourly_count(seed: int, base: int, event_name: str, bucket: datetime) -> int:
    """Seeded-scale hourly volume with daily/weekly shape and mild noise.

    Mirrors the shape family of ``services.demo.noise.hourly_volume`` (which builds
    the seeded EventMetric baseline) closely enough that a live scan's per-event
    count for the ongoing window lands inside the anomaly detector's per-bucket
    tolerance band, so scanning an idle demo does not surface a spurious drop.
    Digest-seeded (never ``Date.now``/random), per the adapter's
    determinism contract, and never below 1.
    """
    hour = bucket.hour
    weekday = bucket.weekday()
    daily = math.sin((hour - 2) * math.pi / 12)
    weekly = _ONGOING_WEEKLY_AMPLITUDE * math.sin(weekday * math.pi / 3.5)
    span = 2 * _ONGOING_TEXTURE_PCT + 1
    texture = (
        _digest_int(seed, "vol_texture", event_name, weekday, hour) % span - _ONGOING_TEXTURE_PCT
    ) / 100.0
    raw = base * (1 + _ONGOING_DAILY_AMPLITUDE * daily + weekly + _ONGOING_RECENT_DRIFT + texture)
    return max(1, round(raw))


def _event_row(
    seed: int,
    bucket: datetime,
    event_def: SyntheticEventDef,
    session_span: int,
    day_ordinal: int,
    *occ: object,
) -> dict[str, object]:
    """Build one deterministic event row for ``event_def``.

    ``occ`` is the per-occurrence digest key (``(epoch_hour, j)`` for a sampled
    row, ``(epoch_hour, event_name, k)`` for an ongoing per-event row) so the two
    generation regimes stay independent yet reproducible. Every component of the
    key is ABSOLUTE: ``epoch_hour`` identifies the bucket itself rather than its
    offset from the anchor, so the same hour keeps the same minutes, platforms,
    versions, users and sessions across the anchor moves ``_build_synthetic``
    makes on every scan.

    ``day_ordinal`` is the bucket's UTC date as a proleptic-Gregorian ordinal,
    which is what ``session_id`` is namespaced by — so a session pool belongs to
    a UTC DAY, the same unit ``toStartOfDay`` buckets the seeded active-sessions
    metric into.
    """
    minute = _digest_int(seed, "ev_minute", *occ) % 60
    second = _digest_int(seed, "ev_second", *occ) % 60
    session_slot = _digest_int(seed, "ev_sess", *occ) % session_span
    return {
        "event_time": bucket + timedelta(minutes=minute, seconds=second),
        "event_type": event_def.event_type,
        "event_name": event_def.event_name,
        "screen_name": event_def.screen_name,
        "platform": _PLATFORMS[_digest_int(seed, "ev_plat", *occ) % len(_PLATFORMS)],
        "button_id": event_def.button_id,
        "product_id": event_def.product_id,
        "amount": event_def.amount,
        "currency": event_def.currency,
        "app_version": _APP_VERSIONS[_digest_int(seed, "ev_ver", *occ) % len(_APP_VERSIONS)],
        "user_id": f"u{_digest_int(seed, 'ev_user', *occ) % 500}",
        "session_id": f"s{day_ordinal}_{session_slot}",
    }


def _spike_platform(seed: int, *occ: object) -> str:
    """The platform of one row of a spike's EXCESS, drawn by ``SPIKE_PLATFORM_SPLIT``."""
    roll = (_digest_int(seed, "spike_plat", *occ) % 10_000) / 10_000
    for platform, share in SPIKE_PLATFORM_SPLIT.items():
        if roll < share:
            return platform
        roll -= share
    return next(iter(SPIKE_PLATFORM_SPLIT))


def _ongoing_hour_rows(
    seed: int,
    bucket: datetime,
    epoch_hour: int,
    session_span: int,
    day_ordinal: int,
    spike: SyntheticSpike | None = None,
    traffic: DemoTraffic | None = None,
) -> list[dict[str, object]]:
    """One ongoing-window hour: each event at its seeded ``ongoing_base`` volume.

    The hour a demo seeded its spike into carries that spike here too: the spiked
    event at ``SPIKE_MULTIPLIER`` times its volume, the excess split by
    ``SPIKE_PLATFORM_SPLIT``. The scheduled collection re-reads the newest hours
    and rewrites them, so a warehouse without the spike erased the demo's seeded
    signal at the first top of the hour after the demo was generated.

    With the demo's ``traffic`` the hour is the demo's own: see
    :func:`_traffic_event_rows`.
    """
    out: list[dict[str, object]] = []
    for event_def in _EVENT_DEFS:
        if event_def.retired:
            continue
        event_name = event_def.event_name
        spiked = spike is not None and spike.event_name == event_name and spike.hour == bucket
        if traffic is not None:
            out.extend(
                _traffic_event_rows(
                    seed,
                    bucket,
                    epoch_hour,
                    session_span,
                    day_ordinal,
                    event_def,
                    traffic,
                    spiked=spiked,
                )
            )
            continue
        count = _ongoing_hourly_count(seed, event_def.ongoing_base, event_name, bucket)
        total = count
        if spiked:
            total = count * SPIKE_MULTIPLIER
        for k in range(total):
            occ = (epoch_hour, event_name, k)
            row = _event_row(seed, bucket, event_def, session_span, day_ordinal, *occ)
            if k >= count:
                row["platform"] = _spike_platform(seed, *occ)
            out.append(row)
    return out


def _traffic_event_rows(
    seed: int,
    bucket: datetime,
    epoch_hour: int,
    session_span: int,
    day_ordinal: int,
    event_def: SyntheticEventDef,
    traffic: DemoTraffic,
    *,
    spiked: bool,
) -> list[dict[str, object]]:
    """One event's rows in one ongoing hour of a demo's own traffic.

    The volume the demo stored for the hour, exactly, and exactly its platform
    and app-version split — the counts ``services.demo.breakdowns`` writes for
    the seeder and the runtime tick — so a collection that rewrites the hour
    writes back what is already there. Before, every row drew its version and
    platform uniformly, and each collection overwrote the newest hours with
    three versions at a third each against a history that had none.
    """
    event_name = event_def.event_name
    count = traffic.volume(event_def.ongoing_base, event_name, bucket)
    excess = count * (SPIKE_MULTIPLIER - 1) if spiked else 0
    platforms = traffic.platform_counts(
        bucket,
        event_name=event_name,
        event_type=event_def.event_type,
        base=event_def.ongoing_base,
        count=count,
        excess=excess,
        excess_shares=SPIKE_PLATFORM_SPLIT if spiked else None,
    )
    versions = traffic.version_counts(bucket, event_name=event_name, count=count + excess)
    platform_of = _spread(platforms, seed, "traffic_platform", epoch_hour, event_name)
    version_of = _spread(versions, seed, "traffic_version", epoch_hour, event_name)
    out: list[dict[str, object]] = []
    for k in range(count + excess):
        occ = (epoch_hour, event_name, k)
        row = _event_row(seed, bucket, event_def, session_span, day_ordinal, *occ)
        row["platform"] = platform_of[k]
        row["app_version"] = version_of[k]
        out.append(row)
    return out


def _spread(counts: Mapping[str, int], *key: object) -> list[str]:
    """``counts`` as one value per row, interleaved in a deterministic order.

    A stride through the grouped values rather than a shuffle: the counts stay
    exact, no row needs a digest of its own, and a preview page is not one
    platform's run of rows.
    """
    values = [value for value, count in counts.items() for _ in range(count)]
    total = len(values)
    if total < 2:
        return values
    stride = max(1, round(total * 0.618))
    while math.gcd(stride, total) != 1:
        stride += 1
    offset = _digest_int(*key) % total
    return [values[(offset + k * stride) % total] for k in range(total)]


def _pick_share(shares: Mapping[str, float], *key: object) -> str:
    """One value drawn from ``shares`` by a digest of ``key``."""
    roll = (_digest_int(*key) % 1_000_000) / 1_000_000 * math.fsum(shares.values())
    chosen = ""
    for value, share in shares.items():
        chosen = value
        if roll < share:
            break
        roll -= share
    return chosen


def _sampled_hour_rows(
    seed: int,
    bucket: datetime,
    epoch_hour: int,
    session_span: int,
    day_ordinal: int,
    traffic: DemoTraffic | None = None,
) -> list[dict[str, object]]:
    """One older-history hour: a small sampled scatter across the event roster.

    With the demo's ``traffic``, each row's platform and app version are drawn
    from the demo's mix for that hour instead of evenly, so a preview of older
    rows shows the versions the demo's charts do.
    """
    out: list[dict[str, object]] = []
    n_events = 3 + _digest_int(seed, "ev_count", epoch_hour) % 6
    version_shares = traffic.version_shares(bucket) if traffic is not None else None
    for j in range(n_events):
        event_def = _EVENT_DEFS[_digest_int(seed, "ev_def", epoch_hour, j) % len(_EVENT_DEFS)]
        # Skipped rather than re-drawn from a shorter roster, so every other
        # identity keeps exactly the rows it had before it was retired.
        if event_def.retired:
            continue
        row = _event_row(seed, bucket, event_def, session_span, day_ordinal, epoch_hour, j)
        if traffic is not None and version_shares is not None:
            platform_shares = traffic.platform_shares(
                bucket, event_name=event_def.event_name, event_type=event_def.event_type
            )
            row["platform"] = _pick_share(platform_shares, seed, "sample_plat", epoch_hour, j)
            row["app_version"] = _pick_share(version_shares, seed, "sample_ver", epoch_hour, j)
        out.append(row)
    return out


def _epoch_hour(bucket: datetime) -> int:
    """The bucket's absolute UTC hour number since the epoch.

    The digest key for everything generated inside one hour. It must not be the
    hour's OFFSET from the anchor: the adapter is rebuilt with ``anchor=None`` on
    every scan, so an offset key made the same wall-clock hour hold different
    rows each time the clock moved on.
    """
    return int(bucket.timestamp()) // 3600


def _session_span(seed: int, day_ordinal: int) -> int:
    """Distinct sessions available on one UTC DAY, in a bounded band.

    Keyed on the UTC date ordinal, which is the unit the seeded active-sessions
    sql metric aggregates by (``toStartOfDay``). Keyed on a day INDEX counted from
    the anchor instead, the pool rolled over at the anchor's hour rather than at
    UTC midnight, so a single UTC day drew from two pools and its distinct-session
    count inflated by up to ~70% for an afternoon anchor — against a seeded
    history written at a different hour, and recollected hourly by the scheduler
    at whatever hour it fired.
    """
    return 25 + _digest_int(seed, "day_sessions", day_ordinal) % 20


def _generate_events(
    seed: int,
    anchor: datetime,
    history_days: int,
    max_rows: int,
    spike: SyntheticSpike | None = None,
    traffic: DemoTraffic | None = None,
) -> list[dict[str, object]]:
    """Deterministic hourly events over the last ``history_days`` before ``anchor``.

    The most-recent ``SYNTHETIC_ONGOING_HOURS`` hours carry each event's seeded
    ``ongoing_base`` volume (so a live scan's current window continues the demo's
    baseline); older hours keep the small sampled scale so the
    full-history dataset stays within ``max_rows``.

    The ongoing window is generated FIRST and is never truncated. It is the only
    part a live scan reads back, so spending the row budget on the old sampled
    tail and clipping the newest hours would read as a volume drop on every
    series — the exact failure the ongoing window exists to prevent.

    With a demo's ``traffic`` the ongoing hours are the demo's stored hours, and
    those keep the stored series' slow upward drift: a demo left running for
    many months outgrows the budget. Then it is the OLDEST ongoing hours that go,
    whole, never a slice of the newest.
    """
    start = anchor - timedelta(days=history_days)
    total_hours = history_days * 24
    ongoing_start_hour = max(0, total_hours - SYNTHETIC_ONGOING_HOURS)

    def hour_keys(offset: int) -> tuple[datetime, int, int]:
        """``(bucket, epoch_hour, day_ordinal)`` for the hour ``offset`` past start.

        The loops below still count hours FROM the start of the window, because
        that is what bounds the window; every digest key is derived here from the
        resulting absolute instant instead, so the same bucket is generated
        identically whichever anchor asked for it.
        """
        bucket = start + timedelta(hours=offset)
        return bucket, _epoch_hour(bucket), bucket.date().toordinal()

    ongoing_hours: list[list[dict[str, object]]] = []
    for hour in range(ongoing_start_hour, total_hours):
        bucket, epoch_hour, day_ordinal = hour_keys(hour)
        ongoing_hours.append(
            _ongoing_hour_rows(
                seed,
                bucket,
                epoch_hour,
                _session_span(seed, day_ordinal),
                day_ordinal,
                spike,
                traffic,
            )
        )
    if traffic is None:
        ongoing = [row for rows in ongoing_hours for row in rows][:max_rows]
    else:
        ongoing = _newest_hours_within(ongoing_hours, max_rows)

    older: list[dict[str, object]] = []
    older_budget = max_rows - len(ongoing)
    for hour in range(ongoing_start_hour):
        remaining = older_budget - len(older)
        if remaining <= 0:
            break
        bucket, epoch_hour, day_ordinal = hour_keys(hour)
        hour_rows = _sampled_hour_rows(
            seed, bucket, epoch_hour, _session_span(seed, day_ordinal), day_ordinal, traffic
        )
        older.extend(hour_rows[:remaining])
    return older + ongoing


def _newest_hours_within(
    hours: list[list[dict[str, object]]], max_rows: int
) -> list[dict[str, object]]:
    """The newest whole hours of ``hours`` (oldest first) that fit in ``max_rows``."""
    kept: list[list[dict[str, object]]] = []
    budget = max_rows
    for rows in reversed(hours):
        if len(rows) > budget:
            break
        kept.append(rows)
        budget -= len(rows)
    return [row for rows in reversed(kept) for row in rows]


def _generate_orders(
    seed: int, anchor: datetime, history_days: int, max_rows: int
) -> list[dict[str, object]]:
    """Deterministic orders over the UTC days ending at ``anchor``, exclusive.

    Two properties that used to be missing, and that the rest of the module
    already assumes:

    * **One horizon with the events table.** Days are whole UTC days and no order
      is stamped at or after the anchor. The days used to be measured from the
      anchor's HOUR and the newest one ran to ``anchor + 24h``, so orders sat up
      to a day in the FUTURE while ``_generate_events`` stopped at the last
      complete hour — the two tables disagreed about when
      "now" was, and the untimed ``get_full_breakdown`` path reported the future
      rows. The newest day is therefore a genuine partial day that fills up as
      the day goes on, exactly like the events table's newest hour.
    * **Absolute keying.** Every digest is keyed on the day's UTC date ordinal,
      not its offset from the anchor, so a given date yields the same orders for
      any anchor. Without it, each scan's freshly-built adapter
      re-rolled the amount, country and status of every historical order.
    """
    anchor_day = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    rows: list[dict[str, object]] = []
    for day_offset in range(history_days):
        day = anchor_day - timedelta(days=history_days - 1 - day_offset)
        day_ordinal = day.date().toordinal()
        n_orders = 5 + _digest_int(seed, "ord_count", day_ordinal) % 15
        for j in range(n_orders):
            hour = _digest_int(seed, "ord_hour", day_ordinal, j) % 24
            minute = _digest_int(seed, "ord_minute", day_ordinal, j) % 60
            created = day + timedelta(hours=hour, minutes=minute)
            # Drop BEFORE the budget check, so the cap counts rows that are
            # actually emitted rather than rows that were only considered.
            if created >= anchor:
                continue
            if len(rows) >= max_rows:
                return rows
            amount = 5.0 + (_digest_int(seed, "ord_amount", day_ordinal, j) % 19500) / 100.0
            rows.append(
                {
                    "created_at": created,
                    "amount": round(amount, 2),
                    "currency": str(_pick(_CURRENCIES, seed, "ord_cur", day_ordinal, j)),
                    "user_id": f"u{_digest_int(seed, 'ord_user', day_ordinal, j) % 500}",
                    "country": str(_pick(_COUNTRIES, seed, "ord_country", day_ordinal, j)),
                    "status": str(_pick(_ORDER_STATUSES, seed, "ord_status", day_ordinal, j)),
                }
            )
    return rows


class SyntheticAdapter(BaseAdapter):
    """Serves a bounded, deterministic in-memory dataset via the adapter contract."""

    def __init__(
        self,
        *,
        seed: int = DEFAULT_SEED,
        anchor: datetime | None = None,
        history_days: int = SYNTHETIC_HISTORY_DAYS,
        timeout_seconds: int | None = None,
        max_rows: int = SYNTHETIC_MAX_ROWS,
        spike: SyntheticSpike | None = None,
        traffic: DemoTraffic | None = None,
    ) -> None:
        self._seed = seed
        self._history_days = history_days
        self._max_rows = max_rows
        # ``timeout_seconds`` is accepted and IGNORED. The signature is shared
        # with the real adapters and ``registry._build_synthetic`` passes the
        # data source's effective timeout, but there is no wall clock to guard
        # here: the dataset is built in this constructor and every scan is a list
        # comprehension over at most ``max_rows`` dicts. The value used to be
        # stored in ``self._timeout_seconds`` and read by nothing, while the
        # module docstring advertised a wall-clock budget — a guard that does not
        # exist reads as one that does. Implementing a real
        # timer would be theatre; saying so is not.
        #
        # Anchor to the start of the current UTC HOUR by default. Events are
        # generated for the ``history_days`` window strictly BEFORE the anchor, so
        # the newest event bucket is ``anchor - 1h`` — which, with an hourly
        # anchor, is exactly the last COMPLETE hour a live scan evaluates (its
        # window ends at the half-open ``floor(now, interval)``). Flooring to the
        # start of the *day* instead — as this used to — left every hour of
        # "today" with no synthetic rows, so once a demo sat idle past midnight a
        # scan of the current window read 0 for every series and the detector
        # stamped a clamped z=-20 "drop to zero" on all of them.
        # Because the adapter is rebuilt per scan with ``anchor=None`` (see
        # registry._build_synthetic), the dataset now always advances to the
        # current hour. Deterministic within the hour; tests pass an explicit
        # anchor for exactness (a midnight anchor floors identically either way).
        base = to_utc(anchor) if anchor is not None else datetime.now(UTC)
        self._anchor = base.replace(minute=0, second=0, microsecond=0)
        self._events = _generate_events(seed, self._anchor, history_days, max_rows, spike, traffic)
        self._orders = _generate_orders(seed, self._anchor, history_days, max_rows)
        self._enforce_budget("events", self._events)
        self._enforce_budget("orders", self._orders)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        # Nothing to release: no socket, no file, no cursor.
        return None

    def test_connection(self) -> bool:
        # Honest LOCAL check: BOTH in-memory tables hold rows. No host is
        # contacted and no real warehouse success is reported. This used to read
        # ``len(self._events) >= 0 and len(self._orders) >= 0``, which is true of
        # any list — a connection test that cannot fail tells the operator
        # nothing. Non-empty can fail: ``history_days=0``, or a
        # future generator change that stops emitting a table, both make the
        # source report a problem instead of a green tick over no data.
        return bool(self._events) and bool(self._orders)

    # -- schema / preview --------------------------------------------------

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        table = self._table_for_query(base_query)
        return [
            ColumnInfo(name=name, type_name=type_name, is_nullable=name in _EVENTS_NULLABLE)
            for name, type_name in self._columns_for_query(base_query, table)
        ]

    def get_schema_tables(self) -> list[SchemaTable]:
        return [
            SchemaTable(
                name="events",
                columns=[SchemaColumn(name=n, data_type=t) for n, t in _EVENTS_COLUMNS],
            ),
            SchemaTable(
                name="orders",
                columns=[SchemaColumn(name=n, data_type=t) for n, t in _ORDERS_COLUMNS],
            ),
        ]

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        if self._is_active_sessions_query(base_query):
            return self._active_sessions_rows(time_from, time_to, limit)
        self._reject_unsupported_scan(base_query)

        table = self._table_for_query(base_query)
        column_names = [name for name, _ in self._columns_for_query(base_query, table)]
        if time_column is not None:
            self._validate_column(table, time_column)
        rows = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        order_key = time_column or column_names[0]
        rows = sorted(rows, key=lambda row: _bval(row.get(order_key)))
        capped = rows[: max(int(limit), 0)]
        out = [tuple(row.get(name) for name in column_names) for row in capped]
        return column_names, out

    # -- full (untimed) breakdown -----------------------------------------

    def get_full_breakdown(
        self,
        base_query: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None = None,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        limit: int = 50000,
    ) -> tuple[list[str], list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        table = self._table_for_query(base_query)
        reg = [self._validate_column(table, column) for column in regular_columns]
        if time_column is not None:
            self._validate_column(table, time_column)
        windowed = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        counts: dict[tuple[object, ...], int] = {}
        for row in windowed:
            key = tuple(row.get(column) for column in reg)
            counts[key] = counts.get(key, 0) + 1
        items = sorted(counts.items(), key=lambda kv: (-kv[1], tuple(_bval(v) for v in kv[0])))
        out = [(*key, count) for key, count in items[: max(int(limit), 0)]]
        return reg, [], [], out

    # -- time-bucketed counts ---------------------------------------------

    def get_time_bucketed_counts(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        table = self._table_for_query(base_query)
        reg = [self._validate_column(table, column) for column in regular_columns]
        self._validate_column(table, time_column)
        groups = self._bucket_groups(
            base_query, table, time_column, interval, reg, time_from, time_to
        )
        out: list[tuple[object, ...]] = []
        for (bucket, *values), members in self._sorted_items(groups):
            out.append((bucket, *values, len(members)))
        return reg, [], out[: max(int(limit), 0)]

    def get_time_bucketed_breakdown_counts(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        self._reject_property_breakdowns([breakdown_column])
        table = self._table_for_query(base_query)
        reg = [self._validate_column(table, column) for column in regular_columns]
        self._validate_column(table, time_column)
        breakdown = self._validate_column(table, breakdown_column)
        windowed = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        top = self._ranked_top_values(
            base_query, table, time_column, windowed, breakdown, values_limit
        )
        groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
        for row in windowed:
            bucket = self._bucket_start(row[time_column], interval)
            value, is_other = self._fold(top, _bval(row.get(breakdown)))
            key = (bucket, value, is_other, *tuple(row.get(column) for column in reg))
            groups.setdefault(key, []).append(row)
        out: list[tuple[object, ...]] = []
        for key, members in self._sorted_breakdown_items(groups):
            k_bucket, k_value, k_is_other, *k_values = key
            out.append((k_bucket, k_value, k_is_other, *k_values, len(members)))
        return reg, [], out[: max(int(limit), 0)]

    def get_time_bucketed_breakdown_counts_multi(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_columns: list[str],
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        self._reject_property_breakdowns(breakdown_columns)
        if not breakdown_columns:
            return [], [], []
        table = self._table_for_query(base_query)
        reg = [self._validate_column(table, column) for column in regular_columns]
        self._validate_column(table, time_column)
        breakdowns = [self._validate_column(table, column) for column in breakdown_columns]
        windowed = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        out: list[tuple[object, ...]] = []
        for breakdown in breakdowns:
            top = self._ranked_top_values(
                base_query, table, time_column, windowed, breakdown, values_limit
            )
            groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
            for row in windowed:
                bucket = self._bucket_start(row[time_column], interval)
                value, is_other = self._fold(top, _bval(row.get(breakdown)))
                key = (bucket, value, is_other, *tuple(row.get(column) for column in reg))
                groups.setdefault(key, []).append(row)
            for key, members in self._sorted_breakdown_items(groups):
                k_bucket, k_value, k_is_other, *k_values = key
                out.append((k_bucket, breakdown, k_value, k_is_other, *k_values, len(members)))
        out.sort(key=lambda row: (row[0], row[1], row[2]))
        return reg, [], out[: max(int(limit), 0)]

    # -- time-bucketed aggregates -----------------------------------------

    def get_time_bucketed_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        table = self._table_for_query(base_query)
        reg = [self._validate_column(table, column) for column in regular_columns]
        self._validate_column(table, time_column)
        measure = self._validate_measure(table, agg_fn, measure_column)
        groups = self._bucket_groups(
            base_query, table, time_column, interval, reg, time_from, time_to
        )
        out: list[tuple[object, ...]] = []
        for (bucket, *values), members in self._sorted_items(groups):
            out.append((bucket, *values, self._aggregate(members, agg_fn, measure)))
        return reg, [], out[: max(int(limit), 0)]

    def get_time_bucketed_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        agg_fn: MetricAggregation,
        measure_column: str | None,
        breakdown_column: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        self._reject_json(json_columns)
        table = self._table_for_query(base_query)
        reg = [self._validate_column(table, column) for column in regular_columns]
        self._validate_column(table, time_column)
        breakdown = self._validate_column(table, breakdown_column)
        measure = self._validate_measure(table, agg_fn, measure_column)
        windowed = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        top = self._ranked_top_values(
            base_query, table, time_column, windowed, breakdown, values_limit
        )
        groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
        for row in windowed:
            bucket = self._bucket_start(row[time_column], interval)
            value, is_other = self._fold(top, _bval(row.get(breakdown)))
            # The breakdown's own regular-column slot carries the FOLDED value,
            # so it adds nothing to the group key: see
            # BaseAdapter.get_time_bucketed_aggregate_breakdown for why the raw
            # value may not be part of it. The three SQL adapters make the same
            # substitution and are guaranteed to make it, because they reject a
            # breakdown that is not also in ``regular_columns``; this adapter
            # does not, so here ``reg`` may simply never contain it.
            key = (
                bucket,
                value,
                is_other,
                *tuple(value if column == breakdown else row.get(column) for column in reg),
            )
            groups.setdefault(key, []).append(row)
        out: list[tuple[object, ...]] = []
        for key, members in self._sorted_breakdown_items(groups):
            k_bucket, k_value, k_is_other, *k_values = key
            aggregate = self._aggregate(members, agg_fn, measure)
            out.append((k_bucket, k_value, k_is_other, *k_values, aggregate))
        return reg, [], out[: max(int(limit), 0)]

    def get_time_bucketed_multi_aggregate(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        table = self._table_for_query(base_query)
        self._validate_column(table, time_column)
        windowed = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        buckets: dict[datetime, list[dict[str, object]]] = {}
        for row in windowed:
            buckets.setdefault(self._bucket_start(row[time_column], interval), []).append(row)
        column_names = ["bucket", *[spec.key for spec in specs]]
        out: list[tuple[object, ...]] = []
        for bucket in sorted(buckets):
            members = buckets[bucket]
            out.append((bucket, *[self._spec_value(table, spec, members) for spec in specs]))
        return column_names, out[: max(int(limit), 0)]

    def get_time_bucketed_multi_aggregate_breakdown(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        breakdown_column: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        values_limit: int | None = None,
        limit: int = 100000,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        table = self._table_for_query(base_query)
        self._validate_column(table, time_column)
        breakdown = self._validate_column(table, breakdown_column)
        windowed = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        top = self._ranked_top_values(
            base_query, table, time_column, windowed, breakdown, values_limit
        )
        groups: dict[tuple[datetime, str, int], list[dict[str, object]]] = {}
        for row in windowed:
            bucket = self._bucket_start(row[time_column], interval)
            value, is_other = self._fold(top, _bval(row.get(breakdown)))
            groups.setdefault((bucket, value, is_other), []).append(row)
        column_names = ["bucket", "breakdown_value", "is_other", *[spec.key for spec in specs]]
        out: list[tuple[object, ...]] = []
        for (bucket, value, is_other), members in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][1])
        ):
            spec_values = [self._spec_value(table, spec, members) for spec in specs]
            out.append((bucket, value, is_other, *spec_values))
        return column_names, out[: max(int(limit), 0)]

    # -- internals ---------------------------------------------------------

    def _columns_for_table(self, table: str) -> tuple[tuple[str, str], ...]:
        return _ORDERS_COLUMNS if table == "orders" else _EVENTS_COLUMNS

    def _columns_for_query(self, base_query: str, table: str) -> tuple[tuple[str, str], ...]:
        """Columns the query actually projects, in table order.

        An unrecognized or ``*`` projection — and one that names no known column
        — yields the full table, so the adapter never silently returns nothing.
        """
        columns = self._columns_for_table(table)
        projection = _projection_columns(base_query)
        if projection is None:
            return columns
        selected = {name.casefold() for name in projection}
        projected = tuple(column for column in columns if column[0].casefold() in selected)
        return projected or columns

    def _rows_for_table(self, table: str) -> list[dict[str, object]]:
        return self._orders if table == "orders" else self._events

    def _scan_rows(self, base_query: str, table: str) -> list[dict[str, object]]:
        """The table's rows, narrowed by a top-level ``WHERE`` in ``base_query``.

        The single seam through which every TABLE-SCAN method reads the dataset,
        so the two ways the metric collector delivers a row filter produce the
        same rows — including the fact table's OWN ``WHERE``, which the
        per-metric wrapper puts one paren level down (``_where_predicates``).
        (``_active_sessions_rows`` reads ``self._events`` directly and
        does not come through here: it serves an exactly-recognized statement,
        which by definition has no WHERE to honour.)

        The batched path sends its combined WHERE as ``AggregateSpec.filter_sql``
        (evaluated in ``_spec_value``); the per-metric path instead WRAPS the
        fact SQL —
        ``_fact_conditions._resolve_fact_operand_query`` returns ``SELECT * FROM
        (<source>) AS _filtered WHERE <combined>`` — and this adapter used to
        throw that clause away and aggregate the whole table. On the demo's own
        seeded ``status = 'completed'`` filter the two paths disagreed by about
        2x, directly contradicting the invariant ``metric_collect`` states for the
        batched path ("The per-bucket VALUES are identical to the per-metric
        path"), and the fact-operand dry run counted every row in the window.

        A predicate the evaluator cannot read raises ``SyntheticCapabilityError``
        — the documented outcome, and the honest one. Refusing every top-level
        WHERE instead would be cheaper and equally truthful, but it breaks the
        demo's own "Collect now" and filter preview, turning a silent wrong number
        into a visibly broken feature.
        """
        rows = self._rows_for_table(table)
        predicates = _where_predicates(base_query)
        if not predicates:
            return rows
        return [
            row
            for row in rows
            if all(self._row_matches_filter(table, row, predicate) for predicate in predicates)
        ]

    def _table_for_query(self, base_query: str) -> str:
        # Table selection is one of the three things parsed out of base_query: a
        # query mentioning ``orders`` reads orders, everything else reads events.
        # The search runs over the literal mask so a VALUE that happens to contain
        # the word (``WHERE product_id = 'orders'``) cannot switch the table out
        # from under an events query.
        if re.search(r"\borders\b", _mask_string_literals(base_query), re.IGNORECASE):
            return "orders"
        return "events"

    def _validate_column(self, table: str, name: str) -> str:
        """Hold a STRUCTURED column parameter to the table's columns.

        Raises ``ValueError``, which is this module's contract for a parameter the
        CALLER supplied positionally (a time / measure / breakdown column). A name
        parsed out of free-text SQL goes through ``_filter_column`` instead and
        raises the capability error, because there the adapter is declining to
        read something, not rejecting an argument.
        """
        if not _IDENT_RE.match(name):
            msg = f"Invalid column name: {name!r}"
            raise ValueError(msg)
        if name not in _table_columns(table):
            msg = f"Column {name!r} not found in {table} query result"
            raise ValueError(msg)
        return name

    def _filter_column(self, table: str, raw: str) -> str:
        """Resolve a column named inside a WHERE fragment, or refuse.

        Strips the dialect identifier quoting the collector emits, then answers to
        the SAME allowlist ``_validate_column`` uses. Without the membership check
        an unknown name simply read as ``None`` on every row, so the filter
        matched nothing and the metric collected NULL for every bucket with no
        error anywhere.
        """
        name = _filter_identifier(raw)
        if name not in _table_columns(table):
            msg = f"Unsupported filter column: {raw!r}"
            raise SyntheticCapabilityError(msg)
        return name

    def _validate_measure(
        self, table: str, agg_fn: MetricAggregation, measure_column: str | None
    ) -> str | None:
        if requires_measure(agg_fn):
            if measure_column is None:
                msg = f"aggregation {coerce_aggregation(agg_fn).value!r} requires a measure column"
                raise ValueError(msg)
            return self._validate_column(table, measure_column)
        # ``count`` ignores any measure column.
        return None

    def _reject_json(self, json_columns: list[str]) -> None:
        if json_columns:
            msg = "The synthetic warehouse has no JSON columns"
            raise SyntheticCapabilityError(msg)

    def _reject_property_breakdowns(self, breakdown_columns: list[str]) -> None:
        """Refuse a property breakdown (``<json_column>.<path>``, F23 #306) by name.

        The SQL adapters extract one from its JSON column; this warehouse has no
        JSON column to extract it from, and ``_validate_column`` would otherwise
        report the entry as a column missing from the table.
        """
        properties = [column for column in breakdown_columns if is_property_field(column)]
        if properties:
            msg = (
                "The synthetic warehouse has no JSON columns, so it cannot break down "
                f"by the properties {', '.join(properties)}"
            )
            raise SyntheticCapabilityError(msg)

    def _windowed_rows(
        self,
        rows: list[dict[str, object]],
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
    ) -> list[dict[str, object]]:
        if time_column is None or (time_from is None and time_to is None):
            return list(rows)
        # Half-open ``[from, to)`` in UTC, per tripl.core.bucketing: a row exactly
        # on ``time_to`` belongs to the NEXT window, so adjacent windows tile
        # without double-counting a boundary row. ``to_utc`` normalizes both the
        # bounds and the row so a naive datetime is read as UTC rather than in the
        # host's local zone.
        lower = to_utc(time_from) if time_from is not None else None
        upper = to_utc(time_to) if time_to is not None else None
        out: list[dict[str, object]] = []
        for row in rows:
            raw = row.get(time_column)
            if not isinstance(raw, datetime):
                continue
            moment = to_utc(raw)
            if lower is not None and moment < lower:
                continue
            if upper is not None and moment >= upper:
                continue
            out.append(row)
        return out

    def _enforce_budget(self, table: str, rows: list[dict[str, object]]) -> None:
        """Hold one generated table to the row budget, at CONSTRUCTION time.

        This used to run on every scan, against ``self._events`` /
        ``self._orders`` — the very lists the generators had already capped at
        ``max_rows`` — so the branch was unreachable and read as a guard while
        guarding nothing. Checking the generators' OUTPUT once,
        here, is the assertion that can actually fire: it catches a future change
        to ``_generate_events`` / ``_generate_orders`` that stops respecting the
        cap, which is the failure mode that matters, because
        ``_generate_events`` fills oldest-first and a cap that bites silently
        drops the NEWEST hours a live scan reads.
        """
        if len(rows) > self._max_rows:
            msg = f"Synthetic {table} dataset exceeded the row budget"
            raise SyntheticCapabilityError(msg)

    def _bucket_groups(
        self,
        base_query: str,
        table: str,
        time_column: str,
        interval: str,
        regular_columns: list[str],
        time_from: datetime,
        time_to: datetime,
    ) -> dict[tuple[object, ...], list[dict[str, object]]]:
        windowed = self._windowed_rows(
            self._scan_rows(base_query, table), time_column, time_from, time_to
        )
        groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
        for row in windowed:
            bucket = self._bucket_start(row[time_column], interval)
            key = (bucket, *tuple(row.get(column) for column in regular_columns))
            groups.setdefault(key, []).append(row)
        return groups

    def _sorted_items(
        self, groups: dict[tuple[object, ...], list[dict[str, object]]]
    ) -> list[tuple[tuple[object, ...], list[dict[str, object]]]]:
        return sorted(
            groups.items(),
            key=lambda kv: (kv[0][0], tuple(_bval(v) for v in kv[0][1:])),
        )

    def _sorted_breakdown_items(
        self, groups: dict[tuple[object, ...], list[dict[str, object]]]
    ) -> list[tuple[tuple[object, ...], list[dict[str, object]]]]:
        # Order by bucket, breakdown_value, then remaining regular columns.
        return sorted(
            groups.items(),
            key=lambda kv: (kv[0][0], _bval(kv[0][1]), tuple(_bval(v) for v in kv[0][3:])),
        )

    def _ranked_top_values(
        self,
        base_query: str,
        table: str,
        time_column: str,
        windowed: list[dict[str, object]],
        breakdown: str,
        values_limit: int | None,
    ) -> set[str] | None:
        """``_top_values`` ranked over the caller's whole window when one is set.

        Inside ``top_n_ranking_window`` the ranking reads the rows of THAT window,
        once per (query, column, limit, window), so every chunk of one collection folds
        the same values into ``'Other'``. Outside it, the rows
        this call already windowed are ranked, as before.
        """
        window = self._top_n_ranking_window
        if window is None:
            return self._top_values(windowed, breakdown, values_limit)
        rank_from, rank_to = window
        return self._ranked_once(
            ("top_values", base_query, time_column, breakdown, values_limit, rank_from, rank_to),
            lambda: self._top_values(
                self._windowed_rows(
                    self._scan_rows(base_query, table), time_column, rank_from, rank_to
                ),
                breakdown,
                values_limit,
            ),
        )

    def _top_values(
        self, windowed: list[dict[str, object]], breakdown: str, values_limit: int | None
    ) -> set[str] | None:
        """Top ``values_limit - 1`` breakdown values by row count, or ``None``.

        ``None`` (no limit) means every value is kept and folds to ``is_other=0``.
        The tie-break in ``(-count, value)`` below is the BaseAdapter top-N
        contract rather than a local convenience: the three SQL adapters rank on
        the same key pair — count descending, then the value ascending — in
        their top-values pre-query, so the demo warehouse and a real one keep
        the same values when counts tie. Python compares ``str`` by code point,
        which is what each engine's value sort resolves to.
        """
        if values_limit is None:
            return None
        top_count = max(values_limit - 1, 0)
        counts: dict[str, int] = {}
        for row in windowed:
            value = _bval(row.get(breakdown))
            counts[value] = counts.get(value, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return {value for value, _ in ranked[:top_count]}

    def _fold(self, top: set[str] | None, value: str) -> tuple[str, int]:
        if top is None or value in top:
            return value, 0
        return "Other", 1

    def _aggregate(
        self, members: list[dict[str, object]], agg_fn: MetricAggregation, measure: str | None
    ) -> object:
        agg = coerce_aggregation(agg_fn)
        if agg is MetricAggregation.count:
            return len(members)
        if measure is None:
            msg = f"aggregation {agg.value!r} requires a measure column"
            raise ValueError(msg)
        raw = [member.get(measure) for member in members]
        if agg is MetricAggregation.count_distinct:
            return len({value for value in raw if value is not None})
        present = [float(value) for value in raw if value is not None]  # type: ignore[arg-type]
        if not present:
            # NULL for ``sum`` too, not ``0.0``: standard SQL ``sum`` over an input
            # holding no non-NULL value is NULL, and all three SQL adapters hand
            # that back untouched, so on them such a bucket is a GAP in the stored
            # series. This used to answer ``0.0`` for ``sum`` alone, which made the
            # demo warehouse store a zero where every real engine stores nothing,
            # contradicting the conditional-aggregate contract on ``BaseAdapter``.
            # Only ``count`` / ``count_distinct`` — handled
            # above — are 0 over such a set.
            return None
        if agg is MetricAggregation.sum:
            return float(sum(present))
        if agg is MetricAggregation.avg:
            return sum(present) / len(present)
        if agg is MetricAggregation.min:
            return min(present)
        return max(present)

    def _spec_value(
        self, table: str, spec: AggregateSpec, members: list[dict[str, object]]
    ) -> object:
        measure = self._validate_column(table, spec.column) if spec.column is not None else None
        if spec.filter_sql:
            matching = [
                row for row in members if self._row_matches_filter(table, row, spec.filter_sql)
            ]
            if not matching:
                # The row-presence gate every adapter owes (the conditional-aggregate
                # contract on BaseAdapter), spelled here as the question the SQL
                # engines have to ask with a second aggregate — ClickHouse
                # ``countIf(cond)``, PostgreSQL ``count(*) FILTER (WHERE cond)``,
                # BigQuery ``COUNTIF(cond)``: a bucket with rows but none matching
                # reads as absent (NULL). Below it, a matching set whose measure is
                # NULL throughout still aggregates — to 0 for a distinct count — and
                # that 0 is a data point rather than a gap.
                return None
            return self._aggregate(matching, spec.aggregation, measure)
        return self._aggregate(members, spec.aggregation, measure)

    # -- interval bucketing ------------------------------------------------

    def _bucket_start(self, value: object, interval_code: str) -> datetime:
        """Floor a row's timestamp to its bucket, per the shared contract.

        This adapter is what the conformance fixtures compare the real warehouses
        against, so it must not carry its own opinion about bucketing. It used to
        hand-roll the interval math (parsing ClickHouse-style ``"1 hour"`` strings
        and re-deriving the epoch/Monday origins), which is exactly the second
        implementation that drifts. There is now one definition —
        :func:`tripl.core.bucketing.floor_to_bucket` — and this delegates to it.

        Unknown interval codes raise ``ValueError`` from ``get_interval``, and the
        old ``month`` unit is gone with the rest of the math: it was never a valid
        interval code.
        """
        if not isinstance(value, datetime):
            msg = f"Cannot bucket non-datetime value: {value!r}"
            raise ValueError(msg)
        return floor_to_bucket(value, interval_code)

    # -- sql-metric support ------------------------------------------------

    def _is_active_sessions_query(self, base_query: str) -> bool:
        # Exact membership in a curated set, NOT a substring probe — see
        # ``_ACTIVE_SESSIONS_STATEMENTS`` for why that distinction is the whole
        # finding. Anything else falls through to ``_reject_unsupported_scan``,
        # whose ``_NON_SCAN_RE`` already matches ``count(`` and ``distinct``, so
        # an edited active-sessions query lands on the documented capability
        # error with no further work here.
        return _normalize_sql(base_query) in _ACTIVE_SESSIONS_STATEMENTS

    def _reject_unsupported_scan(self, base_query: str) -> None:
        # A plain table scan (``SELECT * FROM events`` / an explicit column list)
        # is fine. Anything with aggregation/join/DISTINCT that we did not
        # recognize as a known sql-metric shape is a capability boundary.
        if _NON_SCAN_RE.search(base_query):
            msg = (
                "The synthetic warehouse only supports plain table scans and the "
                "seeded sql-metric queries; this query is not supported"
            )
            raise SyntheticCapabilityError(msg)

    def _active_sessions_rows(
        self, time_from: datetime | None, time_to: datetime | None, limit: int
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Distinct sessions per day — the seeded ``active_sessions`` sql metric.

        The window is applied to the output day bucket ``ts`` (mirroring how the
        real query's ``ts`` alias is filtered), and the projection is
        ``(ts, value)`` so the sql-metric collector reads it back unchanged.
        """
        sessions_by_day: dict[datetime, set[object]] = {}
        for row in self._events:
            day = self._bucket_start(row["event_time"], _DAY_INTERVAL)
            sessions_by_day.setdefault(day, set()).add(row["session_id"])
        lower = to_utc(time_from) if time_from is not None else None
        upper = to_utc(time_to) if time_to is not None else None
        rows: list[tuple[object, ...]] = []
        for day in sorted(sessions_by_day):
            if lower is not None and day < lower:
                continue
            if upper is not None and day >= upper:
                continue
            rows.append((day, len(sessions_by_day[day])))
        return ["ts", "value"], rows[: max(int(limit), 0)]

    def _row_matches_filter(self, table: str, row: dict[str, object], filter_sql: str) -> bool:
        """Evaluate a simple, safe WHERE fragment against one row of ``table``.

        Supports comparisons (``=``/``!=``/``<>``/``>``/``>=``/``<``/``<=``) of a
        column against a quoted-string or numeric literal, combined with ``AND`` /
        ``OR``, plus the *fully-parenthesised* fragments the metric collector
        emits: each named / free-text row filter is wrapped in parentheses and
        ANDed (e.g. ``(status = 'completed')`` or ``(amount > 0) AND (amount >
        100)``), so this evaluator strips boolean grouping parentheses and splits
        on top-level ``AND`` / ``OR`` at parenthesis depth zero. A parenthesis
        that is NOT boolean grouping (e.g. a subquery ``IN (SELECT ...)`` or a
        function call) survives into an atom and still raises a capability error —
        the adapter never guesses at a filter it cannot faithfully evaluate.

        ``table`` is needed because a column name is now RESOLVED rather than
        looked up hopefully: it is unquoted from whichever dialect spelling it
        arrived in and then held to that table's columns, so a name this warehouse
        does not have is refused instead of silently reading as ``NULL`` on every
        row.

        Every split below runs over ``_mask_string_literals``, so nothing inside a
        value can be mistaken for structure: ``country = 'Trinidad and Tobago'`` is
        one atom, not two.
        """
        return self._eval_filter(table, row, filter_sql.strip())

    def _eval_filter(self, table: str, row: dict[str, object], expression: str) -> bool:
        expression = self._strip_wrapping_parens(expression.strip())
        or_parts = self._split_top_level(expression, "or")
        if len(or_parts) > 1:
            return any(self._eval_filter(table, row, part) for part in or_parts)
        and_parts = self._split_top_level(expression, "and")
        if len(and_parts) > 1:
            return all(self._eval_filter(table, row, part) for part in and_parts)
        return self._atom_matches(table, row, expression)

    def _strip_wrapping_parens(self, expression: str) -> str:
        """Strip balanced parentheses that enclose the WHOLE expression.

        Only a paren pair whose opening bracket at index 0 closes at the final
        index is removed (repeatedly). ``(a) AND (b)`` is left untouched because
        its first ``(`` closes mid-string, so only true grouping wrappers like
        ``((status = 'x'))`` collapse.
        """
        while len(expression) >= 2 and expression[0] == "(" and expression[-1] == ")":
            masked = _mask_string_literals(expression)
            depth = 0
            wraps_whole = True
            for index, char in enumerate(masked):
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0 and index != len(masked) - 1:
                        wraps_whole = False
                        break
            if not wraps_whole:
                break
            expression = expression[1:-1].strip()
        return expression

    def _split_top_level(self, expression: str, keyword: str) -> list[str]:
        """Split ``expression`` on a whole-word ``keyword`` at paren depth zero."""
        parts: list[str] = []
        masked = _mask_string_literals(expression)
        lowered = masked.lower()
        depth = 0
        start = 0
        index = 0
        length = len(masked)
        klen = len(keyword)
        while index < length:
            char = masked[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif (
                depth == 0
                and lowered.startswith(keyword, index)
                and _has_word_boundaries(masked, index, index + klen)
            ):
                parts.append(expression[start:index])
                index += klen
                start = index
                continue
            index += 1
        parts.append(expression[start:])
        return [part.strip() for part in parts if part.strip()]

    def _atom_matches(self, table: str, row: dict[str, object], atom: str) -> bool:
        atom = self._strip_wrapping_parens(atom.strip())
        left, operator, right = _split_comparison(atom)
        return self._compare(row, self._filter_column(table, left), operator, right)

    def _compare(self, row: dict[str, object], column: str, operator: str, literal: str) -> bool:
        """Compare one row's ``column`` against a literal.

        The literal's SHAPE is validated before the row's value is looked at, so
        whether a fragment is refused depends only on the fragment — never on
        which row happened to be evaluated first.

        A ``NULL`` value makes every comparison false, including ``!=``. That is
        SQL's three-valued logic (``status != 'x'`` is NULL, not TRUE, for a NULL
        ``status``) and it is what the numeric branch already did; the string
        branch rendered NULL as ``''`` through ``_bval``, so ``button_id !=
        'share'`` matched every row that carried no button at all — and four of
        the events columns are nullable (``_EVENTS_NULLABLE``).
        """
        value = row.get(column)
        if literal.startswith("'"):
            expected = _decode_string_literal(literal)
            if operator not in ("=", "!=", "<>"):
                msg = f"Unsupported string comparison: {operator!r}"
                raise SyntheticCapabilityError(msg)
            if value is None:
                return False
            actual = _bval(value)
            return actual == expected if operator == "=" else actual != expected
        try:
            number = float(literal)
        except ValueError as exc:
            msg = f"Unsupported filter literal: {literal!r}"
            raise SyntheticCapabilityError(msg) from exc
        if value is None:
            return False
        try:
            actual_number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            # A numeric literal compared against a text column. Refusing names the
            # column; letting float() raise reached the user as "Scan failed due
            # to an internal error." with nothing in it to act on.
            msg = f"Cannot compare column {column!r} to the numeric literal {literal!r}"
            raise SyntheticCapabilityError(msg) from exc
        if operator == "=":
            return actual_number == number
        if operator in ("!=", "<>"):
            return actual_number != number
        if operator == ">":
            return actual_number > number
        if operator == ">=":
            return actual_number >= number
        if operator == "<":
            return actual_number < number
        return actual_number <= number
