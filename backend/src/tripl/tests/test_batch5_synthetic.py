"""Batch-5 regression tests for the synthetic warehouse and the demo catalog.

Five findings, all of which produced a WRONG NUMBER rather than an error, so
every test here asserts a value relationship rather than "something came back":

* the adapter could not read the dialect it declares. Every
  filter the metric collector compiled for it (back-tick quoted, backslash
  escaped) matched nothing on ``=`` and everything on ``!=``, and the per-metric
  collection path's row filter — delivered by WRAPPING the source in a
  ``WHERE`` subquery — was thrown away entirely.
* every row digest was keyed on its offset from the anchor, and
  the anchor moves on every scan, so the same absolute hour held different rows
  each time and session pools rolled over at the anchor's hour instead of UTC
  midnight.
* the seeded sql-metric shape was recognized by probing for
  three substrings, so an edited query was answered with the unfiltered series.
* a capability error that could not be surfaced, a budget guard
  that could not fire, and a connection test that could not fail.
* orders were stamped up to 24h into the future while events
  stopped at the last complete hour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

# ``tripl.worker.tasks`` participates in an import cycle that only resolves when
# ``celery_app`` starts the load; entering any other way raises ImportError on a
# partially initialised module.
import tripl.worker.celery_app  # noqa: F401
from tripl.core.adapters import synthetic as synth
from tripl.core.adapters.base import AggregateSpec
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    SqlDialect,
    dialect_for_db_type,
    quote_sql_string_literal,
)
from tripl.core.adapters.synthetic import SyntheticAdapter, SyntheticCapabilityError
from tripl.models.domain_enums import MetricAggregation as MA
from tripl.models.fact_table import FactTable
from tripl.services.demo.builders.catalog import ACTIVE_SESSIONS_METRIC_SQL
from tripl.worker.tasks.metrics._fact_conditions import (
    _FactCondition,
    _FactOperand,
    _resolve_condition_fragment,
    _resolve_fact_operand_filter,
    _resolve_fact_operand_query,
)

ANCHOR = datetime(2026, 6, 1, tzinfo=UTC)
HISTORY_DAYS = 30
SEED = 7

# The demo's own fact-table SQL and row filter, copied from
# ``services.demo.builders.catalog._build_fact_table`` — the shape these findings
# were measured on.
_ORDERS_SQL = "SELECT created_at, amount, currency, user_id, country, status FROM orders"
_COMPLETED_FILTER = "status = 'completed'"
# ``measure_validator`` maps db_type "synthetic" to the ClickHouse dialect, so a
# fragment compiled for this source is back-tick quoted.
_DIALECT = dialect_for_db_type("synthetic")


def _adapter(seed: int = SEED, anchor: datetime = ANCHOR) -> SyntheticAdapter:
    return SyntheticAdapter(seed=seed, anchor=anchor, history_days=HISTORY_DAYS)


def _orders_fact_table() -> FactTable:
    """A transient copy of the demo's orders fact table (no session needed)."""
    return FactTable(
        id=uuid.uuid4(),
        name="orders",
        sql=_ORDERS_SQL,
        timestamp_column="created_at",
        columns=[
            {"name": "created_at", "type": "timestamp"},
            {"name": "amount", "type": "number"},
            {"name": "currency", "type": "string"},
            {"name": "user_id", "type": "string"},
            {"name": "country", "type": "string"},
            {"name": "status", "type": "string"},
        ],
        row_filters=[{"name": "completed", "sql": _COMPLETED_FILTER}],
    )


def _counts(adapter: SyntheticAdapter, filter_sql: str | None) -> dict[datetime, object]:
    """Daily counts over the orders table, optionally through ``filter_sql``."""
    time_from, time_to = ANCHOR - timedelta(days=6), ANCHOR
    spec = AggregateSpec(key="cnt", aggregation=MA.count, filter_sql=filter_sql)
    _columns, rows = adapter.get_time_bucketed_multi_aggregate(
        _ORDERS_SQL, "created_at", "1d", [spec], time_from, time_to
    )
    return {bucket: value for bucket, value in rows}


