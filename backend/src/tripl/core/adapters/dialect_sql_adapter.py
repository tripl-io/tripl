"""The statements Trino (and Athena), Snowflake and Databricks share.

The three engines answer every adapter question with the same statements; only
the spelling differs. Those statements used to be written out once per adapter
and had begun to drift — the Trino copy of the statement runner lost its
client-side deadline. They are written once here, over a small set of dialect
hooks, and each engine keeps only what is its own: the connection and the
statement runner, catalog introspection, and the expressions that spell its
time, JSON and text handling.

**The hooks.** Abstract, so an engine cannot leave one out: the statement runner
(``_run``), the value binder (``_new_params``), identifier quoting, the time
window literal and the bucket, a column or field as text, the JSON path
expressions, a string column parsed as JSON, a grouped value as text, the field
contract's text, regex and number tests, and the regex probe.

Defaults an engine overrides only where it differs:

* ``_COUNT``, ``_COUNT_IF``, ``_IF``, ``_MIN`` and ``_ROW_NUMBER`` — how the
  dialect spells those functions. SQL does not care about their case; the
  statements stay exactly as each engine has always sent them.
* ``_GROUP_BY_ORDINAL`` — group the leading bucket and breakdown columns by
  position rather than by output alias (Trino refuses an alias in GROUP BY).
* ``_nan_guard`` — keep NaN out of a range comparison, for an engine that
  orders NaN above every number.
* ``_key_list_decoder`` — how a grouped key-list cell reads back.
* ``_is_timeout`` and ``_cancel_waits_for_query_id`` — how the deadline
  recognises and cancels a statement (:meth:`_cursor_under_deadline`).

Every value that comes from data or from an analyst goes through the statement's
binder (:class:`ValueBinder`): a bound parameter on Snowflake and Databricks, a
quoted literal on Trino. ``AggregateSpec.filter_sql`` is the one exception, as on
every engine: a validated boolean fragment injected as-is.
"""

from __future__ import annotations

import abc
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from typing import Any, ClassVar, Protocol, override

from tripl.core.adapters.base import (
    FIELD_CONTRACT_EXPECTATIONS_PER_QUERY,
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
    FieldContractViolation,
    contract_bound_literal,
    field_contract_verdict,
    is_breakdown_field_of,
)
from tripl.core.adapters.deadline import StatementDeadline
from tripl.core.adapters.measure_validator import (
    build_aggregate_sql,
    coerce_aggregation,
    validate_measure_column,
)
from tripl.core.adapters.sql_common import (
    IDENTIFIER_PART_RE,
    as_utc_bucket,
    count_cell,
    decode_array_value,
    decode_json_list,
    is_array_type,
    truncate_sql,
)
from tripl.json_paths import split_property_field
from tripl.models.domain_enums import MetricAggregation

logger = logging.getLogger(__name__)


class ValueBinder(Protocol):
    """Where a statement builder puts a value that came from data or from an analyst."""

    def bind(self, value: str) -> str:
        """The SQL text that stands for ``value`` in the statement."""
        ...


