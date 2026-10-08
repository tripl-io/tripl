"""Side-effect-free compilation of fact metric primary batch queries.

The collection worker executes the concrete adapter's
``get_time_bucketed_multi_aggregate`` method. Those methods now delegate their
statement construction to ``build_time_bucketed_multi_aggregate_sql``; this
module primes a connection-free adapter instance from the FactTable's persisted
column metadata and invokes that exact builder for API disclosure.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tripl.core.adapters.base import AggregateSpec, BaseAdapter


def compile_time_bucketed_multi_aggregate_sql(
    *,
    db_type: str,
    base_query: str,
    time_column: str,
    interval: str,
    specs: list[AggregateSpec],
    time_from: datetime,
    time_to: datetime,
    column_types: Mapping[str, str],
    limit: int = 100000,
) -> tuple[list[str], str]:
    """Compile the exact primary batch statement without opening a connection."""
    adapter: BaseAdapter
    if db_type == "clickhouse":
        from tripl.core.adapters.clickhouse import ClickHouseAdapter

        clickhouse = object.__new__(ClickHouseAdapter)
        # ClickHouse's nested-shape SQL is type-directed (JSON / Map / Tuple each
        # need a different shape function), so the primed instance carries the same
        # type map a real ``get_columns`` would have left behind. The multi-aggregate
        # builder itself takes no nested columns today; priming both fields keeps
        # this stand-in a faithful replica of a connected adapter rather than one
        # that happens to be right only for the statement we ask it for.
        clickhouse._column_types = dict(column_types)
        clickhouse._allowed_columns = set(column_types)
        adapter = clickhouse
    elif db_type in ("postgres", "greenplum", "redshift"):
        from tripl.core.adapters.greenplum import GreenplumAdapter
        from tripl.core.adapters.postgres import PostgresAdapter
        from tripl.core.adapters.redshift import RedshiftAdapter

        libpq_class: type[PostgresAdapter] = {
            "postgres": PostgresAdapter,
            "greenplum": GreenplumAdapter,
            "redshift": RedshiftAdapter,
        }[db_type]
        postgres = object.__new__(libpq_class)
        postgres._allowed_columns = set(column_types)
        adapter = postgres
    elif db_type == "bigquery":
        from tripl.core.adapters.bigquery import BigQueryAdapter

        bigquery = object.__new__(BigQueryAdapter)
        # BigQuery's bucket and bound literal families are driven by the
        # timestamp column's declared type (TIMESTAMP / DATETIME / DATE).
        bigquery._column_types = dict(column_types)
        bigquery._allowed_columns = set(column_types)
        adapter = bigquery
    elif db_type == "databricks":
        from tripl.core.adapters.databricks import DatabricksAdapter

        databricks = object.__new__(DatabricksAdapter)
        # Databricks' window literal follows the time column's declared type
        # (TIMESTAMP / TIMESTAMP_NTZ / DATE), like BigQuery's.
        databricks._column_types = dict(column_types)
        databricks._allowed_columns = set(column_types)
        adapter = databricks
    elif db_type == "snowflake":
        from tripl.core.adapters.snowflake import SnowflakeAdapter

        snowflake = object.__new__(SnowflakeAdapter)
        # Snowflake's window literal and bucket follow the time column's declared
        # type too (TIMESTAMP_NTZ / TIMESTAMP_LTZ / TIMESTAMP_TZ / DATE).
        snowflake._column_types = dict(column_types)
        snowflake._allowed_columns = set(column_types)
        adapter = snowflake
    elif db_type in ("trino", "athena"):
        from tripl.core.adapters.athena import AthenaAdapter
        from tripl.core.adapters.trino import TrinoAdapter

        trino = object.__new__(AthenaAdapter if db_type == "athena" else TrinoAdapter)
        # Trino's window literal and bucket follow the time column's declared type
        # (timestamp / timestamp with time zone / date), and Athena is Trino SQL.
        trino._column_types = dict(column_types)
        trino._allowed_columns = set(column_types)
        adapter = trino
    else:
        msg = f"Generated batch SQL is unavailable for data source type {db_type!r}"
        raise ValueError(msg)

    # Measure and timestamp identifiers go through the exact adapter allowlist
    # guard used during collection, but the endpoint never introspects or queries
    # the warehouse. FactTable columns are refreshed by its normal Check flow.
    return adapter.build_time_bucketed_multi_aggregate_sql(
        base_query,
        time_column,
        interval,
        specs,
        time_from,
        time_to,
        limit=limit,
    )
