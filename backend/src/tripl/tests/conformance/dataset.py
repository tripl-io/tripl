"""The one canonical fixture dataset every warehouse conformance gate seeds.

One dataset, three warehouses: the same rows are loaded into PostgreSQL and
ClickHouse and rendered as a table-less GoogleSQL literal for BigQuery, so a
divergence between two adapters is a real divergence and not a fixture artifact.

The timestamps are not arbitrary. They are chosen so that a *timezone-shifted*
window returns the SAME NUMBER OF ROWS as the correct one, but a DIFFERENT SET of
rows — the exact shape of the bug this suite exists to catch. A gate that only
compared counts would have passed against the broken code.

This file holds TWO rowsets. ``ROWS`` (below) is the scan/bucket trap the adapter
gates run on. ``PIPELINE_ROWS`` (further down) is the time SERIES the pipeline gate
runs on — scan, event generation, event metrics, fact metrics and drift need a
baseline plus a spike, which nine rows cannot carry. Both are seeded into both
executing warehouses from the same place, and both derive every expectation from
``floor_to_bucket``, so neither can drift from the product's own bucketing.

Concretely, the correct window is ``[2026-04-02T00:00Z, 2026-04-09T00:00Z)`` and
selects ids 1-7 (seven rows). Reading the window bounds as ``Asia/Kolkata``
wall-clock (UTC+05:30) — which is what an offset-less SQL literal does on a
non-UTC server — slides it to ``[2026-04-01T18:30Z, 2026-04-08T18:30Z)``, which
drops id 7 and picks up id 9: still seven rows, a different seven. Hence every
timezone assertion here compares row MEMBERSHIP, never row count.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tripl.core.adapters.base import FieldContractExpectation
from tripl.core.bucketing import floor_to_bucket
from tripl.json_paths import flatten_json_paths

#: Half-open scan window: ``time_from <= t < time_to``. A Thursday to a Thursday,
#: so it straddles a Monday week boundary and 1w bucketing is actually exercised.
FROM_TIME = datetime(2026, 4, 2, 0, 0, 0, tzinfo=UTC)
TO_TIME = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)

#: Every interval the product offers. All five are asserted against
#: ``floor_to_bucket`` on every executing warehouse.
INTERVALS = ("15m", "1h", "6h", "1d", "1w")


@dataclass(frozen=True)
class FixtureRow:
    id: int
    ts: datetime
    event_name: str
    amount: float | None
    user_id: str
    doc: dict[str, object]


ROWS: tuple[FixtureRow, ...] = (
    # Exactly ON time_from. Half-open windows include it.
    FixtureRow(
        id=1,
        ts=datetime(2026, 4, 2, 0, 0, 0, 0, tzinfo=UTC),
        event_name="click",
        amount=1.5,
        user_id="u1",
        doc={
            "user": {"id": 7, "address": {"city": "Berlin", "zip": None}},
            "tags": ["a", "b"],
            # An empty object has no leaf, so it is NOT a discoverable path.
            "empty": {},
        },
    ),
    # The last microsecond of the first 15m bucket — shares bucket 1 with id 1.
    FixtureRow(
        id=2,
        ts=datetime(2026, 4, 2, 0, 14, 59, 999999, tzinfo=UTC),
        event_name="click",
        amount=2.5,
        user_id="u1",
        doc={"user": {"id": 8, "address": {"city": "Berlin"}}, "tags": []},
    ),
    # The first microsecond of the second 15m bucket.
    FixtureRow(
        id=3,
        ts=datetime(2026, 4, 2, 0, 15, 0, 0, tzinfo=UTC),
        event_name="view",
        amount=None,
        user_id="u2",
        doc={"user": {"id": 9, "address": {"city": "Paris"}}, "flag": True},
    ),
    # 05:30Z. In Asia/Tokyo this is 14:30 the same day; in Asia/Kolkata it is
    # exactly the +05:30 offset, so a shifted grid lands it on a bucket edge.
    FixtureRow(
        id=4,
        ts=datetime(2026, 4, 2, 5, 30, 0, 0, tzinfo=UTC),
        event_name="view",
        amount=10.0,
        user_id="u3",
        doc={"user": {"id": 10, "address": {"city": "Tokyo"}}, "tags": ["c"]},
    ),
    # Sunday 23:59:59 — the last second of the week that STARTED Mon 2026-03-30.
    FixtureRow(
        id=5,
        ts=datetime(2026, 4, 5, 23, 59, 59, 0, tzinfo=UTC),
        event_name="buy",
        amount=99.0,
        user_id="u2",
        doc={"user": {"id": 11, "address": {"city": "Lisbon"}}},
    ),
    # Monday 00:00:00 — the first instant of the week that STARTS Mon 2026-04-06.
    FixtureRow(
        id=6,
        ts=datetime(2026, 4, 6, 0, 0, 0, 0, tzinfo=UTC),
        event_name="buy",
        amount=5.0,
        user_id="u4",
        doc={},
    ),
    # 18:45Z. A window slid +05:30 pushes its upper bound back to 18:30Z and
    # DROPS this row — while picking up id 9 below, keeping the count at seven.
    FixtureRow(
        id=7,
        ts=datetime(2026, 4, 8, 18, 45, 0, 0, tzinfo=UTC),
        event_name="click",
        amount=7.0,
        user_id="u4",
        doc={"user": {"id": 12, "address": {"city": "Oslo"}}, "note": None},
    ),
    # Exactly ON time_to. Half-open windows EXCLUDE it.
    FixtureRow(
        id=8,
        ts=datetime(2026, 4, 9, 0, 0, 0, 0, tzinfo=UTC),
        event_name="click",
        amount=1.0,
        user_id="u9",
        doc={"user": {"id": 98, "address": {"city": "OutAfter"}}},
    ),
    # One microsecond BEFORE time_from. Excluded — unless the window slides.
    FixtureRow(
        id=9,
        ts=datetime(2026, 4, 1, 23, 59, 59, 999999, tzinfo=UTC),
        event_name="click",
        amount=1.0,
        user_id="u9",
        doc={"user": {"id": 99, "address": {"city": "OutBefore"}}},
    ),
)

#: The rows a correct half-open UTC window selects. Ids, not a count: the whole
#: point of this fixture is that the count alone cannot tell you it is right.
IN_WINDOW_IDS = frozenset(row.id for row in ROWS if FROM_TIME <= row.ts < TO_TIME)

#: What a window whose bounds are mis-read as local wall clock selects instead.
#:
#: Both hostile zones this suite uses land on the same set, which is convenient and
#: not a coincidence — any eastward shift large enough to cross 18:45 but smaller
#: than a day drops id 7 and picks up id 9:
#:
#: * Asia/Kolkata (+05:30) -> [2026-04-01T18:30Z, 2026-04-08T18:30Z)
#: * Asia/Tokyo   (+09:00) -> [2026-04-01T15:00Z, 2026-04-08T15:00Z)
#:
#: Asserted to be the same SIZE as IN_WINDOW_IDS but a different SET, so the fixture
#: is proven to have teeth rather than merely assumed to.
SHIFTED_WINDOW_IDS = frozenset({1, 2, 3, 4, 5, 6, 9})


def in_window_rows() -> tuple[FixtureRow, ...]:
    return tuple(row for row in ROWS if row.id in IN_WINDOW_IDS)


def contract_expectations() -> list[FieldContractExpectation]:
    """``CONTRACTS`` as the adapter-facing dataclass, with a zero threshold."""
    return [
        FieldContractExpectation(field_name=field, drift_type=drift, threshold=0.0, **options)  # type: ignore[arg-type]
        for field, drift, options in CONTRACTS
    ]


def expected_bucket_counts(interval: str) -> dict[datetime, int]:
    """The bucket -> row-count map ``floor_to_bucket`` says the warehouse must return."""
    counts: dict[datetime, int] = {}
    for row in in_window_rows():
        bucket = floor_to_bucket(row.ts, interval)
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def expected_bucket_sums(interval: str) -> dict[datetime, float | None]:
    """Per-bucket ``sum(amount)``, with SQL's NULL-skipping semantics."""
    sums: dict[datetime, float | None] = {}
    for row in in_window_rows():
        bucket = floor_to_bucket(row.ts, interval)
        if row.amount is None:
            sums.setdefault(bucket, None)
            continue
        current = sums.get(bucket)
        sums[bucket] = row.amount if current is None else current + row.amount
    return sums


