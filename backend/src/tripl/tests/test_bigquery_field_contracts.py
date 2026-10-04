"""BigQueryAdapter.validate_field_contracts — warehouse-side, over the FULL window.

BigQuery used to inherit ``BaseAdapter.validate_field_contracts``: a Python fallback
that pulls at most ``limit`` (50,000) sampled rows through ``get_preview_rows`` and
evaluates the contracts in memory. A violation that first appears at row 50,001 was
therefore invisible, and the ``bad_rate`` it reported described the *sample*, not the
data — so a badly violated contract could sit quietly under its threshold. ClickHouse
has always done this with warehouse aggregates over the whole window; this brings
BigQuery to the same contract.

What these tests can and cannot prove
-------------------------------------
They prove the SEMANTICS that a SQL analyzer cannot see — which rows land in the
denominator, what counts as "bad", which BigQuery function was chosen — by asserting on
the generated statement. They deliberately do NOT prove the statement is valid
GoogleSQL: a fake client accepts any string, and this codebase has already shipped two
BigQuery P0s (``TIMESTAMP_BIN``, ``GROUP BY <array>``) that passed exactly this kind of
test. Validity is the job of the ZetaSQL gate in ``tests/conformance/``, and every
statement asserted on below was put through that analyzer while it was written.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from google.cloud import bigquery

from tripl.core.adapters.base import FieldContractExpectation, FieldContractViolation
from tripl.core.adapters.bigquery import BigQueryAdapter

BASE = "SELECT * FROM events"
FROM_TIME = datetime(2026, 4, 1, tzinfo=UTC)
TO_TIME = datetime(2026, 4, 8, tzinfo=UTC)

_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("ts", "TIMESTAMP"),
    bigquery.SchemaField("event_name", "STRING"),
    bigquery.SchemaField("amount", "FLOAT64"),
    bigquery.SchemaField("labels", "STRING", mode="REPEATED"),
]

#: The seven columns the contract query projects, in order.
_VIOLATION_COLUMNS = [
    "field_name",
    "drift_type",
    "bad_count",
    "total_count",
    "threshold",
    "bad_rate",
    "sample_value",
]


class _Row:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def values(self) -> tuple[object, ...]:
        return self._values


class _Field:
    def __init__(self, name: str) -> None:
        self.name = name


class _Result:
    def __init__(self, rows: list[tuple[object, ...]], schema: list[object]) -> None:
        self._rows = rows
        self.schema = schema

    def __iter__(self) -> Iterator[_Row]:
        return iter(_Row(row) for row in self._rows)


class _Job:
    def __init__(self, rows: list[tuple[object, ...]], schema: list[object]) -> None:
        self._rows = rows
        self._schema = schema

    def result(self, **_kwargs: object) -> _Result:
        return _Result(self._rows, self._schema)


class _Client:
    """Answers the LIMIT 0 schema probe; hands the contract query canned violation rows."""

    def __init__(self, violation_rows: list[tuple[object, ...]] | None = None) -> None:
        self.sql: list[str] = []
        self._violation_rows = violation_rows or []

    def query(self, sql: str) -> _Job:
        self.sql.append(sql)
        if sql.endswith("LIMIT 0"):
            return _Job([], list(_SCHEMA))
        return _Job(self._violation_rows, [_Field(name) for name in _VIOLATION_COLUMNS])


def _adapter(
    violation_rows: list[tuple[object, ...]] | None = None,
) -> tuple[BigQueryAdapter, _Client]:
    client = _Client(violation_rows)
    adapter = object.__new__(BigQueryAdapter)
    adapter._client = client  # type: ignore[assignment]
    adapter._project = "proj"
    adapter._dataset = "wh"
    adapter._allowed_columns = set()
    adapter._column_types = {}
    adapter._struct_paths = {}
    adapter._repeated_columns = set()
    return adapter, client


def _is_probe(sql: str) -> bool:
    """Statements that are not the contract query.

    The ``LIMIT 0`` schema probe, and the regex probe a ``regex_violation``
    expectation runs first: that one offers the pattern to RE2 over no table, so a
    pattern this engine refuses costs its own expectation instead of the whole
    scan. Neither reads a row of the source.
    """
    return sql.endswith("LIMIT 0") or sql.startswith("SELECT REGEXP_CONTAINS('', ")


def _contract_sql(client: _Client) -> str:
    """The contract statement — i.e. the one that is not a probe."""
    statements = [sql for sql in client.sql if not _is_probe(sql)]
    assert len(statements) == 1, f"expected exactly one contract query, got {len(statements)}"
    return statements[0]


def _validate(
    expectations: list[FieldContractExpectation],
    *,
    rows: list[tuple[object, ...]] | None = None,
    **kwargs: object,
) -> tuple[list[FieldContractViolation], _Client]:
    adapter, client = _adapter(rows)
    violations = adapter.validate_field_contracts(
        BASE,
        expectations,
        time_column="ts",
        time_from=FROM_TIME,
        time_to=TO_TIME,
        **kwargs,  # type: ignore[arg-type]
    )
    return violations, client


# --- the whole point: no sampling, no LIMIT on what is evaluated --------------


def test_contracts_are_evaluated_warehouse_side_not_from_sampled_rows() -> None:
    # The fallback's tell is a `SELECT * ... LIMIT 50000` preview it then loops over in
    # Python. If BigQuery ever emits one of those again for a contract scan, the 50,001st
    # row is invisible again. It must aggregate instead.
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            )
        ]
    )

    sql = _contract_sql(client)
    assert "COUNTIF(" in sql
    assert "SELECT * FROM (SELECT * FROM events) AS _src" not in sql
    # `limit` bounds the violation ROWS returned, never the rows scanned.
    assert "LIMIT 50000" in sql
    assert sql.index("LIMIT 50000") > sql.index("COUNTIF(")


def test_one_job_scans_the_source_once_for_every_expectation() -> None:
    # ClickHouse UNION ALLs one aggregate subquery per expectation: one job, but N scans
    # of base_query. BigQuery bills by bytes scanned, so N scans is an N-times bill. All
    # the aggregates must therefore ride in a SINGLE pass over the source.
    expectations = [
        FieldContractExpectation(
            field_name="event_name", drift_type="required_null_violation", threshold=0.0
        ),
        FieldContractExpectation(
            field_name="event_name",
            drift_type="enum_violation",
            threshold=0.05,
            enum_options=("click", "view"),
        ),
        FieldContractExpectation(
            field_name="amount", drift_type="range_violation", threshold=0.1, min_value=0.0
        ),
    ]
    _violations, client = _validate(expectations)

    sql = _contract_sql(client)
    assert sql.count(f"({BASE}) AS _src") == 1, "the source must be scanned exactly once"
    assert "UNION ALL" not in sql
    # ...and all three expectations still made it into that one pass.
    for index in range(3):
        assert f"AS _bad_{index}" in sql
        assert f"AS _total_{index}" in sql


# --- NULL handling, matched term-for-term to ClickHouse -----------------------


def test_required_null_counts_nulls_in_the_denominator() -> None:
    # For required-ness the NULL *is* the violation, so NULLs belong in `total`:
    # ClickHouse uses count(), and the Python fallback increments total_count BEFORE it
    # tests for None. COUNT(*) is the BigQuery spelling.
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            )
        ]
    )

    sql = _contract_sql(client)
    assert "COUNTIF(`event_name` IS NULL) AS _bad_0" in sql
    assert "COUNT(*) AS _total_0" in sql
    # The sample the fallback records for a null violation is the literal <NULL>.
    assert "MIN(IF(`event_name` IS NULL, '<NULL>', NULL)) AS _sample_0" in sql


@pytest.mark.parametrize(
    ("expectation", "field"),
    [
        (
            FieldContractExpectation(
                field_name="event_name",
                drift_type="enum_violation",
                threshold=0.05,
                enum_options=("click",),
            ),
            "event_name",
        ),
        (
            FieldContractExpectation(
                field_name="event_name",
                drift_type="regex_violation",
                threshold=0.05,
                regex="^ok$",
            ),
            "event_name",
        ),
        (
            FieldContractExpectation(
                field_name="amount",
                drift_type="range_violation",
                threshold=0.05,
                min_value=0.0,
            ),
            "amount",
        ),
    ],
)
def test_enum_regex_and_range_exclude_nulls_from_the_denominator(
    expectation: FieldContractExpectation, field: str
) -> None:
    # A NULL is no evidence either way for these three, so it is excluded from `total`
    # entirely — ClickHouse uses countIf(NOT isNull(c)), and the fallback `continue`s on
    # a None *before* incrementing total_count. Counting nulls in the denominator here
    # would dilute every bad_rate by the null rate and quietly push real violations back
    # under their threshold.
    _violations, client = _validate([expectation])

    sql = _contract_sql(client)
    assert f"COUNTIF(`{field}` IS NOT NULL) AS _total_0" in sql
    assert "COUNT(*) AS _total_0" not in sql
    # ...and a NULL is not "bad" either: every bad condition is gated on presence.
    assert f"COUNTIF(`{field}` IS NOT NULL AND " in sql


# --- per-drift-type semantics -------------------------------------------------


def test_regex_uses_partial_match_like_the_fallback() -> None:
    # GoogleSQL's REGEXP_CONTAINS is a PARTIAL match, which is what ClickHouse's match()
    # and the fallback's regex.search() are. REGEXP_FULL_MATCH is the anchored one and
    # would silently turn every unanchored contract into a violation for every row.
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name",
                drift_type="regex_violation",
                threshold=0.05,
                regex=r"^u\d+$",
            )
        ]
    )

    sql = _contract_sql(client)
    assert "REGEXP_CONTAINS(" in sql
    assert "REGEXP_FULL_MATCH" not in sql
    # The pattern is a quoted literal whose backslashes survive BigQuery's own string
    # unescaping: '^u\\d+$' decodes back to ^u\d+$.
    assert r"'^u\\d+$'" in sql
    assert "NOT REGEXP_CONTAINS" in sql


def test_range_treats_a_malformed_number_as_bad() -> None:
    # The fallback marks a value that float() cannot parse as a violation, and ClickHouse
    # counts isNull(toFloat64OrNull(...)) as one. A non-numeric value must therefore be
    # BAD here, not skipped: SAFE_CAST returns NULL where CAST would abort the query.
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="amount",
                drift_type="range_violation",
                threshold=0.05,
                min_value=1.0,
                max_value=10.0,
            )
        ]
    )

    sql = _contract_sql(client)
    numeric = "SAFE_CAST(IFNULL(CAST(`amount` AS STRING), '') AS FLOAT64)"
    assert f"{numeric} IS NULL" in sql, "a malformed value must be counted as a violation"
    assert f"{numeric} < 1.0" in sql
    assert f"{numeric} > 10.0" in sql
    # Plain CAST would raise and fail the whole scan on the first junk value.
    assert "SAFE_CAST" in sql


def test_range_with_only_one_bound_does_not_invent_the_other() -> None:
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="amount", drift_type="range_violation", threshold=0.0, min_value=1.0
            )
        ]
    )

    sql = _contract_sql(client)
    assert "< 1.0" in sql
    assert " > " not in sql.split("AS _bad_0")[0].split("COUNTIF(")[-1]


def test_enum_options_are_quoted_not_interpolated() -> None:
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name",
                drift_type="enum_violation",
                threshold=0.0,
                enum_options=("click", "o'brien"),
            )
        ]
    )

    sql = _contract_sql(client)
    assert r"NOT IN ('click', 'o\'brien')" in sql


def test_bad_rate_uses_safe_divide() -> None:
    # GoogleSQL's `/` RAISES on a zero denominator ("zero divided error" — the emulator
    # says so), and SQL makes no promise that the `total_count > 0` guard is evaluated
    # first. ClickHouse gets away with a bare division because it yields nan there;
    # BigQuery would fail the entire scan.
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.25
            )
        ]
    )

    sql = _contract_sql(client)
    assert "SAFE_DIVIDE(_agg._bad_0, _agg._total_0)" in sql
    assert "SAFE_DIVIDE(_c.bad_count, _c.total_count) > _c.threshold" in sql
    assert "_bad_0 / _agg._total_0" not in sql
    # Threshold filtering happens warehouse-side: a passing contract never crosses the wire.
    assert "_c.total_count > 0 AND _c.bad_count > 0" in sql
    assert "CAST(0.25 AS FLOAT64) AS threshold" in sql


# --- window and grouped-event filter ------------------------------------------


def test_the_time_window_is_honored_and_half_open() -> None:
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            )
        ]
    )

    sql = _contract_sql(client)
    assert "`ts` >= TIMESTAMP '2026-04-01 00:00:00.000000+00:00'" in sql
    assert "`ts` < TIMESTAMP '2026-04-08 00:00:00.000000+00:00'" in sql


def test_the_group_filter_is_anded_into_the_same_scan() -> None:
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="amount", drift_type="range_violation", threshold=0.0, min_value=0.0
            )
        ],
        group_column="event_name",
        group_value="click",
    )

    sql = _contract_sql(client)
    # Compared against the NULL-collapsed STRING rendering, exactly as ClickHouse does it
    # and as the fallback's `"" if raw is None else str(raw)` does it.
    assert "IFNULL(CAST(`event_name` AS STRING), '') = 'click'" in sql
    assert "`ts` >= TIMESTAMP" in sql and " AND " in sql


def test_a_null_group_value_matches_the_empty_string_group() -> None:
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="amount", drift_type="range_violation", threshold=0.0, min_value=0.0
            )
        ],
        group_column="event_name",
        group_value=None,
    )

    assert "IFNULL(CAST(`event_name` AS STRING), '') = ''" in _contract_sql(client)


def test_an_unknown_group_column_is_refused() -> None:
    adapter, _client = _adapter()
    with pytest.raises(ValueError, match="not found"):
        adapter.validate_field_contracts(
            BASE,
            [
                FieldContractExpectation(
                    field_name="event_name", drift_type="required_null_violation", threshold=0.0
                )
            ],
            time_column="ts",
            time_from=FROM_TIME,
            time_to=TO_TIME,
            group_column="nope",
            group_value="x",
        )


# --- what must NOT become SQL --------------------------------------------------


@pytest.mark.parametrize(
    "inert",
    [
        # An enum contract with no options, a regex contract with no pattern and a range
        # contract with neither bound say nothing about the data. ClickHouse returns None
        # for each rather than compiling a term that can never be violated.
        FieldContractExpectation(field_name="event_name", drift_type="enum_violation", threshold=0),
        FieldContractExpectation(
            field_name="event_name", drift_type="regex_violation", threshold=0
        ),
        FieldContractExpectation(field_name="amount", drift_type="range_violation", threshold=0),
        FieldContractExpectation(
            field_name="event_name", drift_type="not_a_drift_type", threshold=0
        ),
    ],
)
def test_an_inert_expectation_generates_no_query_at_all(inert: FieldContractExpectation) -> None:
    adapter, client = _adapter()

    assert adapter.validate_field_contracts(BASE, [inert]) == []
    assert [sql for sql in client.sql if not sql.endswith("LIMIT 0")] == []


def test_no_expectations_touches_the_warehouse_not_at_all() -> None:
    adapter, client = _adapter()

    assert adapter.validate_field_contracts(BASE, []) == []
    assert client.sql == []


def test_a_contract_on_a_dropped_column_is_skipped_not_fatal() -> None:
    # A stale contract naming a column the source no longer selects must not take the
    # other contracts down with it — the fallback simply skips it (`field_index is None`).
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="was_removed",
                drift_type="enum_violation",
                threshold=0.0,
                enum_options=("a",),
            ),
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            ),
        ]
    )

    sql = _contract_sql(client)
    assert "was_removed" not in sql
    assert "`event_name` IS NULL" in sql


def test_enum_on_a_repeated_column_is_refused_before_it_becomes_sql() -> None:
    """Still refused before it becomes SQL — but it now costs one expectation.

    ``CAST(<array> AS STRING)`` is not a legal GoogleSQL cast, so this would compile
    to SQL that only fails once a worker runs it. That has not changed. What changed
    is the blast radius: this used to RAISE out of ``validate_field_contracts``, and
    the caller is a worker replaying contracts a user declared long ago —
    ``schema_drift`` calls it bare and ``catalog_sync`` once per event-type group —
    so one stale contract on a column that has since become an ARRAY ended the whole
    collection. The refusal is per-expectation now, and the contracts beside it still
    run.

    ``_string_value_expression`` keeps raising for a breakdown column, where the
    caller is still CHOOSING the column and a loud failure is the right answer; that
    half is pinned in test_bigquery_nested_grouping.py. The cross-engine statement of
    the skip rule, and the warning it has to leave behind, are in test_warehouse_engine_parity.py.
    """
    adapter, client = _adapter()
    adapter.validate_field_contracts(
        BASE,
        [
            FieldContractExpectation(
                field_name="labels",
                drift_type="enum_violation",
                threshold=0.0,
                enum_options=("a",),
            ),
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            ),
        ],
    )
    sql = _contract_sql(client)
    assert "labels" not in sql
    assert sql.count("AS _bad_") == 1
    # The survivor keeps its ORIGINAL position (``_bad_1``): a declined expectation
    # contributes no columns, it does not renumber the ones beside it. Harmless on
    # this engine — BigQuery names the field and drift type inside the STRUCT it
    # emits, so nothing decodes by position — and asserting the index rather than
    # just the COUNTIF is what would catch a "tidy" renumbering that moved the
    # alias scheme away from the one PostgreSQL and ClickHouse decode positionally.
    assert "COUNTIF(`event_name` IS NULL) AS _bad_1" in sql


def test_required_null_on_a_repeated_column_still_works() -> None:
    # Required-ness is pure NULL logic and needs no STRING rendering, so it is legal on a
    # column the adapter refuses to stringify.
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="labels", drift_type="required_null_violation", threshold=0.0
            )
        ]
    )

    assert "COUNTIF(`labels` IS NULL) AS _bad_0" in _contract_sql(client)


# --- decoding the result ------------------------------------------------------


def test_violation_rows_are_decoded_into_the_shared_contract() -> None:
    rows: list[tuple[object, ...]] = [
        ("event_name", "required_null_violation", 7, 100, 0.05, 0.07, "<NULL>"),
        ("amount", "range_violation", 3, 90, 0.0, 0.03333333333333333, "-1"),
    ]
    violations, _client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.05
            ),
            FieldContractExpectation(
                field_name="amount", drift_type="range_violation", threshold=0.0, min_value=0.0
            ),
        ],
        rows=rows,
    )

    assert violations == [
        FieldContractViolation(
            field_name="event_name",
            drift_type="required_null_violation",
            bad_count=7,
            total_count=100,
            bad_rate=0.07,
            threshold=0.05,
            sample_value="<NULL>",
        ),
        FieldContractViolation(
            field_name="amount",
            drift_type="range_violation",
            bad_count=3,
            total_count=90,
            bad_rate=0.03333333333333333,
            threshold=0.0,
            sample_value="-1",
        ),
    ]


def test_a_missing_sample_value_stays_none() -> None:
    violations, _client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            )
        ],
        rows=[("event_name", "required_null_violation", 1, 10, 0.0, 0.1, None)],
    )

    assert violations[0].sample_value is None


def test_violations_come_back_in_expectation_order() -> None:
    # UNNEST makes no promise about array order, so the query pins it with WITH OFFSET.
    _violations, client = _validate(
        [
            FieldContractExpectation(
                field_name="event_name", drift_type="required_null_violation", threshold=0.0
            ),
            FieldContractExpectation(
                field_name="amount", drift_type="range_violation", threshold=0.0, min_value=0.0
            ),
        ]
    )

    sql = _contract_sql(client)
    assert "WITH OFFSET AS _ord" in sql
    assert "ORDER BY _ord" in sql