def _condition_fragment(operator: str, value: object) -> str:
    """Compile one visual condition the way the collector compiles it."""
    return _resolve_condition_fragment(
        _FactCondition(column="status", operator=operator, value=value),
        dialect=_DIALECT,
        column_types={"status": "string"},
    )


# --------------------------------------------------------------------------- #
# the adapter reads the dialect it declares
# --------------------------------------------------------------------------- #


def test_compiled_structured_condition_filters_like_the_free_text_form() -> None:
    """A compiled ``eq`` condition selects exactly the rows the bare form does.

    The compiled fragment is back-tick quoted (``measure_validator`` declares
    this source to be ClickHouse), and the evaluator looked its column up
    literally — ``row.get("`status`")`` is ``None`` on every row, so the
    conditional aggregate matched nothing and the metric stored NULL for every
    bucket with no error anywhere. Equality against the free-text form is what
    makes this load-bearing: asserting merely "not None" would pass a fix that
    matched one arbitrary row.
    """
    fragment = _condition_fragment("eq", "completed")
    assert "`status`" in fragment, fragment

    adapter = _adapter()
    compiled = _counts(adapter, fragment)
    free_text = _counts(adapter, _COMPLETED_FILTER)
    unfiltered = _counts(adapter, None)

    assert compiled == free_text
    assert set(compiled) == set(unfiltered)
    assert all(compiled[bucket] is not None for bucket in compiled)
    # A real subset: the demo's statuses are weighted, never all 'completed'.
    assert all(compiled[bucket] < unfiltered[bucket] for bucket in compiled)


def test_compiled_ne_condition_does_not_match_every_row() -> None:
    """``!=`` partitions the bucket instead of returning the unfiltered total.

    ``row.get("`status`")`` was ``None``, and ``_bval(None)`` is ``''``, so
    ``!=`` was true for every row and the metric reported the whole table.
    """
    adapter = _adapter()
    matching = _counts(adapter, _condition_fragment("ne", "completed"))
    complement = _counts(adapter, _condition_fragment("eq", "completed"))
    unfiltered = _counts(adapter, None)

    for bucket, total in unfiltered.items():
        assert matching[bucket] + complement[bucket] == total, bucket
        assert matching[bucket] != total, bucket


def test_unknown_filter_column_is_refused_rather_than_read_as_null() -> None:
    """A column the table does not have raises instead of silently matching none."""
    adapter = _adapter()
    with pytest.raises(SyntheticCapabilityError, match="Unsupported filter column"):
        _counts(adapter, "`nope` = 'x'")


def test_dot_qualified_filter_column_is_refused() -> None:
    """``\\`t\\`.\\`col\\``` names an alias the synthetic tables do not have."""
    adapter = _adapter()
    with pytest.raises(SyntheticCapabilityError, match="Unsupported filter column"):
        _counts(adapter, "`orders`.`status` = 'completed'")


def test_escaped_apostrophe_literals_decode_in_both_dialects() -> None:
    """Both escape spellings decode to the same value.

    ``quote_sql_string_literal`` emits the BACKSLASH form for this source's
    dialect, and the old unescaper (``.strip("'").replace("''", "'")``) left the
    backslash in place, so a value containing an apostrophe never matched. The
    PostgreSQL ``''`` form must keep working too: named and free-text row filters
    are user text written against whatever engine the user had in mind.
    """
    adapter = _adapter()
    row = {"country": "o'brien"}
    clickhouse = quote_sql_string_literal("o'brien", SqlDialect.clickhouse)
    postgres = quote_sql_string_literal("o'brien", SqlDialect.postgres)
    assert clickhouse == "'o\\'brien'"
    assert postgres == "'o''brien'"

    assert adapter._row_matches_filter("orders", row, f"country = {clickhouse}")
    assert adapter._row_matches_filter("orders", row, f"country = {postgres}")
    other = {"country": "obrien"}
    assert not adapter._row_matches_filter("orders", other, f"country = {clickhouse}")


def test_filter_literal_containing_an_operator_is_not_mis_split() -> None:
    """An operator INSIDE a value is part of the value, not a comparison."""
    adapter = _adapter()
    assert adapter._row_matches_filter("orders", {"status": "a<=b"}, "status = 'a<=b'")
    assert not adapter._row_matches_filter("orders", {"status": "ab"}, "status = 'a<=b'")