def expected_json_leaf_paths() -> frozenset[str]:
    """The dotted leaf paths the in-window documents hold.

    Computed with ``flatten_json_paths`` — the local reference implementation the
    warehouse-side path walks (PostgreSQL's recursive ``jsonb_each``, ClickHouse's
    ``JSONAllPaths``, BigQuery's ``JSON_KEYS``) all claim parity with. Objects are
    recursed and are not paths themselves, arrays and JSON nulls are leaves, and an
    empty object contributes nothing.
    """
    paths: set[str] = set()
    for row in in_window_rows():
        paths.update(path for path, _ in flatten_json_paths(row.doc))
    return frozenset(paths)


#: Field contracts checked on every executing warehouse.
#:
#: ClickHouse validates these with a native warehouse-side aggregate query; PostgreSQL
#: and BigQuery still fall back to BaseAdapter's Python row sampling.
#: Two completely different implementations, one required answer — which is precisely
#: what a conformance gate is for. The thresholds are 0.0 so the assertion is about the
#: COUNTS the two paths derive, not about threshold arithmetic.
CONTRACTS: tuple[tuple[str, str, dict[str, object]], ...] = (
    ("event_name", "enum_violation", {"enum_options": ("click", "view")}),
    ("amount", "required_null_violation", {}),
    ("amount", "range_violation", {"min_value": 0.0, "max_value": 50.0}),
    # A contract nothing violates. It must produce NO violation — a validator that
    # reported one here would be inventing drift out of clean data.
    ("user_id", "regex_violation", {"regex": "^u[0-9]+$"}),
)


