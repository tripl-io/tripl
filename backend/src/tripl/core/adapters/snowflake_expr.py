"""Snowflake SQL expressions: time windows, buckets, nested paths and field contracts.

The statement-free half of :class:`~tripl.core.adapters.snowflake.SnowflakeAdapter`,
kept apart so the adapter module stays readable. Everything here reads only the
introspected column types (``_column_types``) and builds SQL text; nothing runs a
statement except the regex probe the adapter supplies.
"""

from __future__ import annotations

from datetime import datetime

from tripl.core.adapters.base import FieldContractExpectation, contract_bound_literal
from tripl.core.adapters.databricks_sql import IDENTIFIER_PART_RE, validate_identifier_column
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.snowflake_sql import (
    Params,
    is_ntz_type,
    is_zoned_type,
    quote_ident,
)
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


class SnowflakeExpressions:
    """SQL text for one adapter's introspected columns."""

    # Class-level defaults: the unit tests build adapters with ``object.__new__``
    # and seed only what they use.
    _allowed_columns: set[str] = set()  # noqa: RUF012 - replaced per instance, never mutated
    _column_types: dict[str, str] = {}  # noqa: RUF012 - replaced per instance, never mutated

    # ------------------------------------------------------------------ #
    # columns and time
    # ------------------------------------------------------------------ #

    def _validate_column(self, column: str) -> str:
        return validate_identifier_column(column, self._allowed_columns)

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

    def _time_condition(
        self, time_column: str | None, time_from: datetime | None, time_to: datetime | None
    ) -> str:
        if time_column is None or time_from is None or time_to is None:
            return ""
        quoted = quote_ident(self._validate_column(time_column))
        lower = self._time_literal(time_column, time_from)
        upper = self._time_literal(time_column, time_to)
        return f"{quoted} >= {lower} AND {quoted} < {upper}"

    def _time_window_where_clause(
        self, time_column: str | None, time_from: datetime | None, time_to: datetime | None
    ) -> str:
        condition = self._time_condition(time_column, time_from, time_to)
        return f" WHERE {condition}" if condition else ""

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
        parts = [part for part in path.split(".") if part]
        if not parts or any(not IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")
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

    def _field_operand(self, field: str) -> str:
        prop = split_property_field(field)
        if prop is None:
            return quote_ident(self._validate_column(field))
        return self._property_value_expression(*prop)

    def _field_value_expression(self, field: str) -> str:
        if split_property_field(field) is None:
            return self._string_value_expression(field)
        return f"COALESCE({self._field_operand(field)}, '')"

    def _validate_breakdown_field(self, field: str) -> str:
        prop = split_property_field(field)
        if prop is None:
            return self._validate_column(field)
        self._validate_column(prop[0])
        return field

    def _nested_source(
        self,
        base_query: str,
        where_clause: str,
        json_cols: list[str],
        json_value_paths: dict[str, list[str]],
    ) -> tuple[str, dict[str, str], list[str]]:
        """The FROM source with every nested expression computed once, under an alias."""
        prepared: list[str] = []
        alias_by_name: dict[str, str] = {}
        json_value_names: list[str] = []
        for index, column in enumerate(json_cols):
            alias = f"__np_{index}"
            prepared.append(f"{self._json_paths_expression(column)} AS {alias}")
            alias_by_name[column] = alias
        for column in json_cols:
            for path in json_value_paths.get(column, []):
                full_path = f"{column}.{path}"
                alias = f"__nv_{len(json_value_names)}"
                prepared.append(f"{self._json_path_expression(column, path)} AS {alias}")
                alias_by_name[full_path] = alias
                json_value_names.append(full_path)
        if not prepared:
            return f"({base_query}) AS _src{where_clause}", alias_by_name, json_value_names
        inner = f"SELECT _src.*, {', '.join(prepared)} FROM ({base_query}) AS _src{where_clause}"
        return f"({inner}) AS _prepared", alias_by_name, json_value_names

    # ------------------------------------------------------------------ #
    # field contracts
    # ------------------------------------------------------------------ #

    def _contract_where_clause(
        self,
        params: Params,
        time_column: str | None,
        time_from: datetime | None,
        time_to: datetime | None,
        group_column: str | None,
        group_value: str | None,
    ) -> str:
        conditions: list[str] = []
        window = self._time_condition(time_column, time_from, time_to)
        if window:
            conditions.append(window)
        if group_column is not None:
            expected = params.bind(group_value or "")
            conditions.append(f"{self._string_value_expression(group_column)} = {expected}")
        if not conditions:
            return ""
        return " WHERE " + " AND ".join(conditions)

    def _contract_bad_predicate(
        self,
        expectation: FieldContractExpectation,
        operand: str,
        params: Params,
        regex_ok: bool,
    ) -> str | None:
        """The predicate that makes one row BAD; ``None`` when the type has none.

        Mirrors ``PostgresAdapter._contract_bad_condition`` clause for clause:
        ``required_null`` counts NULLs, every other drift type skips them, and a
        range value that does not parse as a number is BAD.
        """
        value_expr = f"COALESCE({operand}::STRING, '')"
        present = f"{operand} IS NOT NULL"
        drift_type = expectation.drift_type
        if drift_type == "required_null_violation":
            return f"{operand} IS NULL"
        if drift_type == "enum_violation":
            options = ", ".join(params.bind(option) for option in expectation.enum_options)
            return f"{present} AND {value_expr} NOT IN ({options})"
        if drift_type == "regex_violation":
            assert expectation.regex is not None
            if not regex_ok:
                return None
            # REGEXP_LIKE matches the WHOLE string; REGEXP_INSTR finds, like re.search.
            return f"{present} AND REGEXP_INSTR({value_expr}, {params.bind(expectation.regex)}) = 0"
        if drift_type == "range_violation":
            number = f"TRY_TO_DOUBLE({value_expr})"
            outside: list[str] = []
            if expectation.min_value is not None:
                outside.append(f"{number} < {contract_bound_literal(expectation.min_value)}")
            if expectation.max_value is not None:
                outside.append(f"{number} > {contract_bound_literal(expectation.max_value)}")
            # Unparseable is BAD; NaN is never compared (Snowflake sorts it above
            # every number and makes it equal to itself, Python compares it false).
            return (
                f"{present} AND ({number} IS NULL OR "
                f"({number} <> 'NaN'::FLOAT AND ({' OR '.join(outside)})))"
            )
        return None

    def _contract_aggregate_sql(
        self, expectation: FieldContractExpectation, bad: str, *, index: int
    ) -> str:
        """``_bad_{i}``, ``_total_{i}``, ``_sample_{i}`` — the layout every engine shares."""
        operand = self._field_operand(expectation.field_name)
        is_required = expectation.drift_type == "required_null_violation"
        total = "COUNT(*)" if is_required else f"COUNT_IF({operand} IS NOT NULL)"
        sample = "'<NULL>'" if is_required else f"COALESCE({operand}::STRING, '')"
        return (
            f"COUNT_IF({bad}) AS _bad_{index}, "
            f"{total} AS _total_{index}, "
            f"MIN(IFF({bad}, {sample}, NULL)) AS _sample_{index}"
        )