def test_and_inside_a_filter_literal_is_not_a_boolean_connective() -> None:
    """``'Trinidad and Tobago'`` is one value, not two atoms.

    The splitters used to scan the raw text, so this expression was cut in half
    and each half compared as its own (broken) atom — a silent wrong answer, not
    an error.
    """
    adapter = _adapter()
    row = {"country": "Trinidad and Tobago"}
    assert adapter._row_matches_filter("orders", row, "country = 'Trinidad and Tobago'")
    assert not adapter._row_matches_filter(
        "orders", {"country": "US"}, "country = 'Trinidad and Tobago'"
    )


def test_null_value_never_matches_any_comparison() -> None:
    """SQL three-valued logic: ``!=`` is false for a NULL column, not true.

    Four events columns are nullable, and ``_bval`` rendered NULL as ``''``, so
    ``button_id != 'share'`` counted every row that carried no button at all.
    """
    adapter = _adapter()
    null_row: dict[str, object] = {"button_id": None}
    assert not adapter._row_matches_filter("events", null_row, "button_id = 'share'")
    assert not adapter._row_matches_filter("events", null_row, "button_id != 'share'")
    assert adapter._row_matches_filter("events", {"button_id": "buy_now"}, "button_id != 'share'")


def test_timestamp_condition_still_fails_loudly() -> None:
    """A compiled timestamp bound is a function call — refused, never guessed."""
    adapter = _adapter()
    fragment = "created_at >= parseDateTime64BestEffort('2026-06-01 00:00:00', 6, 'UTC')"
    with pytest.raises(SyntheticCapabilityError, match="Unsupported filter expression"):
        _counts(adapter, fragment)


def test_wrapped_where_subquery_matches_the_conditional_aggregate() -> None:
    """The per-metric and batched collection paths agree, bucket for bucket.

    ``metric_collect`` states as an invariant that "the per-bucket VALUES are
    identical to the per-metric path". They were not: the per-metric path
    delivers its row filter by wrapping the fact SQL in ``SELECT * FROM (...) AS
    _filtered WHERE ...`` and this adapter read only the table name out of that,
    so it summed EVERY order while the batched path summed the completed ones —
    about 2x apart on the demo as shipped. Both queries here are built by the
    real collector helpers, not hand-spelled.
    """
    fact_table = _orders_fact_table()
    operand = _FactOperand(
        fact_table_id=fact_table.id,
        aggregation=MA.sum,
        measure_column="amount",
        distinct_column=None,
        row_filters=("completed",),
        filter_sql=None,
        conditions=(_FactCondition(column="status", operator="eq", value="completed"),),
    )
    per_metric_query = _resolve_fact_operand_query(fact_table, operand, dialect=_DIALECT)
    batched_filter = _resolve_fact_operand_filter(operand, fact_table=fact_table, dialect=_DIALECT)
    assert per_metric_query.startswith("SELECT * FROM (")
    assert batched_filter is not None

    adapter = _adapter()
    time_from, time_to = ANCHOR - timedelta(days=10), ANCHOR
    _cols, _json, per_metric_rows = adapter.get_time_bucketed_aggregate(
        per_metric_query, "created_at", "1d", MA.sum, "amount", [], [], None, time_from, time_to
    )
    per_metric = {bucket: value for bucket, value in per_metric_rows}

    spec = AggregateSpec(key="rev", aggregation=MA.sum, column="amount", filter_sql=batched_filter)
    _columns, batched_rows = adapter.get_time_bucketed_multi_aggregate(
        _ORDERS_SQL, "created_at", "1d", [spec], time_from, time_to
    )
    batched = {bucket: value for bucket, value in batched_rows if value is not None}

    assert per_metric, "the filtered per-metric scan produced no buckets"
    assert set(per_metric) == set(batched)
    for bucket, value in per_metric.items():
        assert value == pytest.approx(batched[bucket]), bucket

    # ...and the filtered answer really is a strict subset of the unfiltered one,
    # so the equality above cannot be satisfied by ignoring the filter twice.
    _c, _j, unfiltered_rows = adapter.get_time_bucketed_aggregate(
        _ORDERS_SQL, "created_at", "1d", MA.sum, "amount", [], [], None, time_from, time_to
    )
    unfiltered = {bucket: value for bucket, value in unfiltered_rows}
    assert all(per_metric[bucket] < unfiltered[bucket] for bucket in per_metric)