def expected_contract_violations() -> dict[tuple[str, str], tuple[int, int]]:
    """``(field, drift_type) -> (bad_count, total_count)``, per BaseAdapter's semantics.

    ``required_null_violation`` counts every row (a NULL is the violation). Every other
    drift type SKIPS nulls entirely, so its total is the non-null population. A contract
    with zero bad rows is not reported at all.
    """
    violations: dict[tuple[str, str], tuple[int, int]] = {}
    for field, drift, options in CONTRACTS:
        bad = total = 0
        for row in in_window_rows():
            value = row.amount if field == "amount" else getattr(row, field)
            if drift == "required_null_violation":
                total += 1
                bad += value is None
                continue
            if value is None:
                continue
            total += 1
            if drift == "enum_violation":
                bad += str(value) not in options["enum_options"]  # type: ignore[operator]
            elif drift == "regex_violation":
                bad += re.search(str(options["regex"]), str(value)) is None
            elif drift == "range_violation":
                numeric = float(str(value))
                bad += numeric < float(str(options["min_value"])) or numeric > float(
                    str(options["max_value"])
                )
        if bad:
            violations[(field, drift)] = (bad, total)
    return violations


# ── the pipeline fixture ──────────────────────────────────────────────────────
#
# A SECOND rowset in the same file, for the same reason the first one exists: the
# metrics pipeline (scan -> event generation -> event metrics -> catalog metrics ->
# anomalies) is driven by the SAME warehouse-agnostic expectations on every engine,
# so a divergence between PostgreSQL and ClickHouse is a real divergence.
#
# It is shaped differently from ``ROWS`` on purpose. ``ROWS`` is a nine-row trap for
# window/bucket bugs, deliberately too small to carry a baseline. The pipeline needs
# a SERIES: enough flat history for the anomaly detector to have an opinion, plus one
# unmistakable spike. Ten 1h buckets, ~250 rows.

#: The interval the whole pipeline gate runs on.
PIPELINE_INTERVAL = "1h"

#: Bucket count and the index of the spike bucket (the last one).
PIPELINE_BUCKET_COUNT = 10
PIPELINE_SPIKE_INDEX = 9

#: The first bucket. Anchored to a RECENT wall-clock hour, not a fixed date, because
#: the worker's own window resolution (``_resolve_collection_window`` /
#: ``_resolve_value_window``) reaches back from ``now`` — a fixture pinned to 2026-04
#: would fall outside the window the production code chooses and collect nothing.
#: Twelve hours back leaves the whole series comfortably inside the default 30-bucket
#: lookback while staying strictly BELOW the latest complete boundary.
PIPELINE_ANCHOR = floor_to_bucket(datetime.now(UTC), PIPELINE_INTERVAL) - timedelta(hours=12)