class DialectSqlAdapter(BaseAdapter):
    """Every statement Trino, Athena, Snowflake and Databricks share, over dialect hooks.

    Semantics mirror the other SQL adapters:
      - toStartOfInterval → the engine's ``_bucket_expression``
      - JSONAllPaths      → the TOP-LEVEL keys of a document, sorted
      - GROUPING SETS     → native syntax, over the columns of a subquery
      - LIMIT n BY col    → ``row_number() OVER (PARTITION BY ...)`` wrapper
      - countIf / anyIf   → ``count_if`` / ``min(IF(...))``, as each dialect spells them
    """

    #: Error prefix and product name in messages and logs.
    engine_label: ClassVar[str]

    _conn: Any
    # A class-level default, like BaseAdapter's ``_column_types``: the unit tests
    # build adapters with ``object.__new__`` and seed only what they use.
    _described: tuple[str, list[str]] | None = None

    # How the dialect spells the functions the shared statements call.
    _COUNT = "count"
    _COUNT_IF = "count_if"
    _IF = "IF"
    _MIN = "min"
    _ROW_NUMBER = "row_number"

    #: Group the leading output columns by position instead of by alias.
    _GROUP_BY_ORDINAL = False

    #: Whether the deadline's cancel must wait for the statement's query id
    #: (see :class:`~tripl.core.adapters.deadline.StatementDeadline`).
    _cancel_waits_for_query_id = False

    supports_json_string_columns = True

    # ------------------------------------------------------------------ #
    # dialect hooks
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def _run(
        self, sql: str, params: ValueBinder | None = None, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here.

        ``params`` is the binder the statement's values went through; an engine
        that binds sends them with the statement. A statement that ran out of
        time surfaces as :meth:`_timeout_error`, never as the driver's text.
        """

    @abc.abstractmethod
    def _new_params(self) -> ValueBinder:
        """A fresh binder for one statement's values."""

    @override
    @abc.abstractmethod
    def _quote_ident(self, name: str) -> str:
        """``name`` as a quoted identifier that matches exactly."""

    @abc.abstractmethod
    def _time_literal(self, time_column: str, value: datetime) -> str:
        """A UTC instant as a literal of the time column's own type family."""

    @abc.abstractmethod
    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        """The bucket of every row's time; agrees with ``floor_to_bucket``."""

    @abc.abstractmethod
    def _string_value_expression(self, column: str) -> str:
        """A scalar column as text, NULL as ``''``."""

    @abc.abstractmethod
    def _field_value_expression(self, field: str) -> str:
        """A column or a property (``<json_column>.<path>``) as text, NULL as ``''``."""

    @abc.abstractmethod
    def _property_value_expression(self, column: str, path: str) -> str:
        """One property as nullable text: the scalar itself, NULL when absent or null."""

    @abc.abstractmethod
    def _json_path_expression(self, column: str, path: str) -> str:
        """The value at ``path`` as JSON text — the grouped ``keep_json_value`` column."""

    @abc.abstractmethod
    def _json_paths_expression(self, column: str) -> str:
        """A nested column's top-level keys as a groupable, sorted JSON array string."""

    @abc.abstractmethod
    def _json_object_or_null(self, quoted: str) -> str:
        """A string column parsed as JSON: NULL unless its text is a JSON object."""

    @abc.abstractmethod
    def _cast_text(self, expr: str) -> str:
        """``expr`` cast to the dialect's text type."""

    @abc.abstractmethod
    def _contract_text(self, field: str) -> str:
        """A contract field as text, NULL as ``''``: what enum, regex and range test."""

    @abc.abstractmethod
    def _regex_mismatch(self, value: str, pattern: str) -> str:
        """True when ``pattern`` is found NOWHERE in ``value``: an unanchored find."""

    @abc.abstractmethod
    def _try_double(self, value: str) -> str:
        """``value`` as a double, NULL when it does not parse as a number."""

    @override
    @abc.abstractmethod
    def _probe_contract_regex(self, pattern: str) -> None:
        """Have the engine compile ``pattern``, reading no table; raise if it will not.

        Abstract here: ``BaseAdapter``'s default is Python's ``re``, which is
        none of these engines' regex library.
        """

    def _nan_guard(self, number: str) -> str | None:
        """A condition that keeps NaN out of a range comparison, or ``None`` for none.

        A range contract compares NaN false both ways, as Python does. An
        engine whose comparison already does needs no guard; one that orders
        NaN above every number returns the test that excludes it.
        """
        del number
        return None

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    def _timeout_error(self, deadline: float, exc: Exception | None) -> TimeoutError:
        """What a statement past its deadline surfaces as: a message the operator can act on."""
        timeout = self._query_deadline()
        if timeout is None or deadline < timeout:
            # A per-call cap below the source's own timeout (``timeout_cap``):
            # raising the source's timeout would not lift it.
            msg = (
                f"{self.engine_label}: query exceeded its {deadline:g}s cap and was cancelled. "
                "This kind of query is capped below the data source's timeout, so narrow "
                "what it reads instead (for the schema browser: set a default schema or "
                "a schema allowlist)."
            )
        else:
            msg = (
                f"{self.engine_label}: query exceeded the {deadline:g}s timeout configured for "
                "this data source and was cancelled. Narrow the time window, reduce the "
                "columns the base query selects, or raise the data source's timeout."
            )
        error = TimeoutError(msg)
        error.__cause__ = exc
        return error

    def _is_timeout(self, exc: Exception) -> bool:
        """Whether ``exc`` is the engine's own report of a statement past its deadline.

        The server-side half of the deadline (``query_max_run_time`` on Trino)
        reports through the driver's error; the client-side half needs no such
        test, because :class:`StatementDeadline` records that it fired.
        """
        del exc
        return False

    def _close_cursor(self, cursor: Any) -> None:
        try:
            cursor.close()
        except Exception:
            logger.debug("%s: cursor close failed", self.engine_label, exc_info=True)

    @contextmanager
    def _cursor_under_deadline(self, deadline: float | None) -> Iterator[Any]:
        """A cursor whose statement is cancelled from a timer once ``deadline`` passes.

        Every error raised inside the block after the deadline fired, or that
        the engine reports as its own timeout, becomes :meth:`_timeout_error`.
        So does a block that ends WITHOUT an error after the deadline fired: a
        driver need not report a cancel that lands between two pages, and the
        rows read by then would pass for the whole result. The timer is
        disarmed and the cursor closed however the block ends, a worker's soft
        time limit or a shutdown included, so no timer fires on a closed cursor.
        """
        cursor = self._conn.cursor()
        guard = StatementDeadline(
            cursor,
            deadline,
            engine=self.engine_label,
            wait_for_query_id=self._cancel_waits_for_query_id,
        )
        try:
            yield cursor
        except Exception as exc:
            if deadline is not None and (guard.fired or self._is_timeout(exc)):
                raise self._timeout_error(deadline, exc) from exc
            raise
        finally:
            guard.disarm()
            self._close_cursor(cursor)
        if deadline is not None and guard.fired:
            raise self._timeout_error(deadline, None)

    def _timed(
        self, label: str, sql: str, params: ValueBinder | None = None
    ) -> list[tuple[object, ...]]:
        engine = self.engine_label
        logger.debug("%s %s query: %s", engine, label, truncate_sql(sql))
        t0 = time.monotonic()
        _, rows = self._run(sql, params)
        logger.info("%s %s done in %.2fs, %s rows", engine, label, time.monotonic() - t0, len(rows))
        return rows

    def test_connection(self) -> bool:
        _, rows = self._run("SELECT 1 AS ok")
        return bool(rows and int(rows[0][0]) == 1)  # type: ignore[call-overload]

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def _remember_columns(self, base_query: str, columns: list[ColumnInfo]) -> list[ColumnInfo]:
        """Keep what ``get_columns`` read: the allowlist, the types and the column order."""
        self._allowed_columns = {c.name for c in columns}
        self._column_types = {c.name: c.type_name for c in columns}
        self._described = (base_query, [c.name for c in columns])
        return columns

    def _ensure_column_types(self, base_query: str) -> None:
        if not self._column_types:
            self.get_columns(base_query)

    def get_preview_rows(
        self,
        base_query: str,
        limit: int = 10,
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        if time_column is not None:
            self._ensure_column_types(base_query)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        sql = f"SELECT * FROM ({base_query}) AS _src{where_clause} LIMIT {int(limit)}"
        logger.debug("%s preview query: %s", self.engine_label, truncate_sql(sql))
        return self._run(sql)

    @override
    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        """Parse string columns as the dialect's JSON type in a wrapper over ``base_query``.

        ``_json_object_or_null`` answers NULL for text that is not JSON instead
        of failing the statement, and for a document that is not an object, so
        either reads as a row that carries none of the keys. Every other column
        is projected unchanged, in order, from the column list ``get_columns``
        just read.
        """
        for column in columns:
            if not IDENTIFIER_PART_RE.match(column):
                raise ValueError(f"{self.engine_label}: invalid column name {column!r}")
        if not columns:
            return base_query
        described = self._described
        names = described[1] if described and described[0] == base_query else None
        if names is None:
            names = [c.name for c in self.get_columns(base_query)]
        wanted = set(columns)
        parts: list[str] = []
        for name in names:
            quoted = self._quote_ident(name)
            if name in wanted:
                parts.append(f"{self._json_object_or_null(quoted)} AS {quoted}")
            else:
                parts.append(quoted)
        return f"SELECT {', '.join(parts)} FROM ({base_query}) AS _json_src"

    # ------------------------------------------------------------------ #
    # shared expressions
    # ------------------------------------------------------------------ #

    def _time_condition(
        self, time_column: str | None, time_from: datetime | None, time_to: datetime | None
    ) -> str:
        if time_column is None or time_from is None or time_to is None:
            return ""
        quoted = self._quote_ident(self._validate_column(time_column))
        lower = self._time_literal(time_column, time_from)
        upper = self._time_literal(time_column, time_to)
        return f"{quoted} >= {lower} AND {quoted} < {upper}"

    def _time_window_where_clause(
        self, time_column: str | None, time_from: datetime | None, time_to: datetime | None
    ) -> str:
        condition = self._time_condition(time_column, time_from, time_to)
        return f" WHERE {condition}" if condition else ""

    def _field_operand(self, field: str) -> str:
        prop = split_property_field(field)
        if prop is None:
            return self._quote_ident(self._validate_column(field))
        return self._property_value_expression(*prop)

    def _validate_breakdown_field(self, field: str) -> str:
        prop = split_property_field(field)
        if prop is None:
            return self._validate_column(field)
        self._validate_column(prop[0])
        return field

    def _group_keys(self, *aliases: str) -> list[str]:
        """GROUP BY keys for the statement's leading output columns, named ``aliases``."""
        if self._GROUP_BY_ORDINAL:
            return [str(position) for position in range(1, len(aliases) + 1)]
        return list(aliases)

    def _nested_source(
        self,
        base_query: str,
        where_clause: str,
        json_cols: list[str],
        json_value_paths: dict[str, list[str]],
    ) -> tuple[str, dict[str, str], list[str]]:
        """The FROM source with every nested expression computed once, under an alias.

        The outer statement then groups by plain columns of this subquery, which
        keeps a lambda or a path expression out of the GROUP BY entirely. With no
        nested columns the source is the bare ``(base_query) AS _src`` and the
        WHERE clause follows it.
        """
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

    def _nested_select_group(
        self, names: list[str], alias_by_name: dict[str, str]
    ) -> tuple[list[str], list[str]]:
        """The select and group entries of the nested columns ``_nested_source`` aliased."""
        select_parts = [f"{alias_by_name[name]} AS {self._quote_ident(name)}" for name in names]
        group_parts = [alias_by_name[name] for name in names]
        return select_parts, group_parts

    # ------------------------------------------------------------------ #
    # result decoding
    # ------------------------------------------------------------------ #

    def _key_list_decoder(self, column: str) -> Callable[[object], object]:
        """How a grouped key-list cell of ``column`` reads back: the JSON array it holds."""
        del column
        return partial(decode_json_list, engine=self.engine_label)

    def _decode_rows(
        self,
        rows: list[tuple[object, ...]],
        *,
        offset: int,
        reg_cols: list[str],
        json_cols: list[str],
    ) -> list[tuple[object, ...]]:
        """ARRAY regular columns and key-list columns back as lists."""
        decoders: dict[int, Callable[[object], object]] = {}
        for index, column in enumerate(reg_cols):
            if is_array_type(self._column_types.get(column, "")):
                decoders[offset + index] = decode_array_value
        for index, column in enumerate(json_cols):
            decoders[offset + len(reg_cols) + index] = self._key_list_decoder(column)
        if not decoders:
            return rows
        return [
            tuple(
                decoders[index](value) if index in decoders else value
                for index, value in enumerate(row)
            )
            for row in rows
        ]

    def _utc_bucket_rows(self, rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
        return [(as_utc_bucket(row[0]), *row[1:]) for row in rows]

    # ------------------------------------------------------------------ #
    # scans and counts
    # ------------------------------------------------------------------ #

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
        self._ensure_column_types(base_query)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        from_sql, alias_by_name, json_value_names = self._nested_source(
            base_query, where_clause, json_cols, json_value_paths or {}
        )
        nested_select, nested_group = self._nested_select_group(
            [*json_cols, *json_value_names], alias_by_name
        )
        quoted_regular = [self._quote_ident(c) for c in reg_cols]
        select_parts = [*quoted_regular, *nested_select, f"{self._COUNT}(*) AS _cnt"]
        group_parts = [*quoted_regular, *nested_group]
        # No grouping key at all is one total row, with no GROUP BY (Snowflake
        # has no ``GROUP BY ()``).
        group_by = f" GROUP BY {', '.join(group_parts)}" if group_parts else ""
        sql = (
            f"SELECT {', '.join(select_parts)} FROM {from_sql}{group_by} "
            f"ORDER BY _cnt DESC LIMIT {int(limit)}"
        )
        rows = self._timed("breakdown", sql)
        decoded = self._decode_rows(rows, offset=0, reg_cols=reg_cols, json_cols=json_cols)
        return reg_cols, json_cols, json_value_names, decoded

    def _bucketed(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        regular_columns: list[str],
        json_columns: list[str],
        json_value_paths: dict[str, list[str]] | None,
        time_from: datetime,
        time_to: datetime,
        value_sql: str,
        limit: int,
        label: str,
    ) -> tuple[list[str], list[str], list[tuple[object, ...]]]:
        """The shared body of the bucketed count and the bucketed aggregate."""
        self._ensure_column_types(base_query)
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        from_sql, alias_by_name, json_value_names = self._nested_source(
            base_query, where_clause, json_cols, json_value_paths or {}
        )
        nested_select, nested_group = self._nested_select_group(
            [*json_cols, *json_value_names], alias_by_name
        )
        quoted_regular = [self._quote_ident(c) for c in reg_cols]
        select_parts = [f"{bucket_expr} AS _bucket", *quoted_regular, *nested_select, value_sql]
        group_parts = [*self._group_keys("_bucket"), *quoted_regular, *nested_group]
        sql = (
            f"SELECT {', '.join(select_parts)} FROM {from_sql} "
            f"GROUP BY {', '.join(group_parts)} ORDER BY _bucket LIMIT {int(limit)}"
        )
        rows = self._timed(label, sql)
        decoded = self._decode_rows(rows, offset=1, reg_cols=reg_cols, json_cols=json_cols)
        return [*reg_cols, *json_cols], json_value_names, self._utc_bucket_rows(decoded)

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
        return self._bucketed(
            base_query,
            time_column,
            interval,
            regular_columns,
            json_columns,
            json_value_paths,
            time_from,
            time_to,
            f"{self._COUNT}(*) AS _cnt",
            limit,
            "bucketed",
        )

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
        value_sql = f"{self._aggregate_value_sql(agg_fn, measure_column)} AS _value"
        return self._bucketed(
            base_query,
            time_column,
            interval,
            regular_columns,
            json_columns,
            json_value_paths,
            time_from,
            time_to,
            value_sql,
            limit,
            "bucketed aggregate",
        )

    def _fold(self, params: ValueBinder, raw_expr: str, top_values: list[str]) -> tuple[str, str]:
        """``(value, is_other)``: ``raw_expr`` where it is a kept value, else ``'Other'``."""
        if not top_values:
            return "'Other'", "1"
        markers = ", ".join(params.bind(value) for value in top_values)
        in_clause = f"{raw_expr} IN ({markers})"
        return (
            f"CASE WHEN {in_clause} THEN {raw_expr} ELSE 'Other' END",
            f"CASE WHEN {in_clause} THEN 0 ELSE 1 END",
        )

    def _breakdown_value_exprs(
        self,
        params: ValueBinder,
        base_query: str,
        time_column: str,
        breakdown: str,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None,
    ) -> tuple[str, str]:
        """``(breakdown_value, is_other)`` with the top-N fold.

        The kept values go through ``params``.
        """
        raw_expr = self._string_value_expression(breakdown)
        if values_limit is None:
            return raw_expr, "0"
        top_values = self._top_breakdown_values_multi(
            base_query, time_column, [breakdown], time_from, time_to, max(values_limit - 1, 0)
        ).get(breakdown, [])
        return self._fold(params, raw_expr, top_values)

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
        self._ensure_column_types(base_query)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        breakdown = self._validate_column(breakdown_column)
        if breakdown not in reg_cols:
            msg = f"Breakdown column must be a scalar column: {breakdown}"
            raise ValueError(msg)
        value_sql = self._aggregate_value_sql(agg_fn, measure_column)

        params = self._new_params()
        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            params, base_query, time_column, breakdown, time_from, time_to, values_limit
        )
        bucket_expr = self._bucket_expression(time_column, interval)
        where_clause = self._time_window_where_clause(time_column, time_from, time_to)
        from_sql, alias_by_name, json_value_names = self._nested_source(
            base_query, where_clause, json_cols, json_value_paths or {}
        )
        select_parts = [
            f"{bucket_expr} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        group_parts = self._group_keys("_bucket", "_breakdown_value", "_is_other")
        for c in reg_cols:
            if c == breakdown:
                # The FOLDED value in the breakdown's own slot, and never a
                # grouping key of its own: see
                # BaseAdapter.get_time_bucketed_aggregate_breakdown. The fold is
                # repeated rather than the alias reused; it is the very
                # expression ``_breakdown_value`` groups by.
                select_parts.append(f"{breakdown_expr} AS {self._quote_ident(c)}")
            else:
                select_parts.append(self._quote_ident(c))
                group_parts.append(self._quote_ident(c))
        nested_select, nested_group = self._nested_select_group(
            [*json_cols, *json_value_names], alias_by_name
        )
        select_parts.extend(nested_select)
        group_parts.extend(nested_group)
        select_parts.append(f"{value_sql} AS _value")
        sql = (
            f"SELECT {', '.join(select_parts)} FROM {from_sql} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket, _breakdown_value LIMIT {int(limit)}"
        )
        rows = self._timed("bucketed aggregate breakdown", sql, params)
        decoded = self._decode_rows(rows, offset=3, reg_cols=reg_cols, json_cols=json_cols)
        return [*reg_cols, *json_cols], json_value_names, self._utc_bucket_rows(decoded)

    def _spec_aggregate_sql(self, spec: AggregateSpec) -> str:
        """One (optionally conditional) aggregate, under the NULL-means-gap rule.

        Folded into the aggregate the way BigQuery does it, with ``count_if`` as
        the row-presence count: ``count`` -> ``NULLIF(count_if(cond), 0)``;
        ``count_distinct`` -> gated on ``count_if(cond) = 0``, because a distinct
        count is 0 both for no matching rows and for matching rows whose measure
        is NULL; ``sum``/``avg``/``min``/``max`` over ``CASE WHEN`` are NULL over
        zero matching rows on their own. See :class:`BaseAdapter`.
        """
        measure_sql: str | None = None
        if spec.column is not None:
            measure_sql = self._quote_ident(
                validate_measure_column(spec.column, self._allowed_columns)
            )
        agg = coerce_aggregation(spec.aggregation)
        if spec.filter_sql is None:
            return build_aggregate_sql(agg, measure_sql)
        cond = spec.filter_sql
        if agg is MetricAggregation.count:
            return f"NULLIF({self._COUNT_IF}({cond}), 0)"
        if not measure_sql:
            msg = f"Aggregation {agg.value!r} requires a measure column"
            raise ValueError(msg)
        if agg is MetricAggregation.count_distinct:
            distinct = f"{self._COUNT}(DISTINCT {self._IF}({cond}, {measure_sql}, NULL))"
            return f"CASE WHEN {self._COUNT_IF}({cond}) = 0 THEN NULL ELSE {distinct} END"
        return f"{agg.value}(CASE WHEN {cond} THEN {measure_sql} END)"

    @override
    def build_time_bucketed_multi_aggregate_sql(
        self,
        base_query: str,
        time_column: str,
        interval: str,
        specs: list[AggregateSpec],
        time_from: datetime,
        time_to: datetime,
        *,
        limit: int = 100000,
    ) -> tuple[list[str], str]:
        tc = self._validate_column(time_column)
        select_parts = [f"{self._bucket_expression(tc, interval)} AS _bucket"]
        col_names = ["bucket"]
        for spec in specs:
            select_parts.append(
                f"{self._spec_aggregate_sql(spec)} AS {self._quote_ident(spec.key)}"
            )
            col_names.append(spec.key)
        window = self._time_condition(tc, time_from, time_to)
        group_by = ", ".join(self._group_keys("_bucket"))
        sql = (
            f"SELECT {', '.join(select_parts)} FROM ({base_query}) AS _src "
            f"WHERE {window} GROUP BY {group_by} ORDER BY _bucket LIMIT {int(limit)}"
        )
        return col_names, sql

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
        self._ensure_column_types(base_query)
        col_names, sql = self.build_time_bucketed_multi_aggregate_sql(
            base_query, time_column, interval, specs, time_from, time_to, limit=limit
        )
        rows = self._timed("bucketed multi-aggregate", sql)
        return col_names, self._utc_bucket_rows(rows)

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
        self._ensure_column_types(base_query)
        tc = self._validate_column(time_column)
        breakdown = self._validate_column(breakdown_column)
        params = self._new_params()
        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            params, base_query, time_column, breakdown, time_from, time_to, values_limit
        )
        select_parts = [
            f"{self._bucket_expression(tc, interval)} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        col_names = ["bucket", "breakdown_value", "is_other"]
        for spec in specs:
            select_parts.append(
                f"{self._spec_aggregate_sql(spec)} AS {self._quote_ident(spec.key)}"
            )
            col_names.append(spec.key)
        window = self._time_condition(tc, time_from, time_to)
        group_by = ", ".join(self._group_keys("_bucket", "_breakdown_value", "_is_other"))
        sql = (
            f"SELECT {', '.join(select_parts)} FROM ({base_query}) AS _src "
            f"WHERE {window} GROUP BY {group_by} "
            f"ORDER BY _bucket, _breakdown_value LIMIT {int(limit)}"
        )
        rows = self._timed("bucketed multi-aggregate breakdown", sql, params)
        return col_names, self._utc_bucket_rows(rows)

    # ------------------------------------------------------------------ #
    # GROUPING SETS breakdowns
    # ------------------------------------------------------------------ #

    @override
    def _query_top_breakdown_values_multi(
        self,
        base_query: str,
        time_column: str,
        breakdown_columns: list[str],
        time_from: datetime,
        time_to: datetime,
        limit: int,
    ) -> dict[str, list[str]]:
        if limit <= 0 or not breakdown_columns:
            return {column: [] for column in breakdown_columns}
        self._ensure_column_types(base_query)
        cols = [self._validate_breakdown_field(c) for c in breakdown_columns]
        window = self._time_condition(time_column, time_from, time_to)
        params = self._new_params()
        prepared = [
            f"{self._field_value_expression(c)} AS __bd_raw_{i}" for i, c in enumerate(cols)
        ]
        grouping_sets = ", ".join(f"(__bd_raw_{i})" for i in range(len(cols)))
        label_branches = " ".join(
            f"WHEN GROUPING(__bd_raw_{i}) = 0 THEN {params.bind(c)}" for i, c in enumerate(cols)
        )
        value_branches = " ".join(
            f"WHEN GROUPING(__bd_raw_{i}) = 0 THEN __bd_raw_{i}" for i in range(len(cols))
        )
        sql = (
            "SELECT _breakdown_column, _breakdown_value FROM ("
            "SELECT _breakdown_column, _breakdown_value, "
            # The value tie-break the BaseAdapter top-N contract requires, in
            # code-point order on all three: Trino compares varchar by code point
            # with no collation to set, and a Snowflake or Databricks string with
            # no collation (UTF8_BINARY) compares byte by byte, which is
            # code-point order over UTF-8.
            f"{self._ROW_NUMBER}() OVER (PARTITION BY _breakdown_column "
            "ORDER BY _cnt DESC, _breakdown_value) AS rn "
            "FROM ("
            f"SELECT CASE {label_branches} ELSE '' END AS _breakdown_column, "
            f"CASE {value_branches} ELSE '' END AS _breakdown_value, "
            f"{self._COUNT}(*) AS _cnt "
            f"FROM (SELECT {', '.join(prepared)} FROM ({base_query}) AS _src WHERE {window}) "
            "AS _prepared "
            f"GROUP BY GROUPING SETS ({grouping_sets})"
            ") AS _scored"
            ") AS _ranked "
            f"WHERE rn <= {int(limit)}"
        )
        logger.debug("%s breakdown top-values query: %s", self.engine_label, truncate_sql(sql))
        top: dict[str, list[str]] = {c: [] for c in cols}
        _, rows = self._run(sql, params)
        for column, value in rows:
            top.setdefault(str(column), []).append(str(value))
        return top

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
        if not breakdown_columns:
            return [], [], []
        self._ensure_column_types(base_query)
        tc = self._validate_column(time_column)
        reg_cols = [self._validate_column(c) for c in regular_columns]
        json_cols = [self._validate_column(c) for c in json_columns]
        breakdown_cols = [self._validate_breakdown_field(c) for c in breakdown_columns]
        invalid = [c for c in breakdown_cols if not is_breakdown_field_of(c, reg_cols, json_cols)]
        if invalid:
            msg = (
                "Breakdown columns must be scalar columns or properties of a JSON column: "
                f"{', '.join(invalid)}"
            )
            raise ValueError(msg)
        json_value_paths = json_value_paths or {}
        top_values_by_column: dict[str, list[str]] | None = None
        if values_limit is not None:
            top_values_by_column = self._top_breakdown_values_multi(
                base_query,
                time_column,
                breakdown_cols,
                time_from,
                time_to,
                max(values_limit - 1, 0),
            )

        params = self._new_params()
        quote = self._quote_ident
        prepared = [f"{self._bucket_expression(tc, interval)} AS _bucket"]
        prepared.extend(f"{quote(c)} AS {quote(c)}" for c in reg_cols)
        prepared.extend(f"{self._json_paths_expression(c)} AS {quote(c)}" for c in json_cols)
        json_value_names: list[str] = []
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                prepared.append(f"{self._json_path_expression(c, path)} AS {quote(full_path)}")
                json_value_names.append(full_path)
        grouping_columns = [quote(n) for n in [*reg_cols, *json_cols, *json_value_names]]

        label_when: list[str] = []
        value_when: list[str] = []
        other_when: list[str] = []
        grouping_sets: list[str] = []
        for idx, column in enumerate(breakdown_cols):
            raw_expr = self._field_value_expression(column)
            if top_values_by_column is None:
                breakdown_expr, is_other_expr = raw_expr, "0"
            else:
                breakdown_expr, is_other_expr = self._fold(
                    params, raw_expr, top_values_by_column.get(column, [])
                )
            value_alias, other_alias = f"__bd_value_{idx}", f"__bd_other_{idx}"
            prepared.append(f"{breakdown_expr} AS {value_alias}")
            prepared.append(f"{is_other_expr} AS {other_alias}")
            check = f"GROUPING({value_alias}) = 0"
            label_when.append(f"WHEN {check} THEN {params.bind(column)}")
            value_when.append(f"WHEN {check} THEN {self._cast_text(value_alias)}")
            other_when.append(f"WHEN {check} THEN {other_alias}")
            grouping_sets.append(
                "(" + ", ".join(["_bucket", value_alias, other_alias, *grouping_columns]) + ")"
            )

        select_parts = [
            "_bucket",
            f"CASE {' '.join(label_when)} ELSE '' END AS _breakdown_column",
            f"CASE {' '.join(value_when)} ELSE '' END AS _breakdown_value",
            f"CASE {' '.join(other_when)} ELSE 0 END AS _is_other",
            *grouping_columns,
            f"{self._COUNT}(*) AS _cnt",
        ]
        window = self._time_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} FROM ("
            f"SELECT {', '.join(prepared)} FROM ({base_query}) AS _src WHERE {window}"
            ") AS _prepared "
            f"GROUP BY GROUPING SETS ({', '.join(grouping_sets)}) "
            f"ORDER BY _bucket, _breakdown_column, _breakdown_value LIMIT {int(limit)}"
        )
        rows = self._timed("bucketed breakdown GROUPING SETS", sql, params)
        decoded = self._decode_rows(rows, offset=4, reg_cols=reg_cols, json_cols=json_cols)
        return [*reg_cols, *json_cols], json_value_names, self._utc_bucket_rows(decoded)

    # ------------------------------------------------------------------ #
    # field contracts
    # ------------------------------------------------------------------ #

    def _contract_where_clause(
        self,
        params: ValueBinder,
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
            # The group column as text with NULL as '' — the NULL-as-empty-string
            # comparison ClickHouse makes with ifNull(toString(col), '').
            expected = params.bind(group_value or "")
            conditions.append(f"{self._string_value_expression(group_column)} = {expected}")
        if not conditions:
            return ""
        return " WHERE " + " AND ".join(conditions)

    def _contract_bad_condition(
        self, expectation: FieldContractExpectation, params: ValueBinder
    ) -> str | None:
        """The predicate that makes one row BAD, or ``None`` when the contract is inert.

        Mirrors ``PostgresAdapter._contract_bad_condition`` clause for clause:
        ``required_null`` counts NULLs, every other drift type skips them, and a
        range value that does not parse as a number is BAD.
        """
        if self._field_contract_is_inert(expectation):
            return None
        operand = self._contract_operand(expectation, self._field_operand)
        if operand is None:
            return None
        present = f"{operand} IS NOT NULL"
        drift_type = expectation.drift_type
        if drift_type == "required_null_violation":
            return f"{operand} IS NULL"
        value_expr = self._contract_text(expectation.field_name)
        if drift_type == "enum_violation":
            options = ", ".join(params.bind(option) for option in expectation.enum_options)
            return f"{present} AND {value_expr} NOT IN ({options})"
        if drift_type == "regex_violation":
            assert expectation.regex is not None
            if not self.contract_regex_is_compilable(expectation.regex):
                self._skip_field_contract(expectation)
                return None
            mismatch = self._regex_mismatch(value_expr, params.bind(expectation.regex))
            return f"{present} AND {mismatch}"
        if drift_type == "range_violation":
            number = self._try_double(value_expr)
            outside: list[str] = []
            if expectation.min_value is not None:
                outside.append(f"{number} < {contract_bound_literal(expectation.min_value)}")
            if expectation.max_value is not None:
                outside.append(f"{number} > {contract_bound_literal(expectation.max_value)}")
            # Unparseable is BAD. NaN compares false both ways, as in Python.
            comparison = " OR ".join(outside)
            guard = self._nan_guard(number)
            if guard is not None:
                comparison = f"({guard} AND ({comparison}))"
            return f"{present} AND ({number} IS NULL OR {comparison})"
        return None

    def _contract_aggregate_sql(
        self, expectation: FieldContractExpectation, bad: str, *, index: int
    ) -> str:
        """``_bad_{i}``, ``_total_{i}``, ``_sample_{i}`` — the layout every engine shares."""
        operand = self._field_operand(expectation.field_name)
        is_required = expectation.drift_type == "required_null_violation"
        total = f"{self._COUNT}(*)" if is_required else f"{self._COUNT_IF}({operand} IS NOT NULL)"
        sample = "'<NULL>'" if is_required else self._contract_text(expectation.field_name)
        return (
            f"{self._COUNT_IF}({bad}) AS _bad_{index}, "
            f"{total} AS _total_{index}, "
            f"{self._MIN}({self._IF}({bad}, {sample}, NULL)) AS _sample_{index}"
        )

    @override
    def validate_field_contracts(
        self,
        base_query: str,
        expectations: list[FieldContractExpectation],
        *,
        time_column: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        group_column: str | None = None,
        group_value: str | None = None,
        limit: int = 50000,
    ) -> list[FieldContractViolation]:
        """Count every contract warehouse-side over the FULL window, in one scan.

        The PostgreSQL shape: the aggregates of up to
        ``FIELD_CONTRACT_EXPECTATIONS_PER_QUERY`` expectations ride side by side
        over a single ``FROM (base_query)``, only the counts come back, and
        ``field_contract_verdict`` decides. ``limit`` bounds the violations
        returned, never the rows counted.
        """
        if not expectations:
            return []
        self._ensure_column_types(base_query)
        params = self._new_params()
        where_clause = self._contract_where_clause(
            params, time_column, time_from, time_to, group_column, group_value
        )
        compiled: list[tuple[FieldContractExpectation, str]] = []
        for expectation in expectations:
            source_column = expectation.field_name.split(".", 1)[0]
            if self._allowed_columns and source_column not in self._allowed_columns:
                self._skip_field_contract(expectation)
                continue
            condition = self._contract_bad_condition(expectation, params)
            if condition is not None:
                compiled.append((expectation, condition))
        if not compiled:
            return []

        label = self.engine_label
        violations: list[FieldContractViolation] = []
        t0 = time.monotonic()
        for start in range(0, len(compiled), FIELD_CONTRACT_EXPECTATIONS_PER_QUERY):
            chunk = compiled[start : start + FIELD_CONTRACT_EXPECTATIONS_PER_QUERY]
            selects = ", ".join(
                self._contract_aggregate_sql(expectation, condition, index=index)
                for index, (expectation, condition) in enumerate(chunk)
            )
            sql = f"SELECT {selects} FROM ({base_query}) AS _src{where_clause}"
            logger.debug("%s field contract query: %s", label, truncate_sql(sql))
            _, rows = self._run(sql, params)
            if not rows:
                continue
            row = rows[0]
            for index, (expectation, _condition) in enumerate(chunk):
                sample = row[index * 3 + 2]
                violation = field_contract_verdict(
                    expectation,
                    bad_count=count_cell(row[index * 3], label),
                    total_count=count_cell(row[index * 3 + 1], label),
                    sample_value=None if sample is None else str(sample),
                )
                if violation is not None:
                    violations.append(violation)
        logger.info(
            "%s field contracts done in %.2fs, %s violations",
            label,
            time.monotonic() - t0,
            len(violations),
        )
        return violations[: max(0, int(limit))]
