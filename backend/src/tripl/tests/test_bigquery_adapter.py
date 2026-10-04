"""The BigQuery adapter, the worker's curated-error set, and the
BigQuery half of the data-source schema.

Four defects, three modules:

* ``_query_rows`` handed the ``_bucket`` cell through exactly as
  ``google-cloud-bigquery`` decoded it, so a DATE time column produced a
  ``datetime.date`` bucket and a DATETIME one a naive ``datetime``. The consumers
  compare that bucket against an aware window bound and store it in a
  ``timestamptz`` column: the first is a ``TypeError``, the second is a silent
  timezone-dependent write. ``BigQueryAdapter._utc_bucket_rows`` now normalizes
  column 0 on every bucketed read path.
* the DATE-column and TIME-column rejections were bare
  ``ValueError``s, which ``worker.tasks._errors.user_facing_error`` replaces with
  "Scan failed due to an internal error.". They are ``WarehouseCapabilityError``
  now, and that type is admitted to ``_CURATED_ERRORS``.
* ``_quote_string`` escaped the backslash and the quote but not
  the line terminators, and GoogleSQL reads a raw newline inside a quoted literal
  as an "Unclosed string literal".
* the allowlist write path accepted 50 datasets and the schema
  browse covered 20, silently. One constant now, and the message says why.

No warehouse, no database and no Celery app: every test here drives the real
``BigQueryAdapter`` through ``object.__new__`` and a client that only records SQL,
which is the established shape for this adapter — see
``tests/test_bigquery_nested_grouping.py`` and the ZetaSQL gate in
``tests/conformance/test_bigquery_analysis.py``.

``tripl.worker.tasks._errors`` is imported directly and on purpose:
``worker/tasks/__init__.py`` is empty and that module's own imports are confined to
``tripl.core``, so it carries none of the ``celery_app`` import cycle.
``tests/test_name_format_errors.py`` already enters the same way, for the same
curated-error question.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from tripl.core.adapters.base import AggregateSpec, FieldContractExpectation
from tripl.core.adapters.bigquery import (
    _MAX_SCHEMA_DATASETS,
    BigQueryAdapter,
    _as_utc_bucket,
)
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.models.domain_enums import MetricAggregation
from tripl.schemas.data_source import (
    MAX_SCHEMA_DATASETS,
    BigQuerySettings,
    ConnectionSettingsError,
    parse_connection_settings,
)
from tripl.worker.tasks._errors import user_facing_error

GENERIC = "Scan failed due to an internal error."

_BASE = "SELECT ts, dt, d, tm, event_name, amount FROM events"
# Aware, because a real caller's window comes from ``datetime.now(UTC)`` — that is
# the whole point of that fix and a naive fixture here would hide it.
_FROM = datetime(2026, 4, 1, tzinfo=UTC)
_TO = datetime(2026, 4, 3, tzinfo=UTC)

# One column per GoogleSQL time family, plus the family the adapter refuses.
_TYPES = {
    "ts": "TIMESTAMP",
    "dt": "DATETIME",
    "d": "DATE",
    "tm": "TIME",
    "event_name": "STRING",
    "amount": "FLOAT64",
}


class _Row:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def values(self) -> tuple[object, ...]:
        return self._values


class _Result:
    schema: list[object] = []

    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[_Row]:
        return iter(_Row(row) for row in self._rows)


class _Job:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def result(self, **_kwargs: object) -> _Result:
        return _Result(self._rows)


class _Client:
    """Records every statement; answers the top-values probe separately.

    The probe is the one query whose *result* becomes part of the next statement
    (it is the ``IN (...)`` list that drives Other-folding), so it is the hook for
    driving a warehouse-supplied value into generated SQL. Identified by its
    ``__bd_raw_`` aliases, exactly as the ZetaSQL gate's client does.
    """

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rows: list[tuple[object, ...]] = []
        self.top_values: list[tuple[object, ...]] = []

    def query(self, sql: str) -> _Job:
        self.sql.append(sql)
        if "__bd_raw_" in sql:
            return _Job(self.top_values)
        return _Job(self.rows)

    def close(self) -> None:
        return None


def _bq() -> tuple[BigQueryAdapter, _Client]:
    """The real adapter, wired to a capturing client.

    ``__init__`` is bypassed because it builds a live ``bigquery.Client`` from
    service-account credentials. The declared types are seeded directly so
    ``_ensure_column_types`` finds them and no schema probe is issued; everything
    downstream of that — bucket family, window literal, row decoding — is the real
    adapter's own.
    """
    client = _Client()
    adapter = object.__new__(BigQueryAdapter)
    adapter._client = client  # type: ignore[assignment]
    adapter._project = "tripl-test"
    adapter._dataset = "wh"
    adapter._allowed_columns = set(_TYPES)
    adapter._column_types = dict(_TYPES)
    adapter._struct_paths = {}
    adapter._repeated_columns = set()
    return adapter, client


# --------------------------------------------------------------------------- #
# the bucket column is an aware UTC datetime on every read path
# --------------------------------------------------------------------------- #


def test_a_date_bucket_comes_back_as_an_aware_utc_datetime() -> None:
    """The DATE family: ``google-cloud-bigquery`` decodes it to ``datetime.date``.

    Midnight UTC of that day is the only reading consistent with the bucket
    contract, which anchors every interval at the epoch in UTC.
    """
    adapter, client = _bq()
    client.rows = [(date(2026, 4, 2), 3)]

    _cols, _values, rows = adapter.get_time_bucketed_counts(
        _BASE, "d", "1d", [], [], None, _FROM, _TO
    )

    assert rows == [(datetime(2026, 4, 2, tzinfo=UTC), 3)]


def test_a_datetime_bucket_keeps_its_time_of_day_and_gains_utc() -> None:
    """The DATETIME family: decoded by ``strptime``, so naive.

    The time-of-day assertion is the guard on the ``isinstance`` ORDER inside
    ``_as_utc_bucket``: ``datetime`` is a subclass of ``date``, so testing ``date``
    first would rebuild this bucket from its date part and land it on midnight
    while still satisfying every "is it aware?" check.
    """
    adapter, client = _bq()
    client.rows = [(datetime(2026, 4, 2, 10, 30), 3)]

    _cols, _values, rows = adapter.get_time_bucketed_counts(
        _BASE, "dt", "1h", [], [], None, _FROM, _TO
    )

    assert rows == [(datetime(2026, 4, 2, 10, 30, tzinfo=UTC), 3)]


def test_an_already_aware_timestamp_bucket_is_unchanged() -> None:
    """The negative control: the TIMESTAMP family already satisfies the contract.

    Not revert-detecting — it passes without the fix too. It is here to catch the
    opposite mistake, a normalization that shifts or re-floors a bucket that was
    already correct, which would move every existing TIMESTAMP deployment's data.
    """
    adapter, client = _bq()
    bucket = datetime(2026, 4, 2, 10, tzinfo=UTC)
    client.rows = [(bucket, 3)]

    _cols, _values, rows = adapter.get_time_bucketed_counts(
        _BASE, "ts", "1h", [], [], None, _FROM, _TO
    )

    assert rows == [(bucket, 3)]


def test_a_date_bucket_is_comparable_against_the_chunk_window() -> None:
    """The production failure itself, not a proxy for it.

    ``metric_rows`` does ``if bucket < time_from or bucket >= time_to`` against a
    bound that is aware by construction (``floor_to_bucket(datetime.now(UTC), ...)``).
    A ``date`` or a naive ``datetime`` on the left is a ``TypeError`` — the same one
    ``tests/conformance/test_pipeline_conformance.py`` records for the sibling path
    earlier. Revert ``_utc_bucket_rows`` and the comparison below raises
    instead of returning a bool.
    """
    adapter, client = _bq()
    client.rows = [(date(2026, 4, 2), 3)]

    _cols, _values, rows = adapter.get_time_bucketed_counts(
        _BASE, "d", "1d", [], [], None, _FROM, _TO
    )

    bucket = rows[0][0]
    assert isinstance(bucket, datetime)
    assert _FROM <= bucket < _TO


def test_every_bucketed_read_path_normalizes_column_zero() -> None:
    """All six methods that emit a ``_bucket``, in one test on purpose.

    Fixing this method by method is how a path gets skipped: the finding as filed
    named four read paths, and ``get_time_bucketed_multi_aggregate`` and
    ``..._multi_aggregate_breakdown`` also select ``{bucket_expr} AS _bucket`` as
    column 0 and feed ``metric_collect``'s batched fact path.
    ``get_time_bucketed_breakdown_counts`` is absent deliberately — it re-projects
    ``..._multi``'s rows and has its own test below.
    """
    naive = datetime(2026, 4, 2, 10, 30)
    aware = datetime(2026, 4, 2, 10, 30, tzinfo=UTC)
    specs = [AggregateSpec(key="c", aggregation=MetricAggregation.count)]

    adapter, client = _bq()
    client.rows = [(naive, 7)]
    assert adapter.get_time_bucketed_counts(_BASE, "dt", "1h", [], [], None, _FROM, _TO)[2] == [
        (aware, 7)
    ]

    adapter, client = _bq()
    client.rows = [(naive, 7)]
    assert adapter.get_time_bucketed_aggregate(
        _BASE, "dt", "1h", MetricAggregation.count, None, [], [], None, _FROM, _TO
    )[2] == [(aware, 7)]

    adapter, client = _bq()
    # Layout: (_bucket, _breakdown_value, _is_other, col1, ..., aggregate). The
    # breakdown column must also be one of the regular columns — the adapter rejects
    # a breakdown that is not a selected scalar.
    client.rows = [(naive, "click", 0, "click", 7)]
    assert adapter.get_time_bucketed_aggregate_breakdown(
        _BASE,
        "dt",
        "1h",
        MetricAggregation.count,
        None,
        "event_name",
        ["event_name"],
        [],
        None,
        _FROM,
        _TO,
    )[2] == [(aware, "click", 0, "click", 7)]

    adapter, client = _bq()
    client.rows = [(naive, 7)]
    assert adapter.get_time_bucketed_multi_aggregate(_BASE, "dt", "1h", specs, _FROM, _TO)[1] == [
        (aware, 7)
    ]

    adapter, client = _bq()
    client.rows = [(naive, "click", 0, 7)]
    assert adapter.get_time_bucketed_multi_aggregate_breakdown(
        _BASE, "dt", "1h", "event_name", specs, _FROM, _TO
    )[1] == [(aware, "click", 0, 7)]

    adapter, client = _bq()
    # Layout: (_bucket, _breakdown_column, _breakdown_value, _is_other, col, count).
    client.rows = [(naive, "event_name", "click", 0, "click", 7)]
    assert adapter.get_time_bucketed_breakdown_counts_multi(
        _BASE, "dt", "1h", ["event_name"], ["event_name"], [], None, _FROM, _TO
    )[2] == [(aware, "event_name", "click", 0, "click", 7)]


def test_breakdown_counts_inherits_the_normalized_bucket_from_multi() -> None:
    """The one bucketed method with no normalize call of its own, pinned.

    It re-projects ``..._multi``'s rows and drops column 1; if it ever stops
    delegating, this goes red rather than the defect coming back silently.
    """
    adapter, client = _bq()
    client.rows = [(datetime(2026, 4, 2, 10, 30), "event_name", "click", 0, "click", 7)]

    _cols, _values, rows = adapter.get_time_bucketed_breakdown_counts(
        _BASE, "dt", "1h", "event_name", ["event_name"], [], None, _FROM, _TO
    )

    assert rows == [(datetime(2026, 4, 2, 10, 30, tzinfo=UTC), "click", 0, "click", 7)]


def test_a_non_date_bucket_cell_is_left_alone() -> None:
    """``_as_utc_bucket`` coerces dates and nothing else.

    Several existing adapter tests drive these methods with an integer in column 0
    (``tests/test_bigquery_nested_grouping.py`` uses ``tuple(range(offset))`` as the
    lead), so "leave anything that is not a date untouched" is load-bearing for the
    suite as well as for diagnosing a row layout that has shifted.
    """
    assert _as_utc_bucket(0) == 0
    assert _as_utc_bucket(None) is None
    assert _as_utc_bucket("2026-04-02") == "2026-04-02"


def test_normalizing_an_empty_rowset_is_not_an_error() -> None:
    """A bucketed query over a window with no rows is ordinary, not exceptional."""
    adapter, client = _bq()
    client.rows = []

    _cols, _values, rows = adapter.get_time_bucketed_counts(
        _BASE, "d", "1d", [], [], None, _FROM, _TO
    )

    assert rows == []


# --------------------------------------------------------------------------- #
# the rejection has to reach the operator, not just the log
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", ["15m", "1h", "6h"])
def test_date_column_sub_day_rejection_is_a_warehouse_capability_error(code: str) -> None:
    """The type is the fix: ``user_facing_error`` curates by type, never by text."""
    adapter, _client = _bq()

    with pytest.raises(WarehouseCapabilityError, match="no time-of-day"):
        adapter._bucket_expression("d", code)


def test_time_column_rejection_is_a_warehouse_capability_error() -> None:
    adapter, _client = _bq()

    with pytest.raises(WarehouseCapabilityError, match="carries no date"):
        adapter._time_kind("tm")


def test_a_warehouse_capability_error_is_still_a_value_error() -> None:
    """``core.adapters.errors`` promises this so existing ``except ValueError``
    handlers keep working; both rejections above used to be bare ``ValueError``s
    and callers were written against that."""
    assert issubclass(WarehouseCapabilityError, ValueError)


def test_user_facing_error_surfaces_a_warehouse_capability_error_verbatim() -> None:
    """The assertion that pins the user-visible half.

    Revert the ``_CURATED_ERRORS`` widening and this returns the generic summary:
    the operator is told a scan failed and nothing about which setting to change.
    """
    exc = WarehouseCapabilityError(
        "BigQuery: time column 'd' is a DATE, which has no time-of-day, so it "
        "cannot be bucketed at '1h'. Use the 1d or 1w interval, or a "
        "TIMESTAMP/DATETIME column."
    )

    surfaced = user_facing_error(exc)

    assert surfaced.startswith("Scan failed")
    assert "no time-of-day" in surfaced
    assert surfaced != GENERIC


def test_a_bare_value_error_with_the_same_text_is_still_sanitised() -> None:
    """The widening is by type, not by text — the same guard
    ``test_name_format_errors`` puts on the ``NameFormatError`` admission."""
    exc = ValueError(
        "BigQuery: time column 'd' is a DATE, which has no time-of-day, so it "
        "cannot be bucketed at '1h'."
    )

    assert user_facing_error(exc) == GENERIC


def test_a_collection_read_on_a_date_column_at_an_hourly_interval_explains_itself() -> None:
    """Both halves composed, on the path the operator actually hits.

    Nothing configuration-time compares the interval against the column's declared
    type, so a config saved as ``1h`` over a DATE column first fails inside a
    collection tick. Revert either the raise type or the curated set and this drops
    back to the generic sentence.
    """
    adapter, _client = _bq()

    with pytest.raises(WarehouseCapabilityError) as excinfo:
        adapter.get_time_bucketed_counts(_BASE, "d", "1h", [], [], None, _FROM, _TO)

    surfaced = user_facing_error(excinfo.value)
    assert surfaced.startswith("Scan failed")
    assert "no time-of-day" in surfaced
    assert "'1h'" in surfaced


def test_the_curated_messages_carry_no_connection_details() -> None:
    """Widening ``_CURATED_ERRORS`` moved this adapter's messages onto a verbatim
    path, so "the message is tripl-authored and carries nothing sensitive" stopped
    being documentation and became a guarantee. Pinned for the two messages this
    change promoted; the other eleven raise sites were read, not tested."""
    adapter, _client = _bq()
    adapter._project = "secret-project"
    adapter._dataset = "secret-dataset"

    messages: list[str] = []
    with pytest.raises(WarehouseCapabilityError) as date_exc:
        adapter._bucket_expression("d", "1h")
    messages.append(str(date_exc.value))
    with pytest.raises(WarehouseCapabilityError) as time_exc:
        adapter._time_kind("tm")
    messages.append(str(time_exc.value))

    for message in messages:
        assert "secret-project" not in message
        assert "secret-dataset" not in message


# --------------------------------------------------------------------------- #
# a GoogleSQL quoted literal may not contain a line terminator
# --------------------------------------------------------------------------- #


def test_quote_string_escapes_the_line_terminators() -> None:
    """A raw newline inside a single-quoted GoogleSQL literal is an "Unclosed
    string literal" — the statement does not parse, so the chunk and the run fail
    for as long as that value is in the data."""
    adapter, _client = _bq()

    assert adapter._quote_string("line1\nline2") == "'line1\\nline2'"
    assert adapter._quote_string("line1\rline2") == "'line1\\rline2'"
    assert "\n" not in adapter._quote_string("line1\nline2")
    assert "\r" not in adapter._quote_string("line1\rline2")


def test_quote_string_still_escapes_the_backslash_and_the_quote() -> None:
    """GoogleSQL has no ``''`` escape; the escape character is the backslash."""
    adapter, _client = _bq()

    assert adapter._quote_string("o'brien") == "'o\\'brien'"
    assert adapter._quote_string("c:\\tmp") == "'c:\\\\tmp'"


def test_quote_string_doubles_the_backslash_before_anything_else() -> None:
    """The ordering guard. Not revert-detecting — the old two-replacement version
    passes it too — but it is the one way to get the NEW code wrong: escaping the
    newline before doubling the backslash turns a literal ``\\n`` in the data into
    an escaped newline, silently changing the value.
    """
    adapter, _client = _bq()

    # The input is the four characters a \ n b. The backslash is doubled and the n
    # stays a plain letter; it must not be read as the escape for a newline.
    assert adapter._quote_string("a\\nb") == "'a\\\\nb'"
    # A value ending in a backslash must not escape the closing quote.
    assert adapter._quote_string("trailing\\") == "'trailing\\\\'"


def test_quote_string_does_not_strip_or_reject_its_input() -> None:
    """The regression guard on NOT delegating to ``quote_sql_string_literal``.

    That helper ``strip()``s and raises on the empty string, and both are wrong
    here: a breakdown value's surrounding whitespace is part of the group key, and
    the empty string is the NULL-collapsed group value ``_contract_where_clause``
    compares against. Also not revert-detecting; it goes red if someone later
    "simplifies" the two into one.
    """
    adapter, _client = _bq()

    assert adapter._quote_string("") == "''"
    assert adapter._quote_string(" a ") == "' a '"
    assert adapter._quote_string("a") != adapter._quote_string("a ")


def test_a_breakdown_top_value_with_a_newline_does_not_break_the_statement() -> None:
    """The reachable path: top-N values come straight out of warehouse data.

    ``_top_breakdown_values_multi`` stringifies whatever the probe returned and the
    next statement interpolates it into an ``IN (...)`` list. Nothing sanitises it
    on the way, which is why the escaping has to be complete rather than
    sufficient-for-the-common-case.
    """
    adapter, client = _bq()
    client.top_values = [("event_name", "line1\nline2"), ("event_name", "carriage\rreturn")]

    adapter.get_time_bucketed_breakdown_counts_multi(
        _BASE,
        "ts",
        "1h",
        ["event_name"],
        ["event_name"],
        [],
        None,
        _FROM,
        _TO,
        values_limit=3,
    )

    grouping_sql = client.sql[-1]
    assert "line1\\nline2" in grouping_sql
    assert "carriage\\rreturn" in grouping_sql
    # The adapter builds every statement from single-line fragments, so a raw line
    # terminator anywhere in it can only have come out of an interpolated value.
    assert "\n" not in grouping_sql
    assert "\r" not in grouping_sql


def test_a_grouped_event_value_with_a_newline_does_not_break_the_contract_scan() -> None:
    """``validate_field_contracts`` runs per grouped event value, and that value is
    an ``event_type_column`` cell from the warehouse."""
    adapter, client = _bq()
    client.rows = []

    adapter.validate_field_contracts(
        _BASE,
        [
            FieldContractExpectation(
                field_name="event_name",
                drift_type="required_null_violation",
                threshold=0.0,
            )
        ],
        group_column="event_name",
        group_value="line1\nline2",
    )

    sql = client.sql[-1]
    assert "line1\\nline2" in sql
    assert "\n" not in sql


def test_an_enum_option_with_a_newline_does_not_break_the_contract_scan() -> None:
    """Analyst-entered: ``schemas/field_definition.FieldDefinitionCreate`` declares
    ``enum_options: list[str] | None`` with no character constraint, so a pasted
    multi-line option reaches this SQL verbatim."""
    adapter, client = _bq()
    client.rows = []

    adapter.validate_field_contracts(
        _BASE,
        [
            FieldContractExpectation(
                field_name="event_name",
                drift_type="enum_violation",
                threshold=0.0,
                enum_options=("ok", "multi\nline"),
            )
        ],
    )

    sql = client.sql[-1]
    assert "multi\\nline" in sql
    assert "\n" not in sql


def test_a_contract_regex_with_a_newline_does_not_break_the_contract_scan() -> None:
    """The regex is interpolated as a string literal into ``REGEXP_CONTAINS``. A
    pattern pasted out of a multi-line editor can carry a newline, and the schema's
    only checks on ``contract_regex`` are its length and that Python can compile it
    — neither excludes one."""
    adapter, client = _bq()
    client.rows = []

    adapter.validate_field_contracts(
        _BASE,
        [
            FieldContractExpectation(
                field_name="event_name",
                drift_type="regex_violation",
                threshold=0.0,
                regex="^a\n|^b",
            )
        ],
    )

    sql = client.sql[-1]
    assert "^a\\n|^b" in sql
    assert "\n" not in sql


# --------------------------------------------------------------------------- #
# one bound for the allowlist and the browse it feeds
# --------------------------------------------------------------------------- #


def _max_accepted_allowlist() -> int:
    """The longest allowlist the validator accepts, measured rather than imported.

    Reading the module's private constant would still pass if the validator stopped
    consulting it; asking the validator itself cannot.
    """
    accepted = 0
    for count in range(1, MAX_SCHEMA_DATASETS + 5):
        try:
            BigQuerySettings.model_validate(
                {"dataset_allowlist": [f"ds{index:03d}" for index in range(count)]}
            )
        except ValidationError:
            break
        accepted = count
    return accepted


def test_the_allowlist_bound_is_derived_from_the_browse_bound() -> None:
    """Two literals (50 in the schema, 20 in the adapter) was the defect: the write
    path accepted a list the read path silently truncated. The allowlist gets one
    fewer because the connection's own default dataset always takes the first browse
    slot."""
    assert _MAX_SCHEMA_DATASETS == MAX_SCHEMA_DATASETS
    assert _max_accepted_allowlist() == MAX_SCHEMA_DATASETS - 1
    assert _max_accepted_allowlist() < _MAX_SCHEMA_DATASETS


def test_an_allowlist_that_would_be_truncated_is_rejected() -> None:
    """Revert the cap and 20 entries validate, then lose one to the browse without
    a word to anyone."""
    with pytest.raises(ValidationError):
        BigQuerySettings.model_validate(
            {"dataset_allowlist": [f"ds{index:03d}" for index in range(MAX_SCHEMA_DATASETS)]}
        )


def test_the_rejection_says_why_rather_than_just_naming_a_number() -> None:
    """The whole complaint is that the operator was given no
    explanation, so a test that only checks the status of the validation would not
    pin the fix."""
    with pytest.raises(ConnectionSettingsError) as excinfo:
        parse_connection_settings(
            "bigquery",
            {"dataset_allowlist": [f"ds{index:03d}" for index in range(MAX_SCHEMA_DATASETS)]},
        )

    message = str(excinfo.value)
    assert "default dataset" in message
    assert str(MAX_SCHEMA_DATASETS - 1) in message


def test_the_largest_accepted_allowlist_round_trips() -> None:
    """The boundary from the other side, so a later "tighten it a bit more" cannot
    pass unnoticed. Not revert-detecting on its own."""
    names = [f"ds{index:03d}" for index in range(MAX_SCHEMA_DATASETS - 1)]

    settings = BigQuerySettings.model_validate({"dataset_allowlist": names})

    assert settings.dataset_allowlist == names


def test_the_adapter_still_truncates_an_over_long_stored_allowlist() -> None:
    """The schema cap is a gate on new writes, not a statement about what is stored.

    Legacy rows, branch copies and hand-edited ``extra_params`` JSON never passed
    through the validator, so the adapter's own truncation stays the last line of
    defense — and the default dataset keeps the first slot rather than being the one
    squeezed out.
    """
    adapter, _client = _bq()
    adapter._dataset_allowlist = tuple(f"ds{index:03d}" for index in range(50))

    datasets = adapter._schema_datasets()

    assert len(datasets) == MAX_SCHEMA_DATASETS
    assert datasets[0] == "wh"