#: The half-open window the gate collects. Half-open, so the spike bucket (which
#: STARTS at anchor+9h) is included and nothing beyond it is.
PIPELINE_FROM = PIPELINE_ANCHOR
PIPELINE_TO = PIPELINE_ANCHOR + timedelta(hours=PIPELINE_BUCKET_COUNT)

# Baseline volume is 12, not 2: ``ProjectAnomalySettings.min_expected_count`` defaults
# to 10, and a baseline below it is filtered out of anomaly detection entirely. A
# fixture that spiked 2 -> 20 would detect NOTHING and the drift assertions would be
# vacuously green. So the series is sized to clear the product's own default gate.
BASELINE_CLICKS = 12
SPIKE_CLICKS = 120
CLICK_AMOUNT = 1.0
BASELINE_BUY_AMOUNT = 10.0
SPIKE_BUY_AMOUNT = 100.0


@dataclass(frozen=True)
class PipelineRow:
    ts: datetime
    event_name: str
    user_id: str
    #: NULL on every ``view`` row. SUM/AVG must SKIP it, COUNT(*) must not — and a
    #: breakdown group must never end up with an all-NULL measure (see the module
    #: note in test_pipeline_conformance.py).
    amount: float | None
    platform: str


def _build_pipeline_rows() -> tuple[PipelineRow, ...]:
    rows: list[PipelineRow] = []
    for index in range(PIPELINE_BUCKET_COUNT):
        bucket = PIPELINE_ANCHOR + timedelta(hours=index)
        is_spike = index == PIPELINE_SPIKE_INDEX
        clicks = SPIKE_CLICKS if is_spike else BASELINE_CLICKS
        for k in range(clicks):
            rows.append(
                PipelineRow(
                    # k=0 lands EXACTLY on the bucket boundary: a half-open bucket
                    # must claim it. 20s apart keeps 120 spike rows inside the hour.
                    ts=bucket + timedelta(seconds=20 * k),
                    event_name="click",
                    user_id=f"u{k % 2 + 1}",
                    amount=CLICK_AMOUNT,
                    platform="ios",
                )
            )
        rows.append(
            PipelineRow(
                # The LAST microsecond of the bucket. It must floor into THIS bucket,
                # not the next one — the off-by-one that a count-only assertion misses.
                ts=bucket + timedelta(hours=1, microseconds=-1),
                event_name="view",
                user_id="u3",
                amount=None,
                platform="ios",
            )
        )
        if index > 0:
            # Bucket 0 has NO buy row, so the ratio metric's denominator is ZERO
            # there. That bucket must come back ABSENT — never 0, never inf, never
            # a NaN. Divide-by-zero is the exact defect class this epic already
            # shipped once on BigQuery.
            rows.append(
                PipelineRow(
                    ts=bucket + timedelta(minutes=30),
                    event_name="buy",
                    user_id="u1",
                    amount=SPIKE_BUY_AMOUNT if is_spike else BASELINE_BUY_AMOUNT,
                    platform="web",
                )
            )
    return tuple(rows)


PIPELINE_ROWS: tuple[PipelineRow, ...] = _build_pipeline_rows()

#: Event identities the scan generates from the pipeline rows. The scan's base query
#: projects ``event_name`` (the only column with a FieldDefinition), and the default
#: event-name format is ``col=value``, so these are the three names.
PIPELINE_EVENT_NAMES = frozenset({"event_name=click", "event_name=view", "event_name=buy"})


def pipeline_buckets() -> list[datetime]:
    """Every bucket the fixture occupies, per ``floor_to_bucket`` — sorted."""
    return sorted({floor_to_bucket(row.ts, PIPELINE_INTERVAL) for row in PIPELINE_ROWS})


def pipeline_spike_bucket() -> datetime:
    return PIPELINE_ANCHOR + timedelta(hours=PIPELINE_SPIKE_INDEX)


def _fold(
    predicate: Callable[[PipelineRow], bool] = lambda _row: True,
) -> dict[datetime, list[PipelineRow]]:
    grouped: dict[datetime, list[PipelineRow]] = {}
    for row in PIPELINE_ROWS:
        if not predicate(row):
            continue
        grouped.setdefault(floor_to_bucket(row.ts, PIPELINE_INTERVAL), []).append(row)
    return grouped


