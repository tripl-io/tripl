"""The warehouse tables: their columns, as both engines report them, and their DDL.

Two tables in the ``warehouse`` schema of the demo database, both partitioned by
UTC day on their time column:

* ``events``: one row per tracked event, with a ``properties`` JSON document;
* ``orders``: one row per charge, derived from the purchase events that carry an
  amount (a completed purchase, a converted trial, a renewal) and from failed
  payments.

The type names are what psycopg reports for the PostgreSQL columns
(``PostgresAdapter._type_name``), so the in-memory engine describes the tables
exactly as the real one does and a scan of either sees the same schema.

The writer's bookkeeping lives in a separate ``warehouse_meta`` schema the
reader role cannot see, so the SQL editor's table list and a user's
``SELECT *`` show only the two tables of the story.
"""

from __future__ import annotations

from datetime import datetime, timedelta

WAREHOUSE_SCHEMA = "warehouse"
META_SCHEMA = "warehouse_meta"
STATE_TABLE = f"{META_SCHEMA}.state"

#: ``(name, PostgreSQL type as psycopg names it, DDL type)``.
EVENTS_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("event_time", "timestamptz", "timestamptz NOT NULL"),
    ("event_type", "text", "text NOT NULL"),
    ("event_name", "text", "text NOT NULL"),
    ("screen_name", "text", "text"),
    ("platform", "text", "text NOT NULL"),
    ("app_version", "text", "text NOT NULL"),
    ("country", "text", "text NOT NULL"),
    ("button_id", "text", "text"),
    ("product_id", "text", "text"),
    ("amount", "numeric", "numeric(12,2)"),
    ("currency", "text", "text"),
    ("user_id", "text", "text NOT NULL"),
    ("session_id", "text", "text NOT NULL"),
    ("properties", "jsonb", "jsonb NOT NULL DEFAULT '{}'::jsonb"),
)
ORDERS_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("created_at", "timestamptz", "timestamptz NOT NULL"),
    ("order_id", "text", "text NOT NULL"),
    ("amount", "numeric", "numeric(12,2) NOT NULL"),
    ("currency", "text", "text NOT NULL"),
    ("user_id", "text", "text NOT NULL"),
    ("country", "text", "text NOT NULL"),
    ("status", "text", "text NOT NULL"),
    ("payment_method", "text", "text NOT NULL"),
)
TABLE_COLUMNS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "events": EVENTS_COLUMNS,
    "orders": ORDERS_COLUMNS,
}
TIME_COLUMNS: dict[str, str] = {"events": "event_time", "orders": "created_at"}
NULLABLE_COLUMNS: dict[str, frozenset[str]] = {
    "events": frozenset({"screen_name", "button_id", "product_id", "amount", "currency"}),
    "orders": frozenset(),
}
JSON_COLUMNS: dict[str, frozenset[str]] = {
    "events": frozenset({"properties"}),
    "orders": frozenset(),
}

#: The columns a scan should group by: every low-cardinality column. A scan
#: must never group by ``user_id``, ``session_id`` or ``order_id``.
SCAN_COLUMNS: tuple[str, ...] = (
    "event_time",
    "event_type",
    "event_name",
    "screen_name",
    "platform",
    "app_version",
    "country",
    "button_id",
    "product_id",
    "amount",
    "currency",
    "properties",
)
#: The base query of the demo's catalog scan (``search_path`` is the warehouse
#: schema on both engines).
EVENTS_SCAN_SQL = f"SELECT {', '.join(SCAN_COLUMNS)} FROM events"
ORDERS_SQL = (
    "SELECT created_at, order_id, amount, currency, user_id, country, status, "
    "payment_method FROM orders"
)
#: The demo's distinct-sessions-per-day sql metric, in PostgreSQL. The in-memory
#: engine recognizes exactly this statement (whitespace and case aside).
ACTIVE_SESSIONS_SQL = (
    "SELECT date_trunc('day', event_time) AS ts, count(DISTINCT session_id) AS value "
    "FROM events GROUP BY 1"
)
#: JSON paths worth extracting as values in the scan (low cardinality).
SUGGESTED_JSON_VALUE_PATHS: tuple[str, ...] = ("source", "payment_method", "error_code")


def partition_name(table: str, day: datetime) -> str:
    return f"{table}_p{day:%Y%m%d}"


def schema_ddl() -> list[str]:
    """Statements that create the schemas and the partitioned parents (idempotent)."""
    statements = [
        f"CREATE SCHEMA IF NOT EXISTS {WAREHOUSE_SCHEMA}",
        f"CREATE SCHEMA IF NOT EXISTS {META_SCHEMA}",
    ]
    for table, columns in TABLE_COLUMNS.items():
        body = ", ".join(f"{name} {ddl}" for name, _, ddl in columns)
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {WAREHOUSE_SCHEMA}.{table} ({body}) "
            f"PARTITION BY RANGE ({TIME_COLUMNS[table]})"
        )
        # BRIN: rows arrive in time order, a day partition at a time, so a
        # block-range index is a few pages and prunes a window scan well.
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {table}_{TIME_COLUMNS[table]}_brin "
            f"ON {WAREHOUSE_SCHEMA}.{table} USING brin ({TIME_COLUMNS[table]})"
        )
    statements.append(
        f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
        "id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1), "
        "model_version text NOT NULL, "
        "written_through timestamptz, "
        "oldest timestamptz, "
        "last_run_at timestamptz, "
        "last_run_rows bigint NOT NULL DEFAULT 0, "
        "last_error text)"
    )
    return statements


def partition_ddl(table: str, day: datetime) -> str:
    """The day partition of ``table`` starting at ``day`` (a UTC midnight)."""
    end = day + timedelta(days=1)
    return (
        f"CREATE TABLE IF NOT EXISTS {WAREHOUSE_SCHEMA}.{partition_name(table, day)} "
        f"PARTITION OF {WAREHOUSE_SCHEMA}.{table} "
        f"FOR VALUES FROM ('{day.isoformat()}') TO ('{end.isoformat()}')"
    )
