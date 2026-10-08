"""Trino SQL expressions: time windows, buckets, JSON paths and field contracts.

The statement-free half of :class:`~tripl.core.adapters.trino.TrinoAdapter`
(and of Athena, whose engine version 3 is Trino), kept apart so the adapter
module stays readable. Everything here reads only the introspected column types
(``_column_types``) and builds SQL text; nothing runs a statement.
"""

from __future__ import annotations

from datetime import datetime

from tripl.core.adapters.base import FieldContractExpectation, contract_bound_literal
from tripl.core.adapters.databricks_sql import IDENTIFIER_PART_RE, validate_identifier_column
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.trino_sql import (
    is_container_type,
    is_float_type,
    is_json_type,
    is_text_type,
    is_zoned_type,
    quote_ident,
    quote_literal,
    utc_date_literal,
    utc_timestamp_literal,
    utc_timestamp_tz_literal,
)
from tripl.core.bucketing import EPOCH, WEEK_ORIGIN
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.core.warehouse_types import ComplexKind, TimeKind, classify_complex, classify_time
from tripl.json_paths import split_property_field

# Intervals finer than a day cannot be expressed against a DATE column.
_SUB_DAY_UNITS = (IntervalUnit.minute, IntervalUnit.hour)

# ``date_trunc`` unit per interval unit, for the intervals whose count is 1.
# Weeks are left out on purpose: they go through the epoch grid anchored at
# WEEK_ORIGIN, so the bucket's anchor is stated rather than inherited.
_TRUNC_UNIT = {IntervalUnit.hour: "hour", IntervalUnit.day: "day"}

#: The zone-less epoch every bucket is counted from.
_EPOCH_NAIVE = "TIMESTAMP '1970-01-01 00:00:00.000'"

#: A JSON document as a map of its TOP-LEVEL keys; NULL for anything that is not
#: an object (an array, a scalar, JSON null).
_AS_OBJECT = "try_cast({doc} AS map(varchar, json))"


