"""Snowflake SQL expressions: time windows, buckets, nested paths, text and contract tests.

The statement-free half of :class:`~tripl.core.adapters.snowflake.SnowflakeAdapter`:
how Snowflake spells what the statements in
:mod:`~tripl.core.adapters.dialect_sql_adapter` ask for. Everything here reads
only the introspected column types (``_column_types``) and builds SQL text;
nothing runs a statement.
"""

from __future__ import annotations

from datetime import datetime

from tripl.core.adapters.dialect_sql_adapter import DialectSqlAdapter
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.snowflake_sql import (
    Params,
    is_ntz_type,
    is_zoned_type,
    quote_ident,
)
from tripl.core.adapters.sql_common import json_path_parts
from tripl.core.bucketing import EPOCH, WEEK_ORIGIN, to_utc
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.core.warehouse_types import ComplexKind, TimeKind, classify_complex, classify_time
from tripl.json_paths import split_property_field

# Intervals finer than a day cannot be expressed against a DATE column.
_SUB_DAY_UNITS = (IntervalUnit.minute, IntervalUnit.hour)

# ``DATE_TRUNC`` unit per interval unit, for the intervals whose count is 1.
# Weeks are left out on purpose: Snowflake truncates a WEEK by the session's
# WEEK_START, so they go through the epoch grid anchored at a Monday instead.
_TRUNC_UNIT = {
    IntervalUnit.minute: "MINUTE",
    IntervalUnit.hour: "HOUR",
    IntervalUnit.day: "DAY",
}

_NTZ_LITERAL_FMT = "%Y-%m-%d %H:%M:%S.%f"
_DATE_LITERAL_FMT = "%Y-%m-%d"


def utc_ntz_literal(value: datetime) -> str:
    """A UTC instant as a ``TIMESTAMP_NTZ`` holding its UTC wall clock."""
    moment = to_utc(value).strftime(_NTZ_LITERAL_FMT)
    return f"TO_TIMESTAMP_NTZ('{moment}', 'YYYY-MM-DD HH24:MI:SS.FF6')"


def utc_tz_literal(value: datetime) -> str:
    """A UTC instant as a ``TIMESTAMP_TZ`` with an explicit ``+00:00`` offset."""
    moment = to_utc(value).strftime(_NTZ_LITERAL_FMT)
    return f"TO_TIMESTAMP_TZ('{moment} +00:00', 'YYYY-MM-DD HH24:MI:SS.FF6 TZH:TZM')"


def utc_date_literal(value: datetime) -> str:
    return f"TO_DATE('{to_utc(value).strftime(_DATE_LITERAL_FMT)}', 'YYYY-MM-DD')"