def test_cte_backed_source_keeps_its_inner_where_inside_the_cte() -> None:
    """Only a depth-0 ``WHERE`` is the scan's predicate.

    A fact source may be a CTE carrying its own ``WHERE``. Mis-splitting on it
    would apply a CTE-internal predicate to the whole table, so the scanner is
    paren-depth aware and this pins it.
    """
    cte = "WITH recent AS (SELECT * FROM orders WHERE status = 'completed') SELECT * FROM recent"
    assert synth._trailing_where_predicate(cte) is None

    wrapped = f"SELECT * FROM ({cte}) AS _filtered WHERE (country = 'US')"
    assert synth._trailing_where_predicate(wrapped) == "(country = 'US')"


def test_the_word_where_inside_a_literal_is_not_a_clause() -> None:
    query = "SELECT * FROM orders WHERE country = 'where'"
    assert synth._trailing_where_predicate(query) == "country = 'where'"
    assert synth._trailing_where_predicate("SELECT * FROM orders") is None


def test_two_top_level_where_clauses_are_refused() -> None:
    """Ambiguity is refused, not resolved by picking one and hoping."""
    query = (
        "SELECT * FROM orders WHERE country = 'US' "
        "UNION ALL SELECT * FROM orders WHERE country = 'GB'"
    )
    with pytest.raises(SyntheticCapabilityError, match="two top-level WHERE"):
        synth._trailing_where_predicate(query)


def test_a_value_containing_the_table_name_does_not_switch_tables() -> None:
    """Table selection reads the query, not the values inside it."""
    query = "SELECT * FROM events WHERE product_id = 'orders'"
    assert _adapter()._table_for_query(query) == "events"


# --------------------------------------------------------------------------- #
# digests keyed on absolute time
# --------------------------------------------------------------------------- #


def _sessions_per_day(adapter: SyntheticAdapter) -> dict[datetime, int]:
    """The seeded active-sessions series, read back through the metric path."""
    _columns, rows = adapter.get_preview_rows(
        ACTIVE_SESSIONS_METRIC_SQL, limit=100_000, time_column="ts"
    )
    return {bucket: int(value) for bucket, value in rows}  # type: ignore[call-overload]


def test_dataset_is_identical_for_any_anchor_over_the_overlapping_window() -> None:
    """Two anchors, same seed: the rows they share are the same rows.

    The adapter is rebuilt with ``anchor=None`` on every scan, so the anchor is
    whatever hour the scheduler fired at. With anchor-relative digest keys the
    minute, second, platform, app_version, user_id and session of every
    historical row changed on each rebuild — a warehouse that rewrote its own
    history between two reads. The window below is in the SAMPLED region of both
    adapters, so the ongoing/sampled regime boundary does not confound it.
    """
    midnight = _adapter(anchor=ANCHOR)
    afternoon = _adapter(anchor=ANCHOR.replace(hour=13))
    low = ANCHOR - timedelta(days=28)
    high = ANCHOR - timedelta(days=1)

    from_midnight = [row for row in midnight._events if low <= row["event_time"] < high]
    from_afternoon = [row for row in afternoon._events if low <= row["event_time"] < high]
    assert from_midnight, "the overlapping window must not be empty"
    assert from_midnight == from_afternoon

    orders_midnight = [row for row in midnight._orders if low <= row["created_at"] < high]
    orders_afternoon = [row for row in afternoon._orders if low <= row["created_at"] < high]
    assert orders_midnight, "the overlapping order window must not be empty"
    assert orders_midnight == orders_afternoon


