from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any, override

from tripl.core.adapters.base import (
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    SchemaColumn,
    SchemaTable,
)
from tripl.core.adapters.databricks_sql import IDENTIFIER_PART_RE
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    build_aggregate_sql,
    coerce_aggregation,
    validate_measure_column,
)
from tripl.core.adapters.trino_queries import TrinoQueries
from tripl.core.adapters.trino_sql import (
    as_utc_bucket,
    decode_array_value,
    decode_json_list,
    is_array_type,
    quote_ident,
    quote_literal,
    truncate_sql,
)
from tripl.models.domain_enums import MetricAggregation

logger = logging.getLogger(__name__)

# Hard cap on catalog rows pulled for SQL-editor autocomplete, the same budget
# the other adapters use. One statement covers every schema in scope.
_SCHEMA_ROW_LIMIT = 50000

# Catalog introspection is a CAP, not a default: a source configuring a shorter
# timeout_seconds still wins (see ``_query_deadline``).
_SCHEMA_QUERY_TIMEOUT_SECONDS = 30

#: Trino's error name for a query that ran past ``query_max_run_time``.
_TIME_LIMIT_ERRORS = frozenset({"EXCEEDED_TIME_LIMIT", "USER_CANCELED"})


class TrinoAdapter(TrinoQueries, BaseAdapter):
    """Trino (and Starburst) adapter, over the ``trino`` client's DB-API.

    Connection: ``host`` and ``port`` reach the coordinator, ``database_name``
    is the catalog, ``username`` the user. The secret is the user's password
    (HTTP basic auth, over HTTPS only); an empty one connects without
    authentication, which a local or in-cluster coordinator often runs with.
    The scheme, default schema and schema allowlist are connection settings.

    Semantics mirror the other SQL adapters, spelled in Trino SQL:
      - toStartOfInterval → ``date_trunc`` of the UTC wall clock for 1h/1d, and
                            whole-second epoch arithmetic (``date_diff`` /
                            ``date_add``) for 15m/6h and weeks (anchored at a
                            Monday); every bucket is a ``timestamp(3)`` UTC wall
                            clock, independent of the session zone
      - JSONAllPaths      → the sorted TOP-LEVEL keys of a ``json`` document
                            (``map_keys(CAST(doc AS map(varchar, json)))``)
      - GROUPING SETS     → native syntax (grouped by subquery column or
                            ordinal: Trino refuses an output alias in GROUP BY)
      - LIMIT n BY col    → row_number() OVER (PARTITION BY ...) wrapper
      - countIf / anyIf   → ``count_if`` / ``min(IF(...))``
      - toFloat64OrNull   → ``try_cast(... AS double)``

    Every value that comes from data or from an analyst is a quoted literal
    (see ``trino_sql`` for why that, and not a bound parameter). Identifiers are
    double-quoted. ``AggregateSpec.filter_sql`` is the one exception, as on every
    engine: a validated boolean fragment injected as-is.

    **Declared divergences.**

    * Scan-time JSON shapes are top-level keys only, the trade PostgreSQL and
      Snowflake make. Nested paths are discovered for the path picker from
      sampled rows (``BaseAdapter.get_json_path_samples``), and any nested path
      can be extracted.
    * Contract regexes are Java-style (Trino's default joni library), run as an
      unanchored find (``regexp_like``, like ``re.search``). Lookaround is
      accepted; Python's ``(?P<name>...)`` is not, and a refused pattern is
      probed and dropped like on every engine.
    * A ``map`` or ``row`` column holds no discoverable paths; only ``json``
      documents (or varchar columns parsed as JSON) are path-expanded.
    """

    # Class-level defaults, for the same reason BigQueryAdapter has them: the unit
    # tests build adapters with ``object.__new__`` and seed only what they use.
    _timeout_seconds: float | None = None
    _catalog: str = ""
    _schema: str | None = None
    _schema_allowlist: tuple[str, ...] = ()
    _described: tuple[str, list[str]] | None = None

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        username: str = "",
        password: str = "",
        *,
        http_scheme: str = "https",
        schema: str | None = None,
        schema_allowlist: list[str] | None = None,
        timeout_seconds: int | None = None,
        address: str | None = None,
        **kwargs: object,
    ) -> None:
        del kwargs
        if not host:
            raise WarehouseCapabilityError("Trino: the coordinator host is required")
        if not database:
            raise WarehouseCapabilityError("Trino: the catalog is required")
        if not username:
            raise WarehouseCapabilityError("Trino: the user name is required")
        if password and http_scheme != "https":
            raise WarehouseCapabilityError(
                "Trino: a password is only sent over HTTPS. Switch the scheme to https, "
                "or leave the password empty for a coordinator without authentication."
            )
        self._timeout_seconds = float(timeout_seconds) if timeout_seconds else None
        self._catalog = database
        self._schema = schema or None
        self._schema_allowlist = tuple(schema_allowlist or ())
        self._allowed_columns = set()
        self._column_types = {}
        self._described = None
        self._conn = self._connect(host, port, username, password, http_scheme, address)

    def _connect(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        http_scheme: str,
        address: str | None,
    ) -> Any:
        from trino.auth import BasicAuthentication

        from tripl.core.adapters.trino_net import PinnedSession
        from tripl.core.adapters.trino_sql import import_trino

        dbapi = import_trino()
        timeout = self._timeout_seconds
        session_properties: dict[str, str] = {}
        if timeout is not None:
            # The coordinator itself kills a runaway statement: the server-side
            # half of the deadline, which holds even if this worker dies.
            session_properties["query_max_run_time"] = f"{math.ceil(timeout)}s"
        kwargs: dict[str, object] = {
            "host": host,
            "port": port,
            "user": username,
            "catalog": self._catalog,
            "schema": self._schema,
            "http_scheme": http_scheme,
            "source": "tripl",
            # Pins the session zone: naive timestamps compared with or cast to a
            # zoned value are read as UTC wall clocks.
            "timezone": "UTC",
            "session_properties": session_properties or None,
            "max_attempts": 1,
        }
        if timeout is not None:
            kwargs["request_timeout"] = math.ceil(timeout)
        if password:
            kwargs["auth"] = BasicAuthentication(username, password)
        if address is not None:
            kwargs["http_session"] = PinnedSession(host, address, port, tls=http_scheme == "https")
        return dbapi.connect(**kwargs)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    def _query_deadline(self, cap: float | None = None) -> float | None:
        timeout = self._timeout_seconds
        if timeout is not None and timeout <= 0:
            timeout = None
        if timeout is None:
            return cap
        if cap is None:
            return timeout
        return min(timeout, cap)

    def _timeout_error(self, deadline: float, exc: Exception) -> TimeoutError:
        msg = (
            f"{self.engine_label}: query exceeded the {deadline:g}s timeout configured for "
            "this data source and was cancelled. Narrow the time window, reduce the "
            "columns the base query selects, or raise the data source's timeout."
        )
        error = TimeoutError(msg)
        error.__cause__ = exc
        return error

    def _is_timeout(self, exc: Exception) -> bool:
        if getattr(exc, "error_name", None) in _TIME_LIMIT_ERRORS:
            return True
        import requests

        return isinstance(exc, requests.exceptions.Timeout)

    def _run(
        self, sql: str, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here.

        The coordinator cancels a statement past ``query_max_run_time``; a
        statement that ran out of time surfaces as a ``TimeoutError`` with a
        message the operator can act on, never as the driver's text.
        """
        deadline = self._query_deadline(timeout_cap)
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            rows = [tuple(row) for row in cursor.fetchall()]
            names = [str(column[0]) for column in cursor.description or []]
        except Exception as exc:
            if deadline is not None and self._is_timeout(exc):
                raise self._timeout_error(deadline, exc) from exc
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                logger.debug("%s: cursor close failed", self.engine_label, exc_info=True)
        return names, rows

    def _describe(self, sql: str) -> list[tuple[str, str]]:
        """``(name, type)`` of every result column of ``sql`` (run with ``LIMIT 0``)."""
        deadline = self._query_deadline()
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql)
            cursor.fetchall()
            description = list(cursor.description or [])
        except Exception as exc:
            if deadline is not None and self._is_timeout(exc):
                raise self._timeout_error(deadline, exc) from exc
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                logger.debug("%s: cursor close failed", self.engine_label, exc_info=True)
        return [(str(column[0]), str(column[1])) for column in description]

    def test_connection(self) -> bool:
        _, rows = self._run("SELECT 1 AS ok")
        return bool(rows and int(rows[0][0]) == 1)  # type: ignore[call-overload]

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        """Columns and types from a ``LIMIT 0`` wrapper: nothing is scanned.

        The planner turns ``LIMIT 0`` into an empty relation before any split
        is scheduled, so the base query is analyzed and typed but no table data
        is read, however large its tables are.
        """
        described = self._describe(f"SELECT * FROM ({base_query}) AS _src LIMIT 0")
        columns = [
            ColumnInfo(name=name, type_name=type_name, is_nullable=True)
            for name, type_name in described
        ]
        self._allowed_columns = {c.name for c in columns}
        self._column_types = {c.name: c.type_name for c in columns}
        self._described = (base_query, [c.name for c in columns])
        return columns

    def _ensure_column_types(self, base_query: str) -> None:
        if not self._column_types:
            self.get_columns(base_query)

    def _schemas_in_scope(self) -> list[str]:
        ordered = [self._schema] if self._schema else []
        ordered.extend(sorted({s for s in self._schema_allowlist if s != self._schema}))
        return [schema.lower() for schema in ordered]

    def get_schema_tables(self) -> list[SchemaTable]:
        """Tables and columns of the default schema plus the allowlisted ones.

        One statement over the catalog's ``information_schema.columns``, which
        the engine already filters to what the user may see. Without a default
        schema or an allowlist every schema of the catalog is listed (under the
        row cap). Tables in the default schema keep their bare name; others are
        ``schema.table``.
        """
        if not self._catalog:
            raise WarehouseCapabilityError(f"{self.engine_label}: no catalog configured")
        schemas = self._schemas_in_scope()
        if schemas:
            scope = f"table_schema IN ({', '.join(quote_literal(s) for s in schemas)})"
        else:
            scope = "table_schema <> 'information_schema'"
        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            f"FROM {quote_ident(self._catalog)}.information_schema.columns "
            f"WHERE {scope} "
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {_SCHEMA_ROW_LIMIT}"
        )
        _, rows = self._run(sql, timeout_cap=_SCHEMA_QUERY_TIMEOUT_SECONDS)
        default_schema = (self._schema or "").lower()
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for table_schema, table_name, column_name, data_type in rows:
            bare = str(table_name)
            qualified = bare if str(table_schema) == default_schema else f"{table_schema}.{bare}"
            columns_by_table.setdefault(qualified, []).append(
                SchemaColumn(name=str(column_name), data_type=str(data_type))
            )
        return [SchemaTable(name=name, columns=cols) for name, cols in columns_by_table.items()]

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

    supports_json_string_columns = True

    @override
    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        """Parse varchar columns as ``json`` in a wrapper over ``base_query``.

        ``try(json_parse(...))`` answers NULL for text that is not JSON instead
        of failing the statement, and a document that is not an object is NULL
        too, so either reads as a row that carries none of the keys. Every other
        column is projected unchanged, in order, from the column list
        ``get_columns`` just read.
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
            quoted = quote_ident(name)
            if name in wanted:
                parsed = f"try(json_parse({quoted}))"
                parts.append(
                    f"IF(try_cast({parsed} AS map(varchar, json)) IS NULL, NULL, {parsed}) "
                    f"AS {quoted}"
                )
            else:
                parts.append(quoted)
        return f"SELECT {', '.join(parts)} FROM ({base_query}) AS _json_src"

    # ------------------------------------------------------------------ #
    # result decoding
    # ------------------------------------------------------------------ #

    def _decode_rows(
        self,
        rows: list[tuple[object, ...]],
        *,
        offset: int,
        reg_cols: list[str],
        json_cols: list[str],
    ) -> list[tuple[object, ...]]:
        """Array regular columns and key-list columns back as lists."""
        decoders: dict[int, Any] = {}
        for index, column in enumerate(reg_cols):
            if is_array_type(self._column_types.get(column, "")):
                decoders[offset + index] = decode_array_value
        for index, _column in enumerate(json_cols):
            decoders[offset + len(reg_cols) + index] = decode_json_list
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

    def _timed(self, label: str, sql: str) -> list[tuple[object, ...]]:
        engine = self.engine_label
        logger.debug("%s %s query: %s", engine, label, truncate_sql(sql))
        t0 = time.monotonic()
        _, rows = self._run(sql)
        logger.info("%s %s done in %.2fs, %s rows", engine, label, time.monotonic() - t0, len(rows))
        return rows

    def _nested_parts(
        self,
        names_groups: tuple[list[str], list[str]],
        alias_by_name: dict[str, str],
        select_parts: list[str],
        group_parts: list[str],
    ) -> None:
        for names in names_groups:
            for name in names:
                select_parts.append(f"{alias_by_name[name]} AS {quote_ident(name)}")
                group_parts.append(alias_by_name[name])

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
        select_parts = [quote_ident(c) for c in reg_cols]
        group_parts = list(select_parts)
        self._nested_parts((json_cols, json_value_names), alias_by_name, select_parts, group_parts)
        select_parts.append("count(*) AS _cnt")
        # No grouping key at all is one total row.
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
        select_parts = [f"{bucket_expr} AS _bucket", *(quote_ident(c) for c in reg_cols)]
        group_parts = ["1", *(quote_ident(c) for c in reg_cols)]
        self._nested_parts((json_cols, json_value_names), alias_by_name, select_parts, group_parts)
        select_parts.append(value_sql)
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
            "count(*) AS _cnt",
            limit,
            "bucketed",
        )

    def _aggregate_value_sql(self, agg_fn: MetricAggregation, measure_column: str | None) -> str:
        measure_sql: str | None = None
        if measure_column is not None:
            measure_sql = quote_ident(
                validate_measure_column(measure_column, self._allowed_columns)
            )
        return build_aggregate_sql(agg_fn, measure_sql)

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

    def _fold(self, raw_expr: str, top_values: list[str]) -> tuple[str, str]:
        if not top_values:
            return "'Other'", "1"
        in_clause = f"{raw_expr} IN ({', '.join(quote_literal(v) for v in top_values)})"
        return (
            f"CASE WHEN {in_clause} THEN {raw_expr} ELSE 'Other' END",
            f"CASE WHEN {in_clause} THEN 0 ELSE 1 END",
        )

    def _breakdown_value_exprs(
        self,
        base_query: str,
        time_column: str,
        breakdown: str,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None,
    ) -> tuple[str, str]:
        """``(breakdown_value, is_other)`` with the top-N fold; kept values are literals."""
        raw_expr = self._string_value_expression(breakdown)
        if values_limit is None:
            return raw_expr, "0"
        top_values = self._top_breakdown_values_multi(
            base_query, time_column, [breakdown], time_from, time_to, max(values_limit - 1, 0)
        ).get(breakdown, [])
        return self._fold(raw_expr, top_values)

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

        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            base_query, time_column, breakdown, time_from, time_to, values_limit
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
        group_parts = ["1", "2", "3"]
        for c in reg_cols:
            if c == breakdown:
                # The FOLDED value in the breakdown's own slot, never a grouping key
                # of its own: see BaseAdapter.get_time_bucketed_aggregate_breakdown.
                # Trino accepts it because it is the grouped expression ``2``.
                select_parts.append(f"{breakdown_expr} AS {quote_ident(c)}")
            else:
                select_parts.append(quote_ident(c))
                group_parts.append(quote_ident(c))
        self._nested_parts((json_cols, json_value_names), alias_by_name, select_parts, group_parts)
        select_parts.append(f"{value_sql} AS _value")
        sql = (
            f"SELECT {', '.join(select_parts)} FROM {from_sql} "
            f"GROUP BY {', '.join(group_parts)} "
            f"ORDER BY _bucket, _breakdown_value LIMIT {int(limit)}"
        )
        rows = self._timed("bucketed aggregate breakdown", sql)
        decoded = self._decode_rows(rows, offset=3, reg_cols=reg_cols, json_cols=json_cols)
        return [*reg_cols, *json_cols], json_value_names, self._utc_bucket_rows(decoded)

    def _spec_aggregate_sql(self, spec: AggregateSpec) -> str:
        """One (optionally conditional) aggregate, under the NULL-means-gap rule.

        Folded into the aggregate with ``count_if`` as the row-presence count:
        ``count`` -> ``NULLIF(count_if(cond), 0)``; ``count_distinct`` -> gated
        on ``count_if(cond) = 0``; the others over ``CASE WHEN`` are NULL over
        zero matching rows on their own.
        """
        measure_sql: str | None = None
        if spec.column is not None:
            measure_sql = quote_ident(validate_measure_column(spec.column, self._allowed_columns))
        agg = coerce_aggregation(spec.aggregation)
        if spec.filter_sql is None:
            return build_aggregate_sql(agg, measure_sql)
        cond = spec.filter_sql
        if agg is MetricAggregation.count:
            return f"NULLIF(count_if({cond}), 0)"
        if not measure_sql:
            msg = f"Aggregation {agg.value!r} requires a measure column"
            raise ValueError(msg)
        if agg is MetricAggregation.count_distinct:
            distinct = f"count(DISTINCT IF({cond}, {measure_sql}, NULL))"
            return f"CASE WHEN count_if({cond}) = 0 THEN NULL ELSE {distinct} END"
        return f"{agg.value}(CASE WHEN {cond} THEN {measure_sql} END)"

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
            select_parts.append(f"{self._spec_aggregate_sql(spec)} AS {quote_ident(spec.key)}")
            col_names.append(spec.key)
        window = self._time_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} FROM ({base_query}) AS _src "
            f"WHERE {window} GROUP BY 1 ORDER BY _bucket LIMIT {int(limit)}"
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
        breakdown_expr, is_other_expr = self._breakdown_value_exprs(
            base_query, time_column, breakdown, time_from, time_to, values_limit
        )
        select_parts = [
            f"{self._bucket_expression(tc, interval)} AS _bucket",
            f"{breakdown_expr} AS _breakdown_value",
            f"{is_other_expr} AS _is_other",
        ]
        col_names = ["bucket", "breakdown_value", "is_other"]
        for spec in specs:
            select_parts.append(f"{self._spec_aggregate_sql(spec)} AS {quote_ident(spec.key)}")
            col_names.append(spec.key)
        window = self._time_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} FROM ({base_query}) AS _src "
            f"WHERE {window} GROUP BY 1, 2, 3 "
            f"ORDER BY _bucket, _breakdown_value LIMIT {int(limit)}"
        )
        rows = self._timed("bucketed multi-aggregate breakdown", sql)
        return col_names, self._utc_bucket_rows(rows)

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
        col_names, json_value_names, rows = self.get_time_bucketed_breakdown_counts_multi(
            base_query,
            time_column,
            interval,
            [breakdown_column],
            regular_columns,
            json_columns,
            json_value_paths,
            time_from,
            time_to,
            values_limit=values_limit,
            limit=limit,
        )
        return col_names, json_value_names, [(row[0], row[2], row[3], *row[4:]) for row in rows]
