from __future__ import annotations

import math
from typing import Any, override

from tripl.core.adapters.base import ColumnInfo, SchemaColumn, SchemaTable
from tripl.core.adapters.dialect_sql_adapter import ValueBinder
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.sql_common import SCHEMA_QUERY_TIMEOUT_SECONDS, SCHEMA_ROW_LIMIT
from tripl.core.adapters.trino_expr import TrinoExpressions
from tripl.core.adapters.trino_sql import quote_ident, quote_literal

#: Trino's error name for a query that ran past ``query_max_run_time``.
_TIME_LIMIT_ERRORS = frozenset({"EXCEEDED_TIME_LIMIT", "USER_CANCELED"})


class TrinoAdapter(TrinoExpressions):
    """Trino (and Starburst) adapter, over the ``trino`` client's DB-API.

    Connection: ``host`` and ``port`` reach the coordinator, ``database_name``
    is the catalog, ``username`` the user. The secret is the user's password
    (HTTP basic auth, over HTTPS only); an empty one connects without
    authentication, which a local or in-cluster coordinator often runs with.
    The scheme, default schema and schema allowlist are connection settings.

    The statements are :class:`~tripl.core.adapters.dialect_sql_adapter.DialectSqlAdapter`'s,
    spelled in Trino SQL (``trino_expr``):
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
    * ``json`` and ``map`` columns are path-expanded (a map through its JSON
      cast, so its keys are properties, as on ClickHouse and Databricks), and
      so are varchar columns parsed as JSON. A ``row`` or ``array`` column is
      read as a value, rendered as JSON text. Athena reports a map column's
      type as a bare ``map``, which names no key type, so there a map is read
      as a value too.
    """

    # Class-level defaults, for the same reason BigQueryAdapter has them: the unit
    # tests build adapters with ``object.__new__`` and seed only what they use.
    _catalog: str = ""
    _schema: str | None = None
    _schema_allowlist: tuple[str, ...] = ()

    # The client's ``cancel`` does nothing until the coordinator names the query.
    _cancel_waits_for_query_id = True

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

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    @override
    def _is_timeout(self, exc: Exception) -> bool:
        if getattr(exc, "error_name", None) in _TIME_LIMIT_ERRORS:
            return True
        import requests

        return isinstance(exc, requests.exceptions.Timeout)

    def _run(
        self, sql: str, params: ValueBinder | None = None, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here.

        Both halves of the deadline stop the statement: the coordinator cancels
        it past ``query_max_run_time`` (the source's timeout), and the timer of
        ``_cursor_under_deadline`` cancels it at the deadline of THIS call, which
        a ``timeout_cap`` can make shorter than the source's (the schema browse
        has one). A statement that ran out of time surfaces as
        a ``TimeoutError`` with a message the operator can act on, never as the
        driver's text. ``params`` carries nothing to send: every value is
        already a literal in ``sql`` (see ``trino_sql``).
        """
        del params
        with self._cursor_under_deadline(self._query_deadline(timeout_cap)) as cursor:
            cursor.execute(sql)
            rows = [tuple(row) for row in cursor.fetchall()]
            names = [str(column[0]) for column in cursor.description or []]
        return names, rows

    def _describe(self, sql: str) -> list[tuple[str, str]]:
        """``(name, type)`` of every result column of ``sql`` (run with ``LIMIT 0``)."""
        with self._cursor_under_deadline(self._query_deadline()) as cursor:
            cursor.execute(sql)
            cursor.fetchall()
            description = list(cursor.description or [])
        return [(str(column[0]), str(column[1])) for column in description]

    def _probe_contract_regex(self, pattern: str) -> None:
        """Have the engine compile the pattern, reading no table."""
        self._run(f"SELECT regexp_like('', {quote_literal(pattern)})")

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
        return self._remember_columns(base_query, columns)

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
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {SCHEMA_ROW_LIMIT}"
        )
        _, rows = self._run(sql, timeout_cap=SCHEMA_QUERY_TIMEOUT_SECONDS)
        default_schema = (self._schema or "").lower()
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for table_schema, table_name, column_name, data_type in rows:
            bare = str(table_name)
            qualified = bare if str(table_schema) == default_schema else f"{table_schema}.{bare}"
            columns_by_table.setdefault(qualified, []).append(
                SchemaColumn(name=str(column_name), data_type=str(data_type))
            )
        return [SchemaTable(name=name, columns=cols) for name, cols in columns_by_table.items()]