def test_an_hour_that_ages_out_of_the_ongoing_window_is_a_different_sample() -> None:
    """Absolute digest keys buy identity WITHIN a regime, not across the boundary.

    ``_generate_events`` measures the ongoing window back from the anchor
    (``ongoing_start_hour = total_hours - SYNTHETIC_ONGOING_HOURS``), so which
    regime an absolute hour sits in depends on which anchor asked. Two anchors
    that put it in the same regime agree row for row; two that do not return
    different SAMPLES of the same hour, not a thinned copy of one. Since the
    adapter is rebuilt with ``anchor=None`` on every scan, that boundary sweeps
    the dataset continuously, so a re-collection whose window reaches further
    back than ``SYNTHETIC_ONGOING_HOURS`` cannot reproduce the earlier read. The
    module docstring states that limit instead of promising identity; this is
    what holds it to it.
    """
    low = ANCHOR - timedelta(hours=1)
    high = ANCHOR
    inside = _adapter(anchor=ANCHOR)
    still_inside = _adapter(anchor=ANCHOR + timedelta(hours=1))
    outside = _adapter(anchor=ANCHOR + timedelta(hours=synth.SYNTHETIC_ONGOING_HOURS))

    ongoing = [row for row in inside._events if low <= row["event_time"] < high]
    ongoing_again = [row for row in still_inside._events if low <= row["event_time"] < high]
    sampled = [row for row in outside._events if low <= row["event_time"] < high]

    # Same regime, two anchors: byte for byte. That is the tripl-0zpq.73 property.
    assert ongoing, "the hour before the anchor must not be empty"
    assert ongoing == ongoing_again

    # Past the boundary the same hour is regenerated at the sampled scale: orders
    # of magnitude thinner, and NOT a subset, so rows an earlier read returned are
    # gone rather than merely fewer.
    assert sampled, "the aged-out hour must still hold rows"
    assert len(sampled) * 100 < len(ongoing)
    assert any(row not in ongoing for row in sampled)


def test_active_sessions_pools_roll_at_utc_midnight() -> None:
    """A UTC day draws from ONE session pool, whatever hour generated it.

    Session ids were namespaced by a day INDEX counted from the anchor, so the
    pool rolled at the anchor's hour and a UTC day straddled two pools. The
    per-day distinct-session count then inflated by up to ~70% for an afternoon
    creation — against a history seeded at a different hour, and recollected
    hourly by the scheduler at whatever hour it fired.
    """
    series = {
        hour: _sessions_per_day(_adapter(anchor=ANCHOR.replace(hour=hour)))
        for hour in (0, 7, 13, 19)
    }
    shared = sorted(set.intersection(*(set(day_counts) for day_counts in series.values())))
    # Drop the oldest and newest, which are partial for at least one anchor.
    complete = shared[1:-1]
    assert len(complete) >= 20, complete

    for day in complete:
        counts = {hour: series[hour][day] for hour in series}
        assert len(set(counts.values())) == 1, (day, counts)


def test_session_pool_size_bounds_every_full_utc_day() -> None:
    """No UTC day can exceed the ``25 + digest % 20`` pool ceiling."""
    adapter = _adapter(anchor=ANCHOR.replace(hour=13))
    per_day = _sessions_per_day(adapter)
    # The oldest and newest buckets are partial days; every other one is whole.
    for day in sorted(per_day)[1:-1]:
        assert 0 < per_day[day] <= 44, (day, per_day[day])


def test_a_naive_anchor_is_read_as_utc() -> None:
    """``to_utc`` normalizes the anchor, so absolute keying works for naive input."""
    naive = SyntheticAdapter(seed=SEED, anchor=datetime(2026, 6, 1, 13), history_days=3)
    aware = SyntheticAdapter(seed=SEED, anchor=ANCHOR.replace(hour=13), history_days=3)
    assert naive._anchor == aware._anchor
    assert naive._events == aware._events
    assert naive._orders == aware._orders