class SnowflakeExpressions(DialectSqlAdapter):
    """Snowflake's spelling of the shared statements, for one adapter's introspected columns."""

    engine_label = "Snowflake"

    _COUNT = "COUNT"
    _COUNT_IF = "COUNT_IF"
    _IF = "IFF"
    _MIN = "MIN"
    _ROW_NUMBER = "ROW_NUMBER"

    # ------------------------------------------------------------------ #
    # values and identifiers
    # ------------------------------------------------------------------ #

    def _new_params(self) -> Params:
        return Params()

    def _quote_ident(self, name: str) -> str:
        return quote_ident(name)

    # ------------------------------------------------------------------ #
    # columns and time
    # ------------------------------------------------------------------ #

    def _time_kind(self, time_column: str) -> TimeKind:
        type_name = self._column_types.get(time_column)
        if type_name is None:
            return TimeKind.timestamp
        kind = classify_time(type_name)
        if kind is TimeKind.unsupported:
            msg = (
                f"Snowflake: time column {time_column!r} has type {type_name}, which "
                "carries no date and cannot be used as a time column. "
                "Use a TIMESTAMP_NTZ, TIMESTAMP_LTZ, TIMESTAMP_TZ or DATE column."
            )
            raise WarehouseCapabilityError(msg)
        return kind

    def _time_literal(self, time_column: str, value: datetime) -> str:
        """A UTC instant as a literal of the column's own type family.

        ``TIMESTAMP_NTZ`` is a zone-less wall clock and gets the UTC wall clock;
        ``TIMESTAMP_LTZ`` / ``TIMESTAMP_TZ`` (and a column of unknown type) get a
        ``TIMESTAMP_TZ`` at ``+00:00``, which names the same instant whatever zone
        the stored values carry; a DATE column compares against the bound's UTC
        day (the 1d/1w windows a DATE supports are day-aligned, so that floor is
        exact).
        """
        kind = self._time_kind(time_column)
        if kind is TimeKind.date:
            return utc_date_literal(value)
        if is_ntz_type(self._column_types.get(time_column, "")):
            return utc_ntz_literal(value)
        return utc_tz_literal(value)

    def _utc_wall_clock(self, time_column: str) -> str:
        """The column as a ``TIMESTAMP_NTZ`` holding the UTC wall clock of each value.

        A zoned value is converted to UTC first: ``DATE_TRUNC`` on a
        ``TIMESTAMP_TZ`` truncates in the value's OWN offset, so a day would start
        at the stored offset's midnight rather than UTC's.
        """
        quoted = quote_ident(self._validate_column(time_column))
        type_name = self._column_types.get(time_column, "")
        if is_zoned_type(type_name):
            return f"TO_TIMESTAMP_NTZ(CONVERT_TIMEZONE('UTC', {quoted}))"
        if self._time_kind(time_column) is TimeKind.date:
            return f"TO_TIMESTAMP_NTZ({quoted})"
        return quoted

    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        """Translate an interval code into Snowflake SQL; agrees with ``floor_to_bucket``.

        A count of 1 below a week truncates the UTC wall clock; any other width
        (15m, 6h, and every week) floors the epoch seconds onto a grid anchored at
        the epoch, or at ``WEEK_ORIGIN`` (a Monday) for weeks, so no session
        parameter (WEEK_START, TIMEZONE) can move a bucket edge.
        """
        spec = get_interval(interval_code)
        kind = self._time_kind(time_column)
        if kind is TimeKind.date and spec.unit in _SUB_DAY_UNITS:
            msg = (
                f"Snowflake: time column {time_column!r} is a DATE, which has no "
                f"time-of-day, so it cannot be bucketed at {interval_code!r}. "
                "Use the 1d or 1w interval, or a TIMESTAMP column."
            )
            raise WarehouseCapabilityError(msg)
        moment = self._utc_wall_clock(time_column)
        if spec.count == 1 and spec.unit in _TRUNC_UNIT:
            return f"DATE_TRUNC('{_TRUNC_UNIT[spec.unit]}', {moment})"
        width = int(spec.delta.total_seconds())
        origin = int((WEEK_ORIGIN if spec.unit is IntervalUnit.week else EPOCH).timestamp())
        offset = f" - {origin}" if origin else ""
        restore = f" + {origin}" if origin else ""
        return (
            f"TO_TIMESTAMP_NTZ(FLOOR((DATE_PART(EPOCH_SECOND, {moment}){offset}) / {width}) "
            f"* {width}{restore})"
        )

    # ------------------------------------------------------------------ #
    # nested columns
    # ------------------------------------------------------------------ #

    def _require_document(self, column: str) -> None:
        type_name = self._column_types.get(column)
        if type_name is None:
            return
        if classify_complex(type_name) is not ComplexKind.json:
            msg = (
                f"Snowflake: column {column!r} has type {type_name} and holds no nested "
                "paths. Only VARIANT and OBJECT columns can be path-expanded (or a "
                "STRING column the scan parses as JSON)."
            )
            raise ValueError(msg)

    def _path_value(self, column: str, path: str) -> str:
        """The VARIANT at ``path`` of a document column; SQL NULL when absent.

        Bracketed keys match a key exactly, case included, the way a JSON key is
        matched on every other engine (``col:key`` notation folds nothing either,
        but brackets need no quoting rules). Every segment holds to the
        identifier grammar before it is spelled.
        """
        parts = json_path_parts(path)
        col = self._validate_column(column)
        self._require_document(col)
        return f"{quote_ident(col)}::VARIANT" + "".join(f"['{part}']" for part in parts)

    def _json_path_expression(self, column: str, path: str) -> str:
        """The value at ``path`` as JSON text — the grouped ``keep_json_value`` column."""
        return f"TO_JSON({self._path_value(column, path)})"

    def _property_value_expression(self, column: str, path: str) -> str:
        """One property as a nullable STRING: the scalar itself, NULL when absent or null.

        A JSON null is a VARIANT null, not SQL NULL, so it is turned into one
        explicitly; a string comes back without its JSON quotes.
        """
        value = self._path_value(column, path)
        return f"IFF(IS_NULL_VALUE({value}), NULL, {value}::STRING)"

    def _json_paths_expression(self, column: str) -> str:
        """A document column's top-level keys as a groupable, sorted JSON array string."""
        col = self._validate_column(column)
        self._require_document(col)
        doc = f"{quote_ident(col)}::VARIANT"
        keys = f"IFF(IS_OBJECT({doc}), OBJECT_KEYS({doc}::OBJECT), NULL)"
        return f"TO_JSON(ARRAY_SORT(COALESCE({keys}, ARRAY_CONSTRUCT())))"

    def _string_value_expression(self, column: str) -> str:
        return f"COALESCE({quote_ident(self._validate_column(column))}::STRING, '')"

    def _field_value_expression(self, field: str) -> str:
        if split_property_field(field) is None:
            return self._string_value_expression(field)
        return f"COALESCE({self._field_operand(field)}, '')"

    def _json_object_or_null(self, quoted: str) -> str:
        parsed = f"TRY_PARSE_JSON({quoted})"
        return f"IFF(IS_OBJECT({parsed}), {parsed}, NULL)"

    def _cast_text(self, expr: str) -> str:
        return f"{expr}::STRING"

    # ------------------------------------------------------------------ #
    # field contracts
    # ------------------------------------------------------------------ #

    def _contract_text(self, field: str) -> str:
        return f"COALESCE({self._cast_text(self._field_operand(field))}, '')"

    def _regex_mismatch(self, value: str, pattern: str) -> str:
        # REGEXP_LIKE matches the WHOLE string; REGEXP_INSTR finds, like re.search.
        return f"REGEXP_INSTR({value}, {pattern}) = 0"

    def _try_double(self, value: str) -> str:
        return f"TRY_TO_DOUBLE({value})"

    def _nan_guard(self, number: str) -> str:
        # Snowflake sorts NaN above every number and makes it equal to itself;
        # Python compares it false both ways, so it is never compared at all.
        return f"{number} <> 'NaN'::FLOAT"
