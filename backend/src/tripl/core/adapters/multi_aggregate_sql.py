"""Side-effect-free compilation of fact metric primary batch queries.

The collection worker executes the concrete adapter's
``get_time_bucketed_multi_aggregate`` method, which builds its statement with
``build_time_bucketed_multi_aggregate_sql``. This module primes a
connection-free instance of the same adapter class from the FactTable's
persisted column metadata and calls that exact builder for API disclosure.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from tripl.core.adapters.base import AggregateSpec, BaseAdapter
from tripl.core.adapters.registry import adapter_class


def _sql_adapter_class(db_type: str) -> type[BaseAdapter]:
    """The adapter class a ``db_type`` source is built with, if it builds warehouse SQL.

    A ``ValueError`` (shown as a 422) for a type with no adapter, and for one
    whose adapter keeps ``BaseAdapter``'s builder: the synthetic warehouse
    computes its rows in memory and has no statement to disclose.
    """
    cls: type[BaseAdapter] | None
    try:
        cls = adapter_class(db_type)
    except ValueError:
        cls = None
    builder = BaseAdapter.build_time_bucketed_multi_aggregate_sql
    if cls is None or cls.build_time_bucketed_multi_aggregate_sql is builder:
        msg = f"Generated batch SQL is unavailable for data source type {db_type!r}"
        raise ValueError(msg)
    return cls


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
    """Compile the exact primary batch statement without opening a connection.

    The primed adapter knows the stored columns and their types the way
    ``get_columns`` leaves them: identifiers go through the same allowlist
    guard as during collection, and an engine whose bucket or window literal
    follows the time column's declared type reads it from there. The warehouse
    is never introspected or queried; FactTable columns are refreshed by its
    Check flow.
    """
    adapter = _sql_adapter_class(db_type).primed(column_types)
    return adapter.build_time_bucketed_multi_aggregate_sql(
        base_query,
        time_column,
        interval,
        specs,
        time_from,
        time_to,
        limit=limit,
    )