# --------------------------------------------------------------------------- #
# the sql-metric shape is recognized exactly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql",
    [
        # A WHERE the adapter cannot honour: it computed the UNFILTERED series.
        "SELECT toStartOfDay(event_time) AS ts, count(DISTINCT session_id) AS value "
        "FROM events WHERE platform = 'ios' GROUP BY ts",
        # Arithmetic on the measure: the answer was the undivided count.
        "SELECT toStartOfDay(event_time) AS ts, count(DISTINCT session_id) / 2 AS value "
        "FROM events GROUP BY ts",
        # A different table -- 'from events' is a SUBSTRING of 'from events_archive'.
        "SELECT toStartOfDay(event_time) AS ts, count(DISTINCT session_id) AS value "
        "FROM events_archive GROUP BY ts",
    ],
)
def test_edited_active_sessions_sql_is_refused(sql: str) -> None:
    """Each of these returned a plausible fabricated series under the substring probe."""
    with pytest.raises(SyntheticCapabilityError):
        _adapter().get_preview_rows(sql, time_column="ts")


def test_seeded_active_sessions_sql_is_recognized() -> None:
    """The EXACT statement the demo builder writes is computable.

    Imported from the builder rather than copied, so the seeder cannot change the
    text without this failing — which is the coupling exact matching needs.
    """
    adapter = _adapter()
    assert "GROUP BY ts" in ACTIVE_SESSIONS_METRIC_SQL, (
        "the seeded statement must be valid ClickHouse"
    )
    # Membership in the adapter's curated set, asserted directly: this is the
    # coupling that stops the seeder and the adapter drifting apart silently.
    assert adapter._is_active_sessions_query(ACTIVE_SESSIONS_METRIC_SQL)
    columns, rows = adapter.get_preview_rows(
        ACTIVE_SESSIONS_METRIC_SQL,
        limit=1000,
        time_column="ts",
        time_from=ANCHOR - timedelta(days=7),
        time_to=ANCHOR,
    )
    assert columns == ["ts", "value"]
    assert len(rows) == 7
    for bucket, value in rows:
        assert bucket == bucket.replace(hour=0, minute=0, second=0, microsecond=0)
        assert isinstance(value, int) and value > 0


def test_legacy_active_sessions_sql_still_collects() -> None:
    """Demos created before the GROUP BY fix keep working without a migration."""
    legacy = (
        "SELECT toStartOfDay(event_time) AS ts, count(DISTINCT session_id) AS value FROM events"
    )
    assert legacy != ACTIVE_SESSIONS_METRIC_SQL
    columns, rows = _adapter().get_preview_rows(legacy, limit=1000, time_column="ts")
    assert columns == ["ts", "value"]
    assert rows


def test_recognition_survives_punctuation_and_whitespace() -> None:
    """Normalization matches what ``validate_select_sql`` hands the collector."""
    adapter = _adapter()
    noisy = "  select  toStartOfDay(event_time)   AS ts,\n count(DISTINCT session_id) AS value\n"
    noisy += " FROM events GROUP BY ts ;  "
    assert adapter._is_active_sessions_query(noisy)


# --------------------------------------------------------------------------- #
# errors that surface, guards that fire, checks that can fail
# --------------------------------------------------------------------------- #


def test_capability_error_is_a_warehouse_capability_error() -> None:
    """The class the sanitisers key on, so the sentence we wrote reaches the user.

    ``SyntheticCapabilityError`` was a bare ``RuntimeError``, so the carefully
    worded "only supports plain table scans" reached a demo user as "Scan failed
    due to an internal error." Both sanitisers key on the BASE class, so the
    inheritance is the whole fix on this side; the preview path is asserted here
    because it needs nothing outside this lane to hold.
    """
    from tripl.services.metric_preview_service import _warehouse_error_message

    assert issubclass(SyntheticCapabilityError, WarehouseCapabilityError)
    message = _warehouse_error_message(
        SyntheticCapabilityError("The synthetic warehouse only supports plain table scans")
    )
    assert message == "The synthetic warehouse only supports plain table scans"


def test_test_connection_reports_absent_data() -> None:
    """A connection test that can fail. ``len(x) >= 0`` is true of any list."""
    assert SyntheticAdapter(seed=SEED, anchor=ANCHOR, history_days=0).test_connection() is False
    assert _adapter().test_connection() is True


