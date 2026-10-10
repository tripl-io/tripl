from __future__ import annotations

import math
from typing import Any

from tripl.core.adapters.base import ColumnInfo, SchemaColumn, SchemaTable
from tripl.core.adapters.dialect_sql_adapter import ValueBinder
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.adapters.snowflake_expr import SnowflakeExpressions
from tripl.core.adapters.snowflake_sql import (
    Params,
    field_type_name,
    object_name,
    type_name_of,
)
from tripl.core.adapters.sql_common import SCHEMA_QUERY_TIMEOUT_SECONDS, SCHEMA_ROW_LIMIT

# Snowflake's error numbers for a statement that was cancelled (the driver's own
# deadline cancels it) and for one that hit STATEMENT_TIMEOUT_IN_SECONDS.
_CANCELLED_ERRNOS = frozenset({604, 630})


def _schema_key(name: str) -> str:
    """How ``information_schema`` spells a schema name a user typed.

    An unquoted identifier is stored upper-cased (``public`` is ``PUBLIC``);
    any other name was created quoted and is stored exactly as written.
    """
    return name.upper() if object_name(name) == name else name


class SnowflakeAdapter(SnowflakeExpressions):
    """Snowflake adapter.

    Connection: ``host`` is the account identifier (or its
    ``*.snowflakecomputing.com`` hostname), ``database_name`` the database,
    ``username`` the user. The warehouse, role, default schema and schema
    allowlist are connection settings. The secret is the source's encrypted
    ``password``: the user's password, or the PEM private key of a key-pair user.

    The statements are :class:`~tripl.core.adapters.dialect_sql_adapter.DialectSqlAdapter`'s,
    spelled in Snowflake SQL (``snowflake_expr``):
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
    _database: str = ""
    _schema: str = "PUBLIC"
    _schema_allowlist: tuple[str, ...] = ()

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

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    def _run(
        self, sql: str, params: ValueBinder | None = None, *, timeout_cap: float | None = None
    ) -> tuple[list[str], list[tuple[object, ...]]]:
        """Execute one statement under the deadline; every statement goes through here.

        The driver cancels the statement itself when ``timeout`` elapses, on top
        of the session's ``STATEMENT_TIMEOUT_IN_SECONDS``. A cancelled statement
        surfaces as a ``TimeoutError`` with a message the operator can act on,
        never as the driver's text.
        """
        deadline = self._query_deadline(timeout_cap)
        values = params.values if isinstance(params, Params) and params.values else None
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                sql,
                values,
                timeout=math.ceil(deadline) if deadline is not None else None,
            )
            names = [str(column[0]) for column in cursor.description or []]
            rows = [tuple(row) for row in cursor.fetchall()]
        except Exception as exc:
            if deadline is not None and getattr(exc, "errno", None) in _CANCELLED_ERRNOS:
                raise self._timeout_error(deadline, exc) from exc
            raise
        finally:
            self._close_cursor(cursor)
        return names, rows

    def _probe_contract_regex(self, pattern: str) -> None:
        """Have the warehouse compile the pattern, reading no table."""
        params = Params()
        self._run(f"SELECT REGEXP_INSTR('', {params.bind(pattern)})", params)

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
            self._close_cursor(cursor)
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
        return self._remember_columns(base_query, columns)

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
            f"ORDER BY table_schema, table_name, ordinal_position LIMIT {SCHEMA_ROW_LIMIT}"
        )
        _, rows = self._run(sql, params, timeout_cap=SCHEMA_QUERY_TIMEOUT_SECONDS)
        columns_by_table: dict[str, list[SchemaColumn]] = {}
        for table_schema, table_name, column_name, data_type in rows:
            bare = str(table_name)
            qualified = bare if str(table_schema) == default_key else f"{table_schema}.{bare}"
            columns_by_table.setdefault(qualified, []).append(
                SchemaColumn(name=str(column_name), data_type=str(data_type))
            )
        return [SchemaTable(name=name, columns=cols) for name, cols in columns_by_table.items()]
