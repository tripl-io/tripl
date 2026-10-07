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
from tripl.core.adapters.snowflake_queries import SnowflakeQueries
from tripl.core.adapters.snowflake_sql import (
    Params,
    as_utc_bucket,
    decode_array_value,
    decode_json_list,
    field_type_name,
    is_array_type,
    object_name,
    quote_ident,
    truncate_sql,
    type_name_of,
)
from tripl.models.domain_enums import MetricAggregation

logger = logging.getLogger(__name__)

# Hard cap on catalog rows pulled for SQL-editor autocomplete, the same budget
# the other adapters use. One statement covers every schema in scope.
_SCHEMA_ROW_LIMIT = 50000

# Catalog introspection is a CAP, not a default: a source configuring a shorter
# timeout_seconds still wins (see ``_query_deadline``).
_SCHEMA_QUERY_TIMEOUT_SECONDS = 30

# Snowflake's error numbers for a statement that was cancelled (the driver's own
# deadline cancels it) and for one that hit STATEMENT_TIMEOUT_IN_SECONDS.
_CANCELLED_ERRNOS = frozenset({604, 630})


def _schema_key(name: str) -> str:
    """How ``information_schema`` spells a schema name a user typed.

    An unquoted identifier is stored upper-cased (``public`` is ``PUBLIC``);
    any other name was created quoted and is stored exactly as written.
    """
    return name.upper() if object_name(name) == name else name