def pipeline_event_counts() -> dict[str, dict[datetime, float]]:
    """``{event identity: {bucket: count}}`` — what ``event_metrics`` must hold."""
    counts: dict[str, dict[datetime, float]] = {}
    for bucket, rows in _fold().items():
        for row in rows:
            identity = f"event_name={row.event_name}"
            series = counts.setdefault(identity, {})
            series[bucket] = series.get(bucket, 0.0) + 1.0
    return counts


def pipeline_row_counts() -> dict[datetime, float]:
    """Per-bucket ``count(*)`` over the fact rowset."""
    return {bucket: float(len(rows)) for bucket, rows in _fold().items()}


def pipeline_amount_sums() -> dict[datetime, float]:
    """Per-bucket ``sum(amount)`` with SQL's NULL-skipping semantics."""
    return {
        bucket: sum(row.amount for row in rows if row.amount is not None)
        for bucket, rows in _fold().items()
    }


def pipeline_buy_counts() -> dict[datetime, float]:
    """Per-bucket ``count(*) WHERE event_name = 'buy'``. Bucket 0 is ABSENT (zero)."""
    return {
        bucket: float(len(rows)) for bucket, rows in _fold(lambda r: r.event_name == "buy").items()
    }


def pipeline_count_ratio() -> dict[datetime, float]:
    """``count(*) / count(*) WHERE buy`` per bucket, with the divide-by-zero bucket DROPPED.

    Mirrors ``evaluate_composition`` + the NOT-NULL row builder: a zero (or absent)
    denominator yields ``None``, which is never written — so bucket 0, which has no
    buy row, must not appear in ``metric_values`` at all.
    """
    numerator = pipeline_row_counts()
    denominator = pipeline_buy_counts()
    return {
        bucket: numerator[bucket] / denominator[bucket]
        for bucket in numerator
        if denominator.get(bucket)
    }


def pipeline_distinct_users() -> dict[datetime, float]:
    """Per-bucket ``count(distinct user_id)``."""
    return {bucket: float(len({row.user_id for row in rows})) for bucket, rows in _fold().items()}


def pipeline_platform_sums() -> dict[tuple[datetime, str], float]:
    """``{(bucket, platform): sum(amount)}`` — the fact metric's breakdown rows.

    Every platform group holds at least one non-NULL amount by construction; see the
    all-NULL-group note in the pipeline gate's module docstring.
    """
    sums: dict[tuple[datetime, str], float] = {}
    for bucket, rows in _fold().items():
        for row in rows:
            if row.amount is None:
                continue
            key = (bucket, row.platform)
            sums[key] = sums.get(key, 0.0) + row.amount
    return sums


def pipeline_event_composition_ratio() -> dict[datetime, float]:
    """``click / buy`` per bucket — the event_composition ratio, zero-denominator dropped."""
    counts = pipeline_event_counts()
    clicks = counts["event_name=click"]
    buys = counts["event_name=buy"]
    return {bucket: clicks[bucket] / buys[bucket] for bucket in clicks if buys.get(bucket)}


def pipeline_events_per_distinct_user() -> dict[datetime, float]:
    """``click / count(distinct user_id)`` per bucket — the per_distinct_user composition."""
    clicks = pipeline_event_counts()["event_name=click"]
    users = pipeline_distinct_users()
    return {bucket: clicks[bucket] / users[bucket] for bucket in clicks if users.get(bucket)}


def json_null_only_leaf_paths() -> frozenset[str]:
    """Leaf paths whose every in-window value is a JSON ``null``.

    These are the paths the three warehouses genuinely DISAGREE about, and the
    conformance gates say so out loud instead of averaging over it.

    PostgreSQL's recursive ``jsonb_each`` walk (and ``flatten_json_paths``, the local
    reference) treat a JSON null as a leaf: the key exists in the document, so it
    stays discoverable. ClickHouse's ``JSON`` type does not materialize a null-valued
    dynamic subcolumn at all, so ``JSONAllPaths`` never reports the path — the key
    is invisible to discovery. Tracked as a real divergence, not hidden: the
    ClickHouse gate asserts these are the ONLY paths it is missing, so any OTHER
    path going missing still fails, and ClickHouse one day reporting them also fails
    (and tells us to tighten this back up).
    """
    values_by_path: dict[str, list[object]] = {}
    for row in in_window_rows():
        for path, value in flatten_json_paths(row.doc):
            values_by_path.setdefault(path, []).append(value)
    return frozenset(
        path for path, values in values_by_path.items() if all(value is None for value in values)
    )
