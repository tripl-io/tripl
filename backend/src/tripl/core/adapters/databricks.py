from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, override

from tripl.core.adapters.base import ColumnInfo, SchemaColumn, SchemaTable
from tripl.core.adapters.databricks_sql import Params, json_text, quote_ident
from tripl.core.adapters.databricks_types import declared_struct_paths, is_ntz_type
from tripl.core.adapters.dialect_sql_adapter import DialectSqlAdapter, ValueBinder
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.sql_common import (
    SCHEMA_QUERY_TIMEOUT_SECONDS,
    SCHEMA_ROW_LIMIT,
    json_path_parts,
)
from tripl.core.bucketing import EPOCH, WEEK_ORIGIN, format_utc_literal, to_utc
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.core.warehouse_types import ComplexKind, TimeKind, classify_complex, classify_time
from tripl.json_paths import split_property_field

# Intervals finer than a day cannot be expressed against a DATE column.
_SUB_DAY_UNITS = (IntervalUnit.minute, IntervalUnit.hour)

# ``date_trunc`` unit per interval unit, for the intervals whose count is 1.
# Databricks truncates a WEEK to its Monday, which is the contract's week start.
_TRUNC_UNIT = {
    IntervalUnit.minute: "MINUTE",
    IntervalUnit.hour: "HOUR",
    IntervalUnit.day: "DAY",
    IntervalUnit.week: "WEEK",
}

_NTZ_LITERAL_FMT = "%Y-%m-%d %H:%M:%S.%f"
_DATE_LITERAL_FMT = "%Y-%m-%d"

# An empty ARRAY<STRING>, typed so ``coalesce`` and ``array_sort`` resolve.
_EMPTY_KEYS = "CAST(array() AS ARRAY<STRING>)"


