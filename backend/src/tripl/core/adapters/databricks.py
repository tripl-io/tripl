from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, date, datetime
from typing import Any, override

from tripl.core.adapters.base import (
    FIELD_CONTRACT_EXPECTATIONS_PER_QUERY,
    AggregateSpec,
    BaseAdapter,
    ColumnInfo,
    FieldContractExpectation,
    FieldContractViolation,
    SchemaColumn,
    SchemaTable,
    contract_bound_literal,
    field_contract_verdict,
    is_breakdown_field_of,
)
from tripl.core.adapters.databricks_sql import (
    IDENTIFIER_PART_RE,
    Params,
    json_text,
    quote_ident,
    truncate_sql,
    validate_identifier_column,
)
from tripl.core.adapters.databricks_types import declared_struct_paths, is_array_type, is_ntz_type
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.measure_validator import (
    build_aggregate_sql,
    coerce_aggregation,
    validate_measure_column,
)
from tripl.core.bucketing import EPOCH, WEEK_ORIGIN, format_utc_literal, to_utc
from tripl.core.intervals import IntervalUnit, get_interval
from tripl.core.warehouse_types import ComplexKind, TimeKind, classify_complex, classify_time
from tripl.json_paths import split_property_field
from tripl.models.domain_enums import MetricAggregation

logger = logging.getLogger(__name__)

# Hard cap on catalog rows pulled for SQL-editor autocomplete, the same budget
# the other adapters use. One statement covers every schema in scope.
_SCHEMA_ROW_LIMIT = 50000

# Catalog introspection is a CAP, not a default: a source configuring a shorter
# timeout_seconds still wins (see ``_query_deadline``).
_SCHEMA_QUERY_TIMEOUT_SECONDS = 30

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


def _as_utc_bucket(value: object) -> object:
    """One ``_bucket`` cell as an aware UTC ``datetime``.

    Every bucket expression here returns a TIMESTAMP computed in the session time
    zone, which ``__init__`` pins to UTC, so a naive value the driver hands back
    is a UTC wall clock and is stamped as one. ``datetime`` is tested before
    ``date`` because it is a subclass of it.
    """
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value


def _count(value: object) -> int:
    """A COUNT cell as an int; an aggregate over no rows is 0, never NULL."""
    if value is None:
        return 0
    if isinstance(value, int | float | str):
        return int(value)
    msg = f"Databricks: expected a count, got {type(value).__name__}"
    raise ValueError(msg)


def _decode_json_list(value: object) -> object:
    """A grouped key list (``to_json(array_sort(...))``) back as the list it stands for."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if not isinstance(value, str):
        msg = f"Databricks: expected a JSON array string, got {type(value).__name__}"
        raise ValueError(msg)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        msg = f"Databricks: could not decode a grouped key list {value!r}: {exc}"
        raise ValueError(msg) from exc
    if decoded is not None and not isinstance(decoded, list):
        msg = f"Databricks: a grouped key list decoded to {type(decoded).__name__}, not a list"
        raise ValueError(msg)
    return decoded


def _decode_array_value(value: object) -> object:
    """An ARRAY regular column as a list; the driver renders complex values as JSON text."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, list) else value
    if isinstance(value, tuple):
        return list(value)
    return value