class TrinoExpressions:
    """SQL text for one adapter's introspected columns."""

    #: Error prefix and product name in messages; Athena overrides it.
    engine_label = "Trino"

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
                f"{self.engine_label}: time column {time_column!r} has type {type_name}, "
                "which carries no date and cannot be used as a time column. Use a "
                "timestamp, timestamp with time zone or date column."
            )
            raise WarehouseCapabilityError(msg)
        return kind

    def _time_literal(self, time_column: str, value: datetime) -> str:
        """A UTC instant as a literal of the column's own type family.

        A zone-less ``timestamp`` gets the UTC wall clock (the session zone is
        UTC, and this compares without any cast); ``timestamp with time zone``
        and a column of unknown type get an explicit ``UTC`` literal, which names
        the same instant whatever zone the stored values carry; a DATE column
        compares against the bound's UTC day (the 1d/1w windows a DATE supports
        are day-aligned, so that floor is exact).
        """
        if self._time_kind(time_column) is TimeKind.date:
            return utc_date_literal(value)
        type_name = self._column_types.get(time_column)
        if type_name is not None and not is_zoned_type(type_name):
            return utc_timestamp_literal(value)
        return utc_timestamp_tz_literal(value)

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
        """The column as a value whose calendar fields are its UTC wall clock.

        A zoned value is moved to UTC first: ``date_trunc`` on a ``timestamp
        with time zone`` truncates in the value's OWN zone, so a day would start
        at the stored zone's midnight rather than UTC's. A zone-less value
        already is a UTC wall clock, and a DATE is its own midnight.
        """
        quoted = quote_ident(self._validate_column(time_column))
        type_name = self._column_types.get(time_column, "")
        if is_zoned_type(type_name):
            return f"({quoted} AT TIME ZONE 'UTC')"
        if self._time_kind(time_column) is TimeKind.date:
            return f"CAST({quoted} AS timestamp(3))"
        return quoted

    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        """Translate an interval code into Trino SQL; agrees with ``floor_to_bucket``.

        Every bucket is a zone-less ``timestamp(3)`` holding the UTC wall clock
        (milliseconds, so pyathena's text parser reads it back too).
        1h and 1d truncate it; 15m, 6h and every week floor the whole seconds
        since the epoch onto a grid anchored at the epoch, or at ``WEEK_ORIGIN``
        (a Monday) for weeks. The value is truncated to its minute FIRST and
        counted in integer seconds (``date_diff``), so a timestamp's fraction —
        up to picoseconds in Trino — is never rounded through a ``double`` into
        the next bucket, and nothing reads the session zone.
        """
        spec = get_interval(interval_code)
        kind = self._time_kind(time_column)
        if kind is TimeKind.date and spec.unit in _SUB_DAY_UNITS:
            msg = (
                f"{self.engine_label}: time column {time_column!r} is a DATE, which has "
                f"no time-of-day, so it cannot be bucketed at {interval_code!r}. "
                "Use the 1d or 1w interval, or a timestamp column."
            )
            raise WarehouseCapabilityError(msg)
        moment = self._utc_wall_clock(time_column)
        if spec.count == 1 and spec.unit in _TRUNC_UNIT:
            return f"CAST(date_trunc('{_TRUNC_UNIT[spec.unit]}', {moment}) AS timestamp(3))"
        width = int(spec.delta.total_seconds())
        origin = int((WEEK_ORIGIN if spec.unit is IntervalUnit.week else EPOCH).timestamp())
        minute = f"CAST(date_trunc('minute', {moment}) AS timestamp(3))"
        seconds = f"date_diff('second', {_EPOCH_NAIVE}, {minute})"
        shifted = f"{seconds} - {origin}" if origin else seconds
        restore = f" + {origin}" if origin else ""
        return (
            f"date_add('second', CAST(floor(CAST({shifted} AS double) / {width}) AS bigint) "
            f"* {width}{restore}, {_EPOCH_NAIVE})"
        )

    # ------------------------------------------------------------------ #
    # JSON columns
    # ------------------------------------------------------------------ #

    def _require_document(self, column: str) -> None:
        type_name = self._column_types.get(column)
        if type_name is None:
            return
        if classify_complex(type_name) is not ComplexKind.json:
            msg = (
                f"{self.engine_label}: column {column!r} has type {type_name} and holds no "
                "nested paths. Only json columns can be path-expanded (or a varchar "
                "column the scan parses as JSON)."
            )
            raise ValueError(msg)

    def _json_path_literal(self, path: str) -> str:
        """``'$["a"]["b"]'``: bracketed keys match exactly, case included.

        Every segment holds to the identifier grammar before it is spelled, so
        neither a quote nor a bracket can reach the path text.
        """
        parts = [part for part in path.split(".") if part]
        if not parts or any(not IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")
        return "'$" + "".join(f'["{part}"]' for part in parts) + "'"

    def _document(self, column: str) -> str:
        col = self._validate_column(column)
        self._require_document(col)
        return quote_ident(col)

    def _json_path_expression(self, column: str, path: str) -> str:
        """The value at ``path`` as JSON text — the grouped ``keep_json_value`` column.

        NULL when the path is absent, ``'null'`` for a JSON null.
        """
        doc = self._document(column)
        return f"json_format(json_extract({doc}, {self._json_path_literal(path)}))"

    def _property_value_expression(self, column: str, path: str) -> str:
        """One property as a nullable varchar: the scalar itself, NULL when absent or null.

        ``json_extract_scalar`` answers a string without its quotes and NULL for an
        object or array; those come back as their JSON text instead, the way the
        other engines render a container property.
        """
        doc = self._document(column)
        path_sql = self._json_path_literal(path)
        return (
            f"COALESCE(json_extract_scalar({doc}, {path_sql}), "
            f"NULLIF(json_format(json_extract({doc}, {path_sql})), 'null'))"
        )

    def _json_paths_expression(self, column: str) -> str:
        """A document column's top-level keys as a groupable, sorted JSON array string."""
        keys = f"map_keys({_AS_OBJECT.format(doc=self._document(column))})"
        return f"COALESCE(json_format(CAST(array_sort({keys}) AS json)), '[]')"

    def _text(self, column: str) -> str:
        """A column's value as varchar, rendered the way the other engines render it.

        ``CAST(double AS varchar)`` is scientific (``1.5E0``), so a float goes
        through ``format('%s', ...)`` (``1.5``); a json value through
        ``json_format``; an array, map or row through its JSON text, since none
        of them casts to varchar at all.
        """
        quoted = quote_ident(self._validate_column(column))
        type_name = self._column_types.get(column, "")
        if is_text_type(type_name):
            return quoted
        if is_float_type(type_name):
            return f"format('%s', {quoted})"
        if is_json_type(type_name):
            return f"json_format({quoted})"
        if is_container_type(type_name):
            return f"json_format(CAST({quoted} AS json))"
        return f"CAST({quoted} AS varchar)"

    def _string_value_expression(self, column: str) -> str:
        return f"COALESCE({self._text(column)}, '')"

    def _field_operand(self, field: str) -> str:
        prop = split_property_field(field)
        if prop is None:
            return quote_ident(self._validate_column(field))
        return self._property_value_expression(*prop)

    def _field_text(self, field: str) -> str:
        """A field's value as varchar: a column rendered by type, a property as is."""
        prop = split_property_field(field)
        if prop is None:
            return self._text(field)
        return self._property_value_expression(*prop)

    def _field_value_expression(self, field: str) -> str:
        return f"COALESCE({self._field_text(field)}, '')"

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
            expected = quote_literal(group_value or "")
            conditions.append(f"{self._string_value_expression(group_column)} = {expected}")
        if not conditions:
            return ""
        return " WHERE " + " AND ".join(conditions)

    def _contract_bad_predicate(
        self, expectation: FieldContractExpectation, regex_ok: bool
    ) -> str | None:
        """The predicate that makes one row BAD; ``None`` when the type has none.

        Mirrors ``PostgresAdapter._contract_bad_condition`` clause for clause:
        ``required_null`` counts NULLs, every other drift type skips them, and a
        range value that does not parse as a number is BAD.
        """
        field = expectation.field_name
        operand = self._field_operand(field)
        value_expr = f"COALESCE({self._field_text(field)}, '')"
        present = f"{operand} IS NOT NULL"
        drift_type = expectation.drift_type
        if drift_type == "required_null_violation":
            return f"{operand} IS NULL"
        if drift_type == "enum_violation":
            options = ", ".join(quote_literal(option) for option in expectation.enum_options)
            return f"{present} AND {value_expr} NOT IN ({options})"
        if drift_type == "regex_violation":
            assert expectation.regex is not None
            if not regex_ok:
                return None
            # regexp_like FINDS the pattern anywhere, like re.search.
            return (
                f"{present} AND NOT regexp_like({value_expr}, {quote_literal(expectation.regex)})"
            )
        if drift_type == "range_violation":
            number = f"try_cast({value_expr} AS double)"
            outside: list[str] = []
            if expectation.min_value is not None:
                outside.append(f"{number} < {contract_bound_literal(expectation.min_value)}")
            if expectation.max_value is not None:
                outside.append(f"{number} > {contract_bound_literal(expectation.max_value)}")
            # Unparseable is BAD. NaN compares false both ways, as in Python.
            return f"{present} AND ({number} IS NULL OR {' OR '.join(outside)})"
        return None

    def _contract_aggregate_sql(
        self, expectation: FieldContractExpectation, bad: str, *, index: int
    ) -> str:
        """``_bad_{i}``, ``_total_{i}``, ``_sample_{i}`` — the layout every engine shares."""
        field = expectation.field_name
        operand = self._field_operand(field)
        is_required = expectation.drift_type == "required_null_violation"
        total = "count(*)" if is_required else f"count_if({operand} IS NOT NULL)"
        sample = "'<NULL>'" if is_required else f"COALESCE({self._field_text(field)}, '')"
        return (
            f"count_if({bad}) AS _bad_{index}, "
            f"{total} AS _total_{index}, "
            f"min(IF({bad}, {sample}, NULL)) AS _sample_{index}"
        )