class SnowflakeAdapter(SnowflakeQueries, BaseAdapter):
    """Snowflake adapter.

    Connection: ``host`` is the account identifier (or its
    ``*.snowflakecomputing.com`` hostname), ``database_name`` the database,
    ``username`` the user. The warehouse, role, default schema and schema
    allowlist are connection settings. The secret is the source's encrypted
    ``password``: the user's password, or the PEM private key of a key-pair user.

    Semantics mirror the other SQL adapters, spelled in Snowflake SQL:
      - toStartOfInterval → ``DATE_TRUNC`` of the UTC wall clock for 15m-less
                            1h/1d, and epoch arithmetic
                            (``TO_TIMESTAMP_NTZ(FLOOR(DATE_PART(EPOCH_SECOND, t) / w) * w)``)
                            for 15m/6h and weeks (anchored at a Monday), so the
                            session's WEEK_START cannot move a bucket
      - JSONAllPaths      → the sorted TOP-LEVEL keys of a VARIANT/OBJECT
                            document (``OBJECT_KEYS``)
      - GROUPING SETS     → native syntax
      - LIMIT n BY col    → ROW_NUMBER() OVER (PARTITION BY ...) wrapper
      - countIf / anyIf   → ``COUNT_IF`` / ``MIN(IFF(...))``
      - toFloat64OrNull   → ``TRY_TO_DOUBLE``

    Every value that comes from data or from an analyst is a bound positional
    parameter (``:1``, the connection's ``numeric`` paramstyle), never text in
    the statement. Identifiers are double-quoted and match exactly, the case
    ``get_columns`` read included. ``AggregateSpec.filter_sql`` is the one
    exception, as on every engine: a validated boolean fragment injected as-is.

    **Declared divergences.**

    * Scan-time JSON shapes are top-level keys only, the trade PostgreSQL and
      Databricks make. Nested paths are discovered for the path picker from
      sampled rows (``BaseAdapter.get_json_path_samples``), and any nested path
      can be extracted.
    * Contract regexes are POSIX extended regular expressions with Perl's
      ``\\d``/``\\w``/``\\s`` classes, run as an unanchored find
      (``REGEXP_INSTR``, like ``re.search``). There is no lookaround and no
      backreference; a refused pattern is probed and dropped like on every
      engine.
    * A MAP or structured OBJECT column holds no discoverable paths; only
      VARIANT and semi-structured OBJECT documents are path-expanded.
    """

    # Class-level defaults, for the same reason BigQueryAdapter has them: the unit
    # tests build adapters with ``object.__new__`` and seed only what they use.
    _timeout_seconds: float | None = None
    _database: str = ""
    _schema: str = "PUBLIC"
    _schema_allowlist: tuple[str, ...] = ()
    _described: tuple[str, list[str]] | None = None

    def __init__(
        self,
        host: str,
        port: int,  # always 443: Snowflake is reached over HTTPS
        database: str,
        username: str = "",
        password: str = "",
        *,
        account: str,
        warehouse: str,
        auth_type: str = "password",
        role: str | None = None,
        schema: str | None = None,
        schema_allowlist: list[str] | None = None,
        timeout_seconds: int | None = None,
        **kwargs: object,
    ) -> None:
        del port, kwargs
        if not account or not host:
            raise WarehouseCapabilityError("Snowflake: the account identifier is required")
        if not warehouse:
            raise WarehouseCapabilityError("Snowflake: the virtual warehouse is required")
        if not username:
            raise WarehouseCapabilityError("Snowflake: the user name is required")
        if not password:
            raise WarehouseCapabilityError(
                "Snowflake: a password or, for key-pair sign-in, the private key is required"
            )

        self._timeout_seconds = float(timeout_seconds) if timeout_seconds else None
        self._database = database
        self._schema = schema or "PUBLIC"
        self._schema_allowlist = tuple(schema_allowlist or ())
        self._allowed_columns = set()
        self._column_types = {}
        self._described = None
        self._conn = self._connect(
            account, host, username, password, auth_type, warehouse, role, schema
        )

    def _connect(
        self,
        account: str,
        host: str,
        username: str,
        password: str,
        auth_type: str,
        warehouse: str,
        role: str | None,
        schema: str | None,
    ) -> Any:
        from tripl.core.adapters.snowflake_sql import import_driver, load_private_key

        connector = import_driver()
        timeout = self._timeout_seconds
        # TIMEZONE pins the session to UTC: LTZ values, CONVERT_TIMEZONE and every
        # window literal are read in it. STATEMENT_TIMEOUT_IN_SECONDS makes the
        # warehouse itself cancel a runaway statement — the server-side half of the
        # deadline, which holds even if this worker dies before its own cancel.
        session: dict[str, object] = {
            "TIMEZONE": "UTC",
            "QUERY_TAG": "tripl",
            "CLIENT_TELEMETRY_ENABLED": False,
        }
        if timeout is not None:
            session["STATEMENT_TIMEOUT_IN_SECONDS"] = math.ceil(timeout)
        kwargs: dict[str, object] = {
            "account": account,
            "host": host,
            "port": 443,
            "protocol": "https",
            "user": username,
            "warehouse": warehouse,
            "database": self._database or None,
            "schema": schema or None,
            "role": role or None,
            "session_parameters": session,
            # Values are bound server-side as :1, :2 ... (see ``Params``), and no
            # client-side %-formatting ever touches a base query.
            "paramstyle": "numeric",
            "application": "tripl",
            "client_session_keep_alive": False,
            "client_store_temporary_credential": False,
            # A missing warehouse/database/schema fails the connection test with
            # Snowflake's own message instead of the first scan.
            "validate_default_parameters": True,
        }
        if timeout is not None:
            seconds = math.ceil(timeout)
            kwargs["login_timeout"] = seconds
            kwargs["network_timeout"] = seconds
            kwargs["socket_timeout"] = seconds
        if auth_type == "key_pair":
            kwargs["authenticator"] = "SNOWFLAKE_JWT"
            kwargs["private_key"] = load_private_key(password)
        else:
            kwargs["password"] = password
        return connector.connect(**kwargs)

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
            f"Snowflake: query exceeded the {deadline:g}s timeout configured for this "
            "data source and was cancelled. Narrow the time window, reduce the columns "
            "the base query selects, or raise the data source's timeout."
        )
        error = TimeoutError(msg)
        error.__cause__ = exc
        return error

    def _run(
        self, sql: str, params: Params | None = None, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here.

        The driver cancels the statement itself when ``timeout`` elapses, on top
        of the session's ``STATEMENT_TIMEOUT_IN_SECONDS``. A cancelled statement
        surfaces as a ``TimeoutError`` with a message the operator can act on,
        never as the driver's text.
        """
        deadline = self._query_deadline(timeout_cap)
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                sql,
                params.values if params and params.values else None,
                timeout=math.ceil(deadline) if deadline is not None else None,
            )
            names = [str(column[0]) for column in cursor.description or []]
            rows = [tuple(row) for row in cursor.fetchall()]
        except Exception as exc:
            if deadline is not None and getattr(exc, "errno", None) in _CANCELLED_ERRNOS:
                raise self._timeout_error(deadline, exc) from exc
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                logger.debug("Snowflake: cursor close failed", exc_info=True)
        return names, rows

    def test_connection(self) -> bool:
        _, rows = self._run("SELECT 1 AS ok")
        return bool(rows and int(rows[0][0]) == 1)  # type: ignore[call-overload]

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        """Columns and types from the driver's describe-only call: nothing is executed.

        ``cursor.describe`` asks Snowflake to compile the statement and return
        its result metadata without running it, so no warehouse time is spent
        and nothing is scanned, however large the base query's tables are.
        """
        deadline = self._query_deadline()
        cursor = self._conn.cursor()
        try:
            metadata = cursor.describe(
                f"SELECT * FROM ({base_query}) AS _src",
                timeout=math.ceil(deadline) if deadline is not None else None,
            )
        except Exception as exc:
            if deadline is not None and getattr(exc, "errno", None) in _CANCELLED_ERRNOS:
                raise self._timeout_error(deadline, exc) from exc
            raise
        finally:
            try:
                cursor.close()
            except Exception:
                logger.debug("Snowflake: cursor close failed", exc_info=True)
        columns: list[ColumnInfo] = []
        for meta in metadata or []:
            driver_name = field_type_name(meta.type_code)
            columns.append(
                ColumnInfo(
                    name=str(meta.name),
                    type_name=type_name_of(driver_name, meta.precision, meta.scale),
                    is_nullable=bool(meta.is_nullable) if meta.is_nullable is not None else True,
                )
            )
        self._allowed_columns = {c.name for c in columns}
        self._column_types = {c.name: c.type_name for c in columns}
        self._described = (base_query, [c.name for c in columns])
        return columns

    def _ensure_column_types(self, base_query: str) -> None:
        if not self._column_types:
            self.get_columns(base_query)

    def _schemas_in_scope(self) -> list[str]:
        ordered = [self._schema]
        ordered.extend(sorted({s for s in self._schema_allowlist if s != self._schema}))
        return ordered

    def get_schema_tables(self) -> list[SchemaTable]:
        """Tables and columns of the default schema plus the allowlisted ones.

        One statement over the database's ``INFORMATION_SCHEMA.COLUMNS``, which
        Snowflake already filters to what the role may see, so a schema it cannot
        read is simply absent rather than a failed browse. Tables in the default
        schema keep their bare name; others are ``schema.table``.
        """
        if not self._database:
            raise WarehouseCapabilityError("Snowflake: no database configured")
        params = Params()
        default_key = _schema_key(self._schema)
        markers = ", ".join(params.bind(_schema_key(s)) for s in self._schemas_in_scope())
        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            f"FROM {object_name(self._database)}.INFORMATION_SCHEMA.COLUMNS "
            f"WHERE table_schema IN ({markers}) "
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {_SCHEMA_ROW_LIMIT}"
        )
        _, rows = self._run(sql, params, timeout_cap=_SCHEMA_QUERY_TIMEOUT_SECONDS)
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for table_schema, table_name, column_name, data_type in rows:
            bare = str(table_name)
            qualified = bare if str(table_schema) == default_key else f"{table_schema}.{bare}"
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
        logger.debug("Snowflake preview query: %s", truncate_sql(sql))
        return self._run(sql)

    supports_json_string_columns = True

    @override
    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        """Parse STRING columns as ``VARIANT`` in a wrapper over ``base_query``.

        ``TRY_PARSE_JSON`` answers NULL for text that is not JSON instead of
        failing the statement, and a document that is not an object is NULL too,
        so either reads as a row that carries none of the keys. Every other
        column is projected unchanged, in order, from the column list
        ``get_columns`` just read.
        """
        for column in columns:
            if not IDENTIFIER_PART_RE.match(column):
                raise ValueError(f"Snowflake: invalid column name {column!r}")
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
                parsed = f"TRY_PARSE_JSON({quoted})"
                parts.append(f"IFF(IS_OBJECT({parsed}), {parsed}, NULL) AS {quoted}")
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
        """ARRAY regular columns and key-list columns back as lists."""
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

    def _timed(
        self, label: str, sql: str, params: Params | None = None
    ) -> list[tuple[object, ...]]:
        logger.debug("Snowflake %s query: %s", label, truncate_sql(sql))
        t0 = time.monotonic()
        _, rows = self._run(sql, params)
        logger.info("Snowflake %s done in %.2fs, %s rows", label, time.monotonic() - t0, len(rows))
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
        select_parts.append("COUNT(*) AS _cnt")
        # No grouping key at all is one total row: Snowflake has no ``GROUP BY ()``.
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
        group_parts = ["_bucket", *(quote_ident(c) for c in reg_cols)]
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
            "COUNT(*) AS _cnt",
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

    def _fold(self, params: Params, raw_expr: str, top_values: list[str]) -> tuple[str, str]:
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
        params: Params,
        base_query: str,
        time_column: str,
        breakdown: str,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None,
    ) -> tuple[str, str]:
        """``(breakdown_value, is_other)`` with the top-N fold; kept values are bound."""
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

        params = Params()
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
        group_parts = ["_bucket", "_breakdown_value", "_is_other"]
        for c in reg_cols:
            if c == breakdown:
                # The FOLDED value in the breakdown's own slot, never a grouping key
                # of its own: see BaseAdapter.get_time_bucketed_aggregate_breakdown.
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
        rows = self._timed("bucketed aggregate breakdown", sql, params)
        decoded = self._decode_rows(rows, offset=3, reg_cols=reg_cols, json_cols=json_cols)
        return [*reg_cols, *json_cols], json_value_names, self._utc_bucket_rows(decoded)

    def _spec_aggregate_sql(self, spec: AggregateSpec) -> str:
        """One (optionally conditional) aggregate, under the NULL-means-gap rule.

        Folded into the aggregate the way BigQuery does it, with ``COUNT_IF`` as
        the row-presence count: ``count`` -> ``NULLIF(COUNT_IF(cond), 0)``;
        ``count_distinct`` -> gated on ``COUNT_IF(cond) = 0``; the others over
        ``CASE WHEN`` are NULL over zero matching rows on their own.
        """
        measure_sql: str | None = None
        if spec.column is not None:
            measure_sql = quote_ident(validate_measure_column(spec.column, self._allowed_columns))
        agg = coerce_aggregation(spec.aggregation)
        if spec.filter_sql is None:
            return build_aggregate_sql(agg, measure_sql)
        cond = spec.filter_sql
        if agg is MetricAggregation.count:
            return f"NULLIF(COUNT_IF({cond}), 0)"
        if not measure_sql:
            msg = f"Aggregation {agg.value!r} requires a measure column"
            raise ValueError(msg)
        if agg is MetricAggregation.count_distinct:
            distinct = f"COUNT(DISTINCT IFF({cond}, {measure_sql}, NULL))"
            return f"CASE WHEN COUNT_IF({cond}) = 0 THEN NULL ELSE {distinct} END"
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
            f"WHERE {window} GROUP BY _bucket ORDER BY _bucket LIMIT {int(limit)}"
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
        params = Params()
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
            select_parts.append(f"{self._spec_aggregate_sql(spec)} AS {quote_ident(spec.key)}")
            col_names.append(spec.key)
        window = self._time_condition(tc, time_from, time_to)
        sql = (
            f"SELECT {', '.join(select_parts)} FROM ({base_query}) AS _src "
            f"WHERE {window} GROUP BY _bucket, _breakdown_value, _is_other "
            f"ORDER BY _bucket, _breakdown_value LIMIT {int(limit)}"
        )
        rows = self._timed("bucketed multi-aggregate breakdown", sql, params)
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