class DatabricksAdapter(DialectSqlAdapter):
    """Databricks SQL warehouse (Unity Catalog) adapter.

    Connection: ``host`` is the workspace's server hostname, ``database_name``
    the default catalog, and the HTTP path, default schema and schema allowlist
    are connection settings. The credential (a personal access token, or a
    service principal's OAuth secret) is the source's encrypted ``password``.

    The statements are :class:`~tripl.core.adapters.dialect_sql_adapter.DialectSqlAdapter`'s,
    spelled in Databricks SQL:
      - toStartOfInterval → ``date_trunc`` for the 1h/1d/1w intervals (Databricks
                            truncates a week to its Monday) and epoch arithmetic
                            (``timestamp_seconds(floor(unix_seconds(t) / w) * w)``)
                            for 15m/6h, both over the column cast to TIMESTAMP in
                            a session pinned to UTC
      - JSONAllPaths      → the sorted TOP-LEVEL keys of a VARIANT document
                            (``json_object_keys``) or of a MAP (``map_keys``), and
                            the declared field schema of a STRUCT
      - GROUPING SETS     → native syntax
      - LIMIT n BY col    → ROW_NUMBER() OVER (PARTITION BY ...) wrapper
      - countIf / anyIf   → ``count_if`` / ``min(IF(...))``
      - toFloat64OrNull   → ``try_cast(... AS DOUBLE)`` (ANSI mode makes a bare
                            CAST raise on a malformed value)

    Every value that comes from data or from an analyst — a top-N breakdown
    value, a grouped event-type value, an enum option, a contract regex — is a
    bound named parameter (``:p0``), never text in the statement. Identifiers go
    through :func:`~tripl.core.adapters.databricks_sql.quote_ident`, and JSON
    path segments are held to the identifier grammar before they are spelled.
    ``AggregateSpec.filter_sql`` is the one exception, as on every engine: a
    validated boolean fragment injected as-is.

    **Declared divergences.**

    * Scan-time JSON shapes are top-level keys only, the same trade PostgreSQL
      makes: there is no per-row nested-path primitive to group by. Nested paths
      are still discovered for the path picker, from sampled rows
      (``BaseAdapter.get_json_path_samples``), and any nested path can be
      extracted.
    * Contract regexes are Java regular expressions (``RLIKE``, an unanchored
      find, like ``re.search``). Java accepts lookaround and backreferences, and
      refuses Python's ``(?P<name>...)`` named group; a refused pattern is probed
      and dropped like on every engine.
    * Range contracts parse with ``try_cast(... AS DOUBLE)``, which follows
      Java's number grammar: it also reads ``1d`` / ``1f`` suffixes and hex
      floats that Python's ``float`` refuses. NaN is kept out of the comparison
      explicitly, because Databricks orders NaN above every number.
    * Fields of an ``ARRAY<STRUCT<...>>`` are listed but cannot be addressed, and
      a MAP has one level of keys.
    """

    engine_label = "Databricks"

    _ROW_NUMBER = "ROW_NUMBER"

    # Class-level defaults, for the same reason BigQueryAdapter has them: the unit
    # tests build adapters with ``object.__new__`` and seed only what they use.
    _catalog: str = ""
    _schema: str = "default"
    # Whether the source names its default schema. Only then is the session
    # opened in it: a catalog without a ``default`` schema (``samples`` has
    # none) would refuse a session pinned to one.
    _schema_explicit: bool = False
    _schema_allowlist: tuple[str, ...] = ()

    def __init__(
        self,
        host: str,
        port: int,  # always 443: the warehouse is reached over HTTPS
        database: str,
        username: str = "",
        password: str = "",
        *,
        http_path: str,
        auth_type: str = "pat",
        schema: str | None = None,
        schema_allowlist: list[str] | None = None,
        timeout_seconds: int | None = None,
        **kwargs: object,
    ) -> None:
        del port, kwargs
        if not host:
            raise WarehouseCapabilityError("Databricks: host (the workspace hostname) is required")
        if not http_path:
            raise WarehouseCapabilityError(
                "Databricks: the SQL warehouse HTTP path is required "
                "(Connection details of the warehouse, e.g. /sql/1.0/warehouses/1234abcd)"
            )
        if not password:
            raise WarehouseCapabilityError(
                "Databricks: a personal access token or an OAuth client secret is required"
            )
        if auth_type == "oauth_m2m" and not username:
            raise WarehouseCapabilityError(
                "Databricks: OAuth machine-to-machine sign-in needs the service principal's "
                "client ID in the username field"
            )

        self._timeout_seconds = float(timeout_seconds) if timeout_seconds else None
        self._catalog = database
        self._schema = schema or "default"
        self._schema_explicit = bool(schema)
        self._schema_allowlist = tuple(schema_allowlist or ())
        self._allowed_columns: set[str] = set()
        self._column_types: dict[str, str] = {}
        self._struct_paths: dict[str, dict[str, bool]] = {}
        self._described = None
        self._conn = self._connect(host, http_path, username, password, auth_type)

    def _connect(
        self, host: str, http_path: str, username: str, password: str, auth_type: str
    ) -> Any:
        from tripl.core.adapters.databricks_sql import import_driver

        sql = import_driver()
        timeout = self._timeout_seconds
        credentials: dict[str, object]
        if auth_type == "oauth_m2m":
            from tripl.core.adapters.databricks_auth import ServicePrincipalCredentials

            credentials = {
                "credentials_provider": ServicePrincipalCredentials(
                    host, username, password, timeout=timeout or 60.0
                )
            }
        else:
            credentials = {"access_token": password}
        # TIMEZONE pins the session to UTC (window literals, date_trunc and the
        # TIMESTAMP_NTZ -> TIMESTAMP cast are all read in it), and STATEMENT_TIMEOUT
        # makes the warehouse itself cancel a runaway statement — the server-side
        # half of the deadline, which holds even if this worker is killed before
        # its own cancel (``_run``) fires.
        session: dict[str, str] = {"TIMEZONE": "UTC"}
        if timeout is not None:
            session["STATEMENT_TIMEOUT"] = str(int(timeout))
        kwargs: dict[str, object] = {
            "server_hostname": host,
            "http_path": http_path,
            "catalog": self._catalog or None,
            "schema": self._schema if self._schema_explicit else None,
            "session_configuration": session,
            # Complex values (STRUCT/ARRAY/MAP/VARIANT) come back as JSON text,
            # which is what the decoders here and ``decode_json_path_value`` read.
            "_use_arrow_native_complex_types": False,
            # Results stream inline from the warehouse host: cloud fetch would
            # download them from presigned cloud-storage URLs, a second host the
            # outbound rule could not vet. It needs pyarrow anyway.
            "use_cloud_fetch": False,
            "enable_telemetry": False,
            "user_agent_entry": "tripl",
        }
        if timeout is not None:
            # Bounds each socket operation and the driver's own retry loop, so a
            # warehouse that is starting up cannot hold the connect past the budget.
            kwargs["_socket_timeout"] = timeout
            kwargs["_retry_stop_after_attempts_duration"] = max(1.0, timeout)
        kwargs.update(credentials)
        return sql.connect(**kwargs)

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    def _run(
        self, sql: str, params: ValueBinder | None = None, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here.

        The deadline is enforced client-side by cancelling the statement from a
        timer thread (the driver's ``cancel`` is documented to be called from
        another thread), on top of the session's ``STATEMENT_TIMEOUT``. A
        cancelled statement surfaces as a ``TimeoutError`` with a message the
        operator can act on, never as the driver's text.
        """
        values = params.values if isinstance(params, Params) and params.values else None
        with self._cursor_under_deadline(self._query_deadline(timeout_cap)) as cursor:
            cursor.execute(sql, parameters=values)
            names = [str(column[0]) for column in cursor.description or []]
            rows = [tuple(row) for row in cursor.fetchall()]
        return names, rows

    def _probe_contract_regex(self, pattern: str) -> None:
        """Have the warehouse compile the pattern as a Java regex, reading no table."""
        params = Params()
        self._run(f"SELECT '' RLIKE {params.bind(pattern)}", params)

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def get_columns(self, base_query: str) -> list[ColumnInfo]:
        """Columns and FULL types, from ``DESCRIBE QUERY`` (the statement does not run).

        The cursor description names a STRUCT column's type as plain ``struct``;
        only ``DESCRIBE QUERY`` prints its fields, and the nested-path SQL is
        type-directed. Nullability is not reported, so every column is nullable.
        """
        _, rows = self._run(f"DESCRIBE QUERY SELECT * FROM ({base_query}) AS _src")
        columns: list[ColumnInfo] = []
        for row in rows:
            name = "" if row[0] is None else str(row[0])
            if not name or name.startswith("#"):
                continue
            columns.append(ColumnInfo(name=name, type_name=str(row[1]), is_nullable=True))
        self._struct_paths = {
            c.name: declared_struct_paths(c.type_name)
            for c in columns
            if classify_complex(c.type_name) is ComplexKind.struct
        }
        return self._remember_columns(base_query, columns)

    def _schemas_in_scope(self) -> list[str]:
        ordered = [self._schema]
        ordered.extend(sorted({s for s in self._schema_allowlist if s != self._schema}))
        return ordered

    def get_schema_tables(self) -> list[SchemaTable]:
        """Tables and columns of the default schema plus the allowlisted ones.

        One statement over the catalog's ``information_schema.columns``, which
        Unity Catalog already filters to what the credential may see, so a schema
        it cannot read is simply absent rather than a failed browse. Tables in the
        default schema keep their bare name; tables in another schema of the
        catalog are ``schema.table``, the one-dot convention the editor expects.
        """
        if not self._catalog:
            raise WarehouseCapabilityError("Databricks: no catalog configured")
        params = Params()
        schemas = self._schemas_in_scope()
        markers = ", ".join(params.bind(schema) for schema in schemas)
        sql = (
            "SELECT table_schema, table_name, column_name, data_type "
            f"FROM {quote_ident(self._catalog)}.information_schema.columns "
            f"WHERE table_schema IN ({markers}) "
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {SCHEMA_ROW_LIMIT}"
        )
        _, rows = self._run(sql, params, timeout_cap=SCHEMA_QUERY_TIMEOUT_SECONDS)
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for table_schema, table_name, column_name, data_type in rows:
            bare = str(table_name)
            qualified = bare if str(table_schema) == self._schema else f"{table_schema}.{bare}"
            columns_by_table.setdefault(qualified, []).append(
                SchemaColumn(name=str(column_name), data_type=str(data_type))
            )
        return [SchemaTable(name=name, columns=cols) for name, cols in columns_by_table.items()]

    # ------------------------------------------------------------------ #
    # expressions
    # ------------------------------------------------------------------ #

    def _new_params(self) -> Params:
        return Params()

    def _quote_ident(self, name: str) -> str:
        return quote_ident(name)

    def _time_kind(self, time_column: str) -> TimeKind:
        type_name = self._column_types.get(time_column)
        if type_name is None:
            return TimeKind.timestamp
        kind = classify_time(type_name)
        if kind is TimeKind.unsupported:
            msg = (
                f"Databricks: time column {time_column!r} has type {type_name}, which "
                "carries no date and cannot be used as a time column. "
                "Use a TIMESTAMP, TIMESTAMP_NTZ or DATE column."
            )
            raise WarehouseCapabilityError(msg)
        return kind

    def _time_literal(self, time_column: str, value: datetime) -> str:
        """A UTC instant as a literal of the column's own type family.

        ``TIMESTAMP`` carries the explicit ``+00:00`` offset; ``TIMESTAMP_NTZ`` is
        a zone-less wall clock and gets the UTC wall clock with no offset; a DATE
        column compares against the bound's UTC day (the 1d/1w windows a DATE
        supports are day-aligned, so that floor is exact).
        """
        moment = to_utc(value)
        kind = self._time_kind(time_column)
        if kind is TimeKind.date:
            return f"DATE '{moment.strftime(_DATE_LITERAL_FMT)}'"
        if is_ntz_type(self._column_types.get(time_column, "")):
            return f"TIMESTAMP_NTZ '{moment.strftime(_NTZ_LITERAL_FMT)}'"
        return f"TIMESTAMP '{format_utc_literal(moment)}'"

    def _bucket_expression(self, time_column: str, interval_code: str) -> str:
        """Translate an interval code into Databricks SQL; agrees with ``floor_to_bucket``.

        The column is cast to TIMESTAMP first, so a TIMESTAMP_NTZ wall clock and a
        DATE bucket in the same UTC session as a TIMESTAMP. A count of 1 truncates
        (a WEEK truncates to Monday); any other count floors the Unix seconds onto
        a grid anchored at the epoch, or at ``WEEK_ORIGIN`` for weeks.
        """
        spec = get_interval(interval_code)
        kind = self._time_kind(time_column)
        if kind is TimeKind.date and spec.unit in _SUB_DAY_UNITS:
            msg = (
                f"Databricks: time column {time_column!r} is a DATE, which has no "
                f"time-of-day, so it cannot be bucketed at {interval_code!r}. "
                "Use the 1d or 1w interval, or a TIMESTAMP column."
            )
            raise WarehouseCapabilityError(msg)
        moment = f"CAST({quote_ident(self._validate_column(time_column))} AS TIMESTAMP)"
        if spec.count == 1:
            return f"date_trunc('{_TRUNC_UNIT[spec.unit]}', {moment})"
        width = int(spec.delta.total_seconds())
        origin = int((WEEK_ORIGIN if spec.unit is IntervalUnit.week else EPOCH).timestamp())
        offset = f" - {origin}" if origin else ""
        restore = f" + {origin}" if origin else ""
        return (
            f"timestamp_seconds(floor((unix_seconds({moment}){offset}) / {width}) * {width}"
            f"{restore})"
        )

    def _complex_kind(self, column: str) -> ComplexKind:
        type_name = self._column_types.get(column)
        if type_name is None:
            return ComplexKind.json
        kind = classify_complex(type_name)
        if kind is None:
            msg = (
                f"Databricks: column {column!r} has scalar type {type_name} and holds no "
                "nested paths. Only VARIANT, STRUCT and MAP columns can be path-expanded "
                "(or a STRING column the scan parses as JSON)."
            )
            raise ValueError(msg)
        return kind

    def _struct_field_expression(self, column: str, parts: list[str]) -> str:
        declared = self._struct_paths.get(column, {})
        path = ".".join(parts)
        addressable = declared.get(path)
        if addressable is None:
            known = ", ".join(declared) or "<none>"
            msg = (
                f"Databricks: {path!r} is not a declared field of STRUCT column "
                f"{column!r}. Known paths: {known}"
            )
            raise ValueError(msg)
        if not addressable:
            msg = (
                f"Databricks: STRUCT path {column}.{path} is a field of an ARRAY of structs. "
                "It needs explode or transform, which this adapter does not generate. "
                "Select a path outside the array."
            )
            raise ValueError(msg)
        return quote_ident(column) + "".join(f".{quote_ident(part)}" for part in parts)

    def _path_value(self, column: str, path: str) -> tuple[str, bool]:
        """``(expression, is_variant)`` for the value at ``path`` of a nested column."""
        parts = json_path_parts(path)
        col = self._validate_column(column)
        kind = self._complex_kind(col)
        if kind is ComplexKind.struct:
            return self._struct_field_expression(col, parts), False
        if kind is ComplexKind.map:
            if len(parts) != 1:
                msg = (
                    f"Databricks: MAP column {col!r} holds one level of keys, so {path!r} "
                    "cannot be addressed. Select a top-level key."
                )
                raise ValueError(msg)
            return f"try_element_at({quote_ident(col)}, '{parts[0]}')", False
        # VARIANT: the colon path with bracketed keys, which match a key exactly
        # (case included), the way a JSON key is matched on every other engine.
        return quote_ident(col) + ":" + "".join(f"['{part}']" for part in parts), True

    def _json_path_expression(self, column: str, path: str) -> str:
        """The value at ``path`` as JSON text — the grouped ``keep_json_value`` column."""
        expr, is_variant = self._path_value(column, path)
        return f"to_json({expr})" if is_variant else json_text(expr)

    def _property_value_expression(self, column: str, path: str) -> str:
        """One property as a nullable STRING: the scalar itself, NULL when absent or null."""
        expr, _is_variant = self._path_value(column, path)
        return f"CAST({expr} AS STRING)"

    def _json_paths_expression(self, column: str) -> str:
        """A nested column's key set as a groupable JSON array string.

        A STRUCT's paths are declared by the schema and identical for every row, so
        the statement groups by a constant and ``_decode_rows`` fills the declared
        list in on the way out instead of shipping it through SQL.
        """
        col = self._validate_column(column)
        quoted = quote_ident(col)
        kind = self._complex_kind(col)
        if kind is ComplexKind.struct:
            return "CAST(NULL AS STRING)"
        if kind is ComplexKind.map:
            keys = f"transform(map_keys({quoted}), _k -> CAST(_k AS STRING))"
        else:
            keys = f"json_object_keys(to_json({quoted}))"
        return f"to_json(array_sort(coalesce({keys}, {_EMPTY_KEYS})))"

    def _string_value_expression(self, column: str) -> str:
        return f"coalesce(CAST({quote_ident(self._validate_column(column))} AS STRING), '')"

    def _field_value_expression(self, field: str) -> str:
        if split_property_field(field) is None:
            return self._string_value_expression(field)
        return f"coalesce({self._field_operand(field)}, '')"

    def _json_object_or_null(self, quoted: str) -> str:
        parsed = f"try_parse_json({quoted})"
        return f"CASE WHEN startswith(schema_of_variant({parsed}), 'OBJECT') THEN {parsed} END"

    def _cast_text(self, expr: str) -> str:
        return f"CAST({expr} AS STRING)"

    # ------------------------------------------------------------------ #
    # result decoding
    # ------------------------------------------------------------------ #

    @override
    def _key_list_decoder(self, column: str) -> Callable[[object], object]:
        """A STRUCT's paths are declared, not grouped: its declared list fills the cell."""
        if column in self._struct_paths or (
            classify_complex(self._column_types.get(column, "")) is ComplexKind.struct
        ):
            declared = list(self._struct_paths.get(column, {}))
            return lambda _value: list(declared)
        return super()._key_list_decoder(column)

    # ------------------------------------------------------------------ #
    # field contracts
    # ------------------------------------------------------------------ #

    def _contract_text(self, field: str) -> str:
        return f"coalesce({self._cast_text(self._field_operand(field))}, '')"

    def _regex_mismatch(self, value: str, pattern: str) -> str:
        # RLIKE finds the pattern anywhere, like re.search.
        return f"NOT ({value} RLIKE {pattern})"

    def _try_double(self, value: str) -> str:
        # ANSI mode makes a bare CAST raise on a malformed value.
        return f"try_cast({value} AS DOUBLE)"

    def _nan_guard(self, number: str) -> str:
        # Databricks sorts NaN above every number; Python compares it false both
        # ways, so it is never compared at all.
        return f"NOT isnan({number})"
