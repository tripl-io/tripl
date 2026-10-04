"""The SQL PostgresAdapter.validate_field_contracts builds.

Shape only — that the aggregate is warehouse-side, that NULLs land in the right
denominator, that a malformed number cannot make the cast explode, and that a
contract with nothing to say produces no query at all.

The *semantics* are pinned where they have to be: against a real PostgreSQL, in
``conformance/test_postgres_field_contracts_conformance.py`` (which proves this SQL
agrees with BaseAdapter's Python fallback row for row) and in the conformance gate
(which proves it agrees with ClickHouse). A string assertion cannot tell you that
``count(*) FILTER (...)`` counted the right rows; running it can.

What is NOT here: anything a second engine also has to satisfy. The row layout,
the shared verdict and the per-expectation regex probe are pinned across all four
engines at once in ``test_warehouse_engine_parity.py``, because a rule two adapters must
agree on cannot be pinned in one adapter's file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tripl.core.adapters.base import FieldContractExpectation, FieldContractViolation
from tripl.core.adapters.postgres import PostgresAdapter

BASE = "SELECT * FROM events"
FROM_TIME = datetime(2026, 4, 2, tzinfo=UTC)
TO_TIME = datetime(2026, 4, 9, tzinfo=UTC)
_ALLOWED = {"event_name", "amount", "user_id", "ts", "group_key"}


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        self._conn.sql.append(sql)

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._conn.rows


class _Conn:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rows: list[tuple[object, ...]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _pg() -> tuple[PostgresAdapter, _Conn]:
    conn = _Conn()
    adapter = object.__new__(PostgresAdapter)
    adapter._conn = conn
    adapter._allowed_columns = set(_ALLOWED)
    return adapter, conn


def _sql(expectations: list[FieldContractExpectation], **kwargs: object) -> str:
    adapter, conn = _pg()
    adapter.validate_field_contracts(BASE, expectations, **kwargs)  # type: ignore[arg-type]
    # A regex expectation offers its pattern to the server before it reaches a
    # statement (``_probe_contract_regex`` -> ``SELECT '' ~ <pattern>``), so a
    # pattern ARE refuses costs its own expectation instead of the whole scan.
    # That probe reads no table and is not the contract query; everything else
    # still has to share ONE.
    contract_sql = [sql for sql in conn.sql if not sql.startswith("SELECT '' ~ ")]
    assert len(contract_sql) == 1, f"all expectations must share ONE query: {conn.sql}"
    return contract_sql[0]


def _expectation(drift_type: str, **options: object) -> FieldContractExpectation:
    return FieldContractExpectation(
        field_name=str(options.pop("field_name", "amount")),
        drift_type=drift_type,
        threshold=float(options.pop("threshold", 0.0)),
        **options,  # type: ignore[arg-type]
    )


# --- the denominator ---------------------------------------------------------


def test_required_null_counts_nulls_as_bad_and_in_the_total() -> None:
    # The aliases are POSITIONAL (``_bad_0``/``_total_0``/``_sample_0``) because
    # every expectation now contributes three columns to one flat aggregate and the
    # decode reads them at ``index * 3``. Named ``bad_count``/``total_count``
    # columns belonged to the UNION-of-arms shape, where each arm was its own row.
    sql = _sql([_expectation("required_null_violation")])
    assert 'count(*) FILTER (WHERE "amount" IS NULL) AS _bad_0' in sql
    # Every row is in the denominator: a NULL is the thing being measured, so it
    # cannot also be excluded from the population.
    assert "count(*) AS _total_0" in sql


@pytest.mark.parametrize(
    "expectation",
    [
        _expectation("enum_violation", enum_options=("click", "view")),
        _expectation("regex_violation", regex="^u[0-9]+$", field_name="user_id"),
        _expectation("range_violation", min_value=0.0, max_value=50.0),
    ],
)
def test_every_other_drift_type_excludes_nulls_from_the_total(
    expectation: FieldContractExpectation,
) -> None:
    # ClickHouse: countIf(NOT isNull(col)). A NULL is neither bad nor counted here —
    # required_null_violation is the contract that has an opinion about NULLs.
    sql = _sql([expectation])
    column = expectation.field_name
    assert f'count(*) FILTER (WHERE "{column}" IS NOT NULL) AS _total_0' in sql
    assert f'"{column}" IS NOT NULL AND' in sql


# --- range: the malformed-number trap ----------------------------------------


def test_a_range_cast_is_guarded_by_a_regex_so_a_bad_string_cannot_abort_the_query() -> None:
    sql = _sql([_expectation("range_violation", min_value=0.0, max_value=50.0)])
    # A bare `::numeric` on 'twelve' RAISES in Postgres (ClickHouse's
    # toFloat64OrNull just returns NULL), and one bad row would take down the
    # contract query for every OTHER expectation in the same statement too. The
    # regex also bounds MAGNITUDE, because a well-formed `1e999999999` overflows
    # numeric too.
    assert "CASE WHEN COALESCE(\"amount\"::text, '') ~ '^[+-]?" in sql
    assert "COALESCE(NOT (" in sql, "an unparseable value must fall through to BAD"
    assert "TRUE)" in sql


def test_a_malformed_value_is_bad_but_a_nan_is_not() -> None:
    sql = _sql([_expectation("range_violation", min_value=0.0, max_value=50.0)])
    # Python's float('nan') parses, and every NaN comparison is false, so the
    # fallback calls NaN in-range. Postgres sorts NaN ABOVE every float, so a NaN
    # reaching the comparison would read as "over max". It is excluded instead.
    assert "~* '^[+-]?nan$'" in sql
    assert "'Infinity'::numeric" in sql
    # And nothing in this statement is float8 any more: float8's input function
    # raises 22003 on BOTH overflow ('1e400') and underflow-to-zero ('1e-400'),
    # which the syntax-only guard admitted, and numeric compares exact decimals
    # and cannot. The rate that used to be cast to `double precision` here is not
    # in the statement either — `field_contract_verdict` divides in Python now.
    assert "double precision" not in sql


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"min_value": 0.0}, ">= 0.0"),
        ({"max_value": 50.0}, "<= 50.0"),
    ],
)
def test_a_one_sided_range_only_checks_the_bound_it_has(
    options: dict[str, float], expected: str
) -> None:
    sql = _sql([_expectation("range_violation", **options)])
    assert expected in sql
    absent = "<=" if "min_value" in options else ">="
    assert absent not in sql.split("COALESCE(NOT (")[1].split("), TRUE)")[0]


# --- window, grouping, thresholds --------------------------------------------


def test_the_window_and_the_group_filter_are_applied_in_the_database() -> None:
    sql = _sql(
        [_expectation("required_null_violation")],
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        group_column="group_key",
        group_value="checkout",
    )
    assert "\"ts\" >= TIMESTAMPTZ '2026-04-02 00:00:00.000000+00:00'" in sql
    assert "\"ts\" < TIMESTAMPTZ '2026-04-09 00:00:00.000000+00:00'" in sql
    # NULL group values compare as '' — ClickHouse's ifNull(toString(col), '').
    assert "COALESCE(\"group_key\"::text, '') = 'checkout'" in sql


def test_the_threshold_never_reaches_the_statement_but_is_still_clamped() -> None:
    """The comparison left SQL; the clamp came with it, unchanged in meaning.

    This used to assert ``bad_count::double precision / total_count > 0.25`` and a
    ``WHERE total_count > 0 AND bad_count > 0`` wrapper — i.e. that PostgreSQL
    decided what a violation IS. Three engines each deciding that is how the rule
    drifted, so the statement returns counts and nothing else and
    ``field_contract_verdict`` judges. That the OTHER engines stopped shipping it
    too is ``test_warehouse_engine_parity.py``'s to say; what is pinned here is that this
    adapter's statement carries no threshold and no rate.

    Out-of-range thresholds are still clamped rather than trusted, and the clamp is
    asserted through ``validate_field_contracts`` rather than against
    ``clamp_field_contract_threshold`` directly, because what regressed before was
    an adapter forgetting to apply it, not the helper getting it wrong.
    """
    sql = _sql([_expectation("required_null_violation", threshold=0.25)])
    assert "0.25" not in sql
    assert "bad_count" not in sql
    assert "total_count" not in sql

    # -1.0 clamps to 0.0, which a bad rate of 3/12 clears, and the violation
    # REPORTS the bound that was applied rather than the one that was stored.
    adapter, conn = _pg()
    conn.rows = [(3, 12, "<NULL>")]
    below = adapter.validate_field_contracts(
        BASE, [_expectation("required_null_violation", threshold=-1.0)]
    )
    assert [violation.threshold for violation in below] == [0.0]

    # 9.0 clamps to 1.0, which no bad rate can exceed, so there is nothing to
    # report — the same answer 9.0 itself would have given, which is why clamping
    # is a reporting fix and not a verdict fix.
    adapter, conn = _pg()
    conn.rows = [(3, 12, "<NULL>")]
    assert (
        adapter.validate_field_contracts(
            BASE, [_expectation("required_null_violation", threshold=9.0)]
        )
        == []
    )


def test_one_query_serves_every_expectation() -> None:
    sql = _sql(
        [
            _expectation("enum_violation", field_name="event_name", enum_options=("click",)),
            _expectation("required_null_violation"),
            _expectation("range_violation", min_value=0.0, max_value=50.0),
        ]
    )
    # One SCAN, not just one statement. The shape this replaced was a UNION ALL of
    # an aggregate subquery per expectation: still one statement, but PostgreSQL
    # does not deduplicate an identical inline subquery across UNION arms, so it
    # re-read the whole window once per contract — and catalog_sync calls this once
    # per event-type group.
    assert "UNION ALL" not in sql
    assert sql.count(f"FROM ({BASE}) AS _src") == 1
    # Deterministic order, matching the order the caller listed the contracts in.
    # The ordering IS the numbering now: the decode walks the expectations and
    # reads (bad, total, sample) at index * 3, which is what retired the
    # `ORDER BY _ord LIMIT 50000` a set of independent rows needed.
    positions = [sql.index(f"AS _bad_{index}") for index in range(3)]
    assert positions == sorted(positions)
    assert "ORDER BY" not in sql


# --- contracts that say nothing ----------------------------------------------


@pytest.mark.parametrize(
    "expectation",
    [
        _expectation("enum_violation", enum_options=()),
        _expectation("regex_violation", regex=None),
        _expectation("range_violation"),
        _expectation("something_else_entirely"),
    ],
)
def test_a_contract_with_nothing_to_check_runs_no_query(
    expectation: FieldContractExpectation,
) -> None:
    # An enum with no options would otherwise mark every row bad, inventing drift
    # out of a contract that does not constrain anything.
    adapter, conn = _pg()
    assert adapter.validate_field_contracts(BASE, [expectation]) == []
    assert conn.sql == []


def test_no_expectations_runs_no_query() -> None:
    adapter, conn = _pg()
    assert adapter.validate_field_contracts(BASE, []) == []
    assert conn.sql == []


def test_a_field_outside_the_result_set_is_refused() -> None:
    adapter, _ = _pg()
    with pytest.raises(ValueError, match="not found in query result"):
        adapter.validate_field_contracts(
            BASE, [_expectation("required_null_violation", field_name="nope")]
        )


# --- the rows that come back -------------------------------------------------


def test_violations_are_mapped_off_the_warehouse_row() -> None:
    adapter, conn = _pg()
    # The column order the SELECT emits, per expectation: bad, total, sample. The
    # identity (field, drift) and the judgement (threshold, rate) are NOT in the
    # row any more — they are read off the expectation the decode is walking and
    # computed by `field_contract_verdict`. Reading the counts in the wrong order
    # is what this pins now; it used to be a seven-column row per violation where
    # threshold and bad_rate sat next to each other and could be swapped.
    conn.rows = [(3, 12, "99")]
    violations = adapter.validate_field_contracts(
        BASE, [_expectation("range_violation", min_value=0.0, max_value=50.0)]
    )
    assert violations == [
        FieldContractViolation(
            field_name="amount",
            drift_type="range_violation",
            bad_count=3,
            total_count=12,
            bad_rate=0.25,
            threshold=0.0,
            sample_value="99",
        )
    ]