class DatabricksAdapter(BaseAdapter):
    """Databricks SQL warehouse (Unity Catalog) adapter.

    Connection: ``host`` is the workspace's server hostname, ``database_name``
    the default catalog, and the HTTP path, default schema and schema allowlist
    are connection settings. The credential (a personal access token, or a
    service principal's OAuth secret) is the source's encrypted ``password``.

    Semantics mirror the other SQL adapters, spelled in Databricks SQL:
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

    # Class-level defaults, for the same reason BigQueryAdapter has them: the unit
    # tests build adapters with ``object.__new__`` and seed only what they use.
    _timeout_seconds: float | None = None
    _catalog: str = ""
    _schema: str = "default"
    # Whether the source names its default schema. Only then is the session
    # opened in it: a catalog without a ``default`` schema (``samples`` has
    # none) would refuse a session pinned to one.
    _schema_explicit: bool = False
    _schema_allowlist: tuple[str, ...] = ()
    _described: tuple[str, list[str]] | None = None

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

    def _run(
        self, sql: str, params: Params | None = None, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here.

        The deadline is enforced client-side by cancelling the statement from a
        timer thread (the driver's ``cancel`` is documented to be called from
        another thread), on top of the session's ``STATEMENT_TIMEOUT``. A
        cancelled statement surfaces as a ``TimeoutError`` with a message the
        operator can act on, never as the driver's text.
        """
        deadline = self._query_deadline(timeout_cap)
        cursor = self._conn.cursor()
        fired = threading.Event()
        timer: threading.Timer | None = None
        if deadline is not None:
            timer = threading.Timer(deadline, self._cancel, args=(cursor, fired))
            timer.daemon = True
            timer.start()
        try:
            cursor.execute(sql, parameters=params.values if params and params.values else None)
            names = [str(column[0]) for column in cursor.description or []]
            rows = [tuple(row) for row in cursor.fetchall()]
        except Exception as exc:
            if fired.is_set():
                msg = (
                    f"Databricks: query exceeded the {deadline:g}s timeout configured for "
                    "this data source and was cancelled. Narrow the time window, reduce the "
                    "columns the base query selects, or raise the data source's timeout."
                )
                raise TimeoutError(msg) from exc
            raise
        finally:
            if timer is not None:
                timer.cancel()
            try:
                cursor.close()
            except Exception:
                logger.debug("Databricks: cursor close failed", exc_info=True)
        return names, rows

    def _cancel(self, cursor: Any, fired: threading.Event) -> None:
        fired.set()
        try:
            cursor.cancel()
        except Exception:
            logger.warning("Databricks: could not cancel a timed-out statement", exc_info=True)

    def test_connection(self) -> bool:
        _, rows = self._run("SELECT 1 AS ok")
        return bool(rows and rows[0][0] == 1)

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
        self._allowed_columns = {c.name for c in columns}
        self._column_types = {c.name: c.type_name for c in columns}
        self._struct_paths = {
            c.name: declared_struct_paths(c.type_name)
            for c in columns
            if classify_complex(c.type_name) is ComplexKind.struct
        }
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
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {_SCHEMA_ROW_LIMIT}"
        )
        _, rows = self._run(sql, params, timeout_cap=_SCHEMA_QUERY_TIMEOUT_SECONDS)
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for table_schema, table_name, column_name, data_type in rows:
            bare = str(table_name)
            qualified = bare if str(table_schema) == self._schema else f"{table_schema}.{bare}"
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
        logger.debug("Databricks preview query: %s", truncate_sql(sql))
        return self._run(sql)

    supports_json_string_columns = True

    @override
    def json_string_source(self, base_query: str, columns: list[str]) -> str:
        """Parse STRING columns as ``VARIANT`` in a wrapper over ``base_query``.

        ``try_parse_json`` answers NULL for text that is not JSON instead of
        failing the statement, and a document that is not an object is NULL too,
        so either reads as a row that carries none of the keys. Every other
        column is projected unchanged, in order — Databricks has no
        ``SELECT * REPLACE`` — from the column list ``get_columns`` just read.
        """
        for column in columns:
            if not IDENTIFIER_PART_RE.match(column):
                raise ValueError(f"Databricks: invalid column name {column!r}")
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
                parsed = f"try_parse_json({quoted})"
                parts.append(
                    f"CASE WHEN startswith(schema_of_variant({parsed}), 'OBJECT') "
                    f"THEN {parsed} END AS {quoted}"
                )
            else:
                parts.append(quoted)
        return f"SELECT {', '.join(parts)} FROM ({base_query}) AS _json_src"

    # ------------------------------------------------------------------ #
    # expressions
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

    def _path_parts(self, path: str) -> list[str]:
        parts = [part for part in path.split(".") if part]
        if not parts or any(not IDENTIFIER_PART_RE.match(part) for part in parts):
            raise ValueError(f"Unsupported JSON path: {path}")
        return parts

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
        parts = self._path_parts(path)
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

    def _field_operand(self, field: str) -> str:
        prop = split_property_field(field)
        if prop is None:
            return quote_ident(self._validate_column(field))
        return self._property_value_expression(*prop)

    def _field_value_expression(self, field: str) -> str:
        if split_property_field(field) is None:
            return self._string_value_expression(field)
        return f"coalesce({self._field_operand(field)}, '')"

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
        """The FROM source with every nested expression computed once, under an alias.

        The outer statement then groups by plain columns of this subquery, which
        keeps a lambda (``transform``) or a path expression out of the GROUP BY
        entirely. With no nested columns the source is the bare
        ``(base_query) AS _src`` and the WHERE clause follows it.
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
        inner = f"SELECT *, {', '.join(prepared)} FROM ({base_query}) AS _src{where_clause}"
        return f"({inner}) AS _prepared", alias_by_name, json_value_names

    def _nested_select_group(
        self, names: list[str], alias_by_name: dict[str, str]
    ) -> tuple[list[str], list[str]]:
        select_parts = [f"{alias_by_name[name]} AS {quote_ident(name)}" for name in names]
        group_parts = [alias_by_name[name] for name in names]
        return select_parts, group_parts

    def _decode_rows(
        self,
        rows: list[tuple[object, ...]],
        *,
        offset: int,
        reg_cols: list[str],
        json_cols: list[str],
    ) -> list[tuple[object, ...]]:
        """ARRAY regular columns and key-list columns back as lists; STRUCT paths filled in."""
        decoders: dict[int, Any] = {}
        for index, column in enumerate(reg_cols):
            if is_array_type(self._column_types.get(column, "")):
                decoders[offset + index] = _decode_array_value
        for index, column in enumerate(json_cols):
            position = offset + len(reg_cols) + index
            if column in self._struct_paths or (
                classify_complex(self._column_types.get(column, "")) is ComplexKind.struct
            ):
                declared = list(self._struct_paths.get(column, {}))
                decoders[position] = lambda _value, paths=declared: list(paths)
            else:
                decoders[position] = _decode_json_list
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
        return [(_as_utc_bucket(row[0]), *row[1:]) for row in rows]

    # ------------------------------------------------------------------ #
    # field contracts
    # ------------------------------------------------------------------ #

    @override
    def _probe_contract_regex(self, pattern: str) -> None:
        """Have the warehouse compile the pattern as a Java regex, reading no table."""
        params = Params()
        self._run(f"SELECT '' RLIKE {params.bind(pattern)}", params)

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
            # coalesce(CAST(col AS STRING), '') = value — the NULL-as-empty-string
            # comparison ClickHouse makes with ifNull(toString(col), '').
            expected = params.bind(group_value or "")
            conditions.append(f"{self._string_value_expression(group_column)} = {expected}")
        if not conditions:
            return ""
        return " WHERE " + " AND ".join(conditions)

    def _contract_bad_condition(
        self, expectation: FieldContractExpectation, params: Params
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
        value_expr = f"coalesce(CAST({operand} AS STRING), '')"
        present = f"{operand} IS NOT NULL"
        drift_type = expectation.drift_type
        if drift_type == "required_null_violation":
            return f"{operand} IS NULL"
        if drift_type == "enum_violation":
            options = ", ".join(params.bind(option) for option in expectation.enum_options)
            return f"{present} AND {value_expr} NOT IN ({options})"
        if drift_type == "regex_violation":
            assert expectation.regex is not None
            if not self.contract_regex_is_compilable(expectation.regex):
                self._skip_field_contract(expectation)
                return None
            return f"{present} AND NOT ({value_expr} RLIKE {params.bind(expectation.regex)})"
        if drift_type == "range_violation":
            number = f"try_cast({value_expr} AS DOUBLE)"
            outside: list[str] = []
            if expectation.min_value is not None:
                outside.append(f"{number} < {contract_bound_literal(expectation.min_value)}")
            if expectation.max_value is not None:
                outside.append(f"{number} > {contract_bound_literal(expectation.max_value)}")
            # Unparseable is BAD; NaN is never compared (Databricks sorts it above
            # every number, Python compares it false both ways).
            return (
                f"{present} AND ({number} IS NULL OR "
                f"(NOT isnan({number}) AND ({' OR '.join(outside)})))"
            )
        return None

    def _contract_aggregate_sql(
        self, expectation: FieldContractExpectation, bad: str, *, index: int
    ) -> str:
        """``_bad_{i}``, ``_total_{i}``, ``_sample_{i}`` — the layout every engine shares."""
        operand = self._field_operand(expectation.field_name)
        is_required = expectation.drift_type == "required_null_violation"
        total = "count(*)" if is_required else f"count_if({operand} IS NOT NULL)"
        sample = "'<NULL>'" if is_required else f"coalesce(CAST({operand} AS STRING), '')"
        return (
            f"count_if({bad}) AS _bad_{index}, "
            f"{total} AS _total_{index}, "
            f"min(IF({bad}, {sample}, NULL)) AS _sample_{index}"
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
        params = Params()
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

        violations: list[FieldContractViolation] = []
        t0 = time.monotonic()
        for start in range(0, len(compiled), FIELD_CONTRACT_EXPECTATIONS_PER_QUERY):
            chunk = compiled[start : start + FIELD_CONTRACT_EXPECTATIONS_PER_QUERY]
            selects = ", ".join(
                self._contract_aggregate_sql(expectation, condition, index=index)
                for index, (expectation, condition) in enumerate(chunk)
            )
            sql = f"SELECT {selects} FROM ({base_query}) AS _src{where_clause}"
            logger.debug("Databricks field contract query: %s", truncate_sql(sql))
            _, rows = self._run(sql, params)
            if not rows:
                continue
            row = rows[0]
            for index, (expectation, _condition) in enumerate(chunk):
                sample = row[index * 3 + 2]
                violation = field_contract_verdict(
                    expectation,
                    bad_count=_count(row[index * 3]),
                    total_count=_count(row[index * 3 + 1]),
                    sample_value=None if sample is None else str(sample),
                )
                if violation is not None:
                    violations.append(violation)
        logger.info(
            "Databricks field contracts done in %.2fs, %s violations",
            time.monotonic() - t0,
            len(violations),
        )
        return violations[: max(0, int(limit))]

    # ------------------------------------------------------------------ #
    # scans and counts
    # ------------------------------------------------------------------ #

    def _timed(
        self, label: str, sql: str, params: Params | None = None
    ) -> list[tuple[object, ...]]:
        logger.debug("Databricks %s query: %s", label, truncate_sql(sql))
        t0 = time.monotonic()
        _, rows = self._run(sql, params)
        logger.info("Databricks %s done in %.2fs, %s rows", label, time.monotonic() - t0, len(rows))
        return rows

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
        for names in (json_cols, json_value_names):
            nested_select, nested_group = self._nested_select_group(names, alias_by_name)
            select_parts.extend(nested_select)
            group_parts.extend(nested_group)
        select_parts.append("count(*) AS _cnt")
        group_by = ", ".join(group_parts) if group_parts else "()"
        sql = (
            f"SELECT {', '.join(select_parts)} FROM {from_sql} "
            f"GROUP BY {group_by} ORDER BY _cnt DESC LIMIT {int(limit)}"
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
        for names in (json_cols, json_value_names):
            nested_select, nested_group = self._nested_select_group(names, alias_by_name)
            select_parts.extend(nested_select)
            group_parts.extend(nested_group)
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

    def _breakdown_value_exprs(
        self,
        params: Params,
        base_query: str,
        time_column: str,
        breakdown: str,
        raw_expr: str,
        time_from: datetime,
        time_to: datetime,
        values_limit: int | None,
    ) -> tuple[str, str]:
        """``(breakdown_value, is_other)`` with the top-N fold; kept values are bound."""
        if values_limit is None:
            return raw_expr, "0"
        top_values = self._top_breakdown_values_multi(
            base_query, time_column, [breakdown], time_from, time_to, max(values_limit - 1, 0)
        ).get(breakdown, [])
        return self._fold(params, raw_expr, top_values)

    def _fold(self, params: Params, raw_expr: str, top_values: list[str]) -> tuple[str, str]:
        if not top_values:
            return "'Other'", "1"
        markers = ", ".join(params.bind(value) for value in top_values)
        in_clause = f"{raw_expr} IN ({markers})"
        return (
            f"CASE WHEN {in_clause} THEN {raw_expr} ELSE 'Other' END",
            f"CASE WHEN {in_clause} THEN 0 ELSE 1 END",
        )

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
            params,
            base_query,
            time_column,
            breakdown,
            self._string_value_expression(breakdown),
            time_from,
            time_to,
            values_limit,
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
                # The FOLDED value in the breakdown's own slot, and never a
                # grouping key of its own: see
                # BaseAdapter.get_time_bucketed_aggregate_breakdown. The fold is
                # repeated rather than the alias reused; it is semantically the
                # expression ``_breakdown_value`` groups by.
                select_parts.append(f"{breakdown_expr} AS {quote_ident(c)}")
            else:
                select_parts.append(quote_ident(c))
                group_parts.append(quote_ident(c))
        for names in (json_cols, json_value_names):
            nested_select, nested_group = self._nested_select_group(names, alias_by_name)
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
            params,
            base_query,
            time_column,
            breakdown,
            self._string_value_expression(breakdown),
            time_from,
            time_to,
            values_limit,
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
        params = Params()
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
            # The value tie-break the BaseAdapter top-N contract requires. A
            # Databricks STRING compares under UTF8_BINARY unless a column says
            # otherwise, which is byte order — code-point order over UTF-8.
            "ROW_NUMBER() OVER (PARTITION BY _breakdown_column "
            "ORDER BY _cnt DESC, _breakdown_value) AS rn "
            "FROM ("
            f"SELECT CASE {label_branches} ELSE '' END AS _breakdown_column, "
            f"CASE {value_branches} ELSE '' END AS _breakdown_value, "
            "count(*) AS _cnt "
            f"FROM (SELECT {', '.join(prepared)} FROM ({base_query}) AS _src WHERE {window}) "
            "AS _prepared "
            f"GROUP BY GROUPING SETS ({grouping_sets})"
            ") AS _scored"
            ") AS _ranked "
            f"WHERE rn <= {int(limit)}"
        )
        logger.debug("Databricks breakdown top-values query: %s", truncate_sql(sql))
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

        params = Params()
        prepared = [f"{self._bucket_expression(tc, interval)} AS _bucket"]
        prepared.extend(f"{quote_ident(c)} AS {quote_ident(c)}" for c in reg_cols)
        prepared.extend(f"{self._json_paths_expression(c)} AS {quote_ident(c)}" for c in json_cols)
        json_value_names: list[str] = []
        for c in json_cols:
            for path in json_value_paths.get(c, []):
                full_path = f"{c}.{path}"
                prepared.append(
                    f"{self._json_path_expression(c, path)} AS {quote_ident(full_path)}"
                )
                json_value_names.append(full_path)
        grouping_columns = [quote_ident(n) for n in [*reg_cols, *json_cols, *json_value_names]]

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
            value_when.append(f"WHEN {check} THEN CAST({value_alias} AS STRING)")
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
            "count(*) AS _cnt",
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