def test_budget_guard_rejects_an_oversized_dataset() -> None:
    """The guard fires at construction, where a generator defect can reach it.

    It used to run per scan against the generators' own already-capped output, so
    the branch was unreachable — a guard that guarded nothing, for a cap whose
    breach silently drops the NEWEST hours a live scan reads.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            synth,
            "_generate_orders",
            lambda seed, anchor, history_days, max_rows: [{"created_at": anchor}] * (max_rows + 1),
        )
        with pytest.raises(SyntheticCapabilityError, match="row budget"):
            SyntheticAdapter(seed=SEED, anchor=ANCHOR, history_days=1, max_rows=10)


# --------------------------------------------------------------------------- #
# one horizon for both tables
# --------------------------------------------------------------------------- #

# Deliberately NOT midnight: midnight is the single hour at which the old
# generator's error was smallest, which is why every existing test missed this.
_MIDDAY = ANCHOR.replace(hour=13)


def test_no_order_is_timestamped_after_the_anchor() -> None:
    adapter = _adapter(anchor=_MIDDAY)
    assert adapter._orders, "expected orders in the window"
    assert max(row["created_at"] for row in adapter._orders) < adapter._anchor


def test_orders_and_events_share_one_horizon() -> None:
    """Both tables agree about where "now" is — the invariant, stated once.

    Events stopped at the last complete hour while orders ran to ``anchor + 24h``,
    so the untimed ``get_full_breakdown`` path reported rows from the future.
    """
    adapter = _adapter(anchor=_MIDDAY)
    assert max(row["created_at"] for row in adapter._orders) < adapter._anchor
    assert max(row["event_time"] for row in adapter._events) < adapter._anchor


def test_order_days_are_utc_aligned() -> None:
    """Order days are whole UTC days ending today, not anchor-hour-offset days."""
    adapter = _adapter(anchor=_MIDDAY)
    expected = {(_MIDDAY - timedelta(days=offset)).date() for offset in range(HISTORY_DAYS)}
    seen = {row["created_at"].date() for row in adapter._orders}
    assert seen <= expected
    # Today is present and PARTIAL: the day fills up as it goes, like the events
    # table's newest hour. Reverting the day alignment alone puts orders on a day
    # that straddles the anchor hour and this set stops matching.
    assert _MIDDAY.date() in seen
    assert all(row["created_at"] < adapter._anchor for row in adapter._orders)


def test_demo_seeds_only_complete_daily_buckets() -> None:
    """The seeded series stop where a collection would stop.

    ``_resolve_value_window`` ends a daily collection at ``floor(now, 1d)``, so a
    seeded bucket for "today" is one no recollection can ever reproduce: it held
    a fraction of a day's data next to thirty full ones and read as a drop.
    Exercised against the REAL builder with a recording session, no database.
    """
    from tripl.services.demo.builders import catalog
    from tripl.services.demo.scenario import DemoContext

    class _RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, instance: object) -> None:
            self.added.append(instance)

    now = datetime(2026, 6, 1, 13, 42, tzinfo=UTC)
    ctx = DemoContext(project_id=uuid.uuid4(), branch_id=uuid.uuid4(), slug="demo-batch5", now=now)

    class _Definition:
        def __init__(self, config: dict[str, object] | None = None) -> None:
            self.id = uuid.uuid4()
            self.config = config

    metric_defs = {
        "revenue_completed": _Definition(),
        "average_order_value": _Definition(),
        "active_sessions": _Definition({"metric_sql": ACTIVE_SESSIONS_METRIC_SQL}),
    }
    session = _RecordingSession()
    adapter = _adapter(anchor=now)
    series = catalog._derive_adapter_series(adapter, ctx.now, ACTIVE_SESSIONS_METRIC_SQL)
    catalog._add_adapter_derived_values(session, metric_defs, series)

    end_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    buckets_by_metric: dict[uuid.UUID, list[datetime]] = {}
    for value in session.added:
        buckets_by_metric.setdefault(value.metric_definition_id, []).append(value.bucket)

    assert len(buckets_by_metric) == 3, "every derived metric must seed a series"
    for metric_id, buckets in buckets_by_metric.items():
        assert buckets, metric_id
        assert max(buckets) < end_day, (metric_id, max(buckets))
