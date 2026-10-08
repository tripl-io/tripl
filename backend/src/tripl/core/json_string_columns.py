"""Opt-in: read String columns as JSON (F23.9, #306).

Many event tables keep their properties as JSON *text* in a plain String
(ClickHouse), STRING (BigQuery, Databricks, Snowflake) or varchar (Trino,
Athena) column. A scan config lists such columns in ``json_string_columns``,
and every warehouse read of the scan's source goes through
:func:`scan_source_query`, which hands the adapter's
``json_string_source`` wrapper back instead of the bare ``base_query``. In that
wrapper the listed columns ARE JSON columns: introspection reports them as JSON,
so the cardinality split, path discovery and sampling (whole objects included),
event generation, object grouping, the "event + properties" preset, property
breakdowns, drift fields and contracts, replay and metric collection all take the
path they already take for a native JSON column. There is no second code path to
drift from the first.

A row whose text is not a JSON object reads as a document with no keys; the
adapters use the engine's non-failing parse for that (see their
``json_string_source``).

PostgreSQL is not supported: it has no non-failing cast from text to ``jsonb``
before version 16 (``pg_input_is_valid``), and an opt-in that fails a whole scan
on one malformed row is worse than none. The API refuses the setting there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import Protocol

from tripl.core.adapters.base import BaseAdapter
from tripl.core.adapters.errors import WarehouseCapabilityError
from tripl.core.warehouse_types import is_string_type

logger = logging.getLogger(__name__)

#: The data source types whose adapters implement ``json_string_source``.
JSON_STRING_DB_TYPES = frozenset(
    {"clickhouse", "bigquery", "databricks", "snowflake", "trino", "athena"}
)

#: How many columns one scan may parse. Each is one parse per row read, and a
#: scan rarely has more than one properties column; the cap keeps a config from
#: multiplying the cost of every statement.
MAX_JSON_STRING_COLUMNS = 5

#: A plain column name. No dots: ``<column>.<path>`` is the property syntax, so a
#: dotted column name could not be told apart from a property of another column.
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Where :func:`resolve_json_string_source` caches its answer on an adapter.
_CACHE_ATTR = "_json_string_sources"


class _ScanSource(Protocol):
    @property
    def base_query(self) -> str: ...


def normalize_json_string_columns(value: Iterable[str]) -> list[str]:
    """Trim, drop blanks and dedupe; raise ``ValueError`` on a bad name or too many."""
    normalized: list[str] = []
    for item in value:
        column = item.strip()
        if not column or column in normalized:
            continue
        if len(column) > 255 or not _COLUMN_RE.match(column):
            msg = (
                f"json_string_columns: {column!r} is not a plain column name "
                "(letters, digits and underscores, not starting with a digit)"
            )
            raise ValueError(msg)
        normalized.append(column)
    if len(normalized) > MAX_JSON_STRING_COLUMNS:
        msg = f"json_string_columns can list at most {MAX_JSON_STRING_COLUMNS} columns"
        raise ValueError(msg)
    return normalized


def check_json_string_columns_roles(
    json_string_columns: Sequence[str],
    *,
    event_type_column: str | None = None,
    time_column: str | None = None,
    app_version_column: str | None = None,
    platform_column: str | None = None,
    event_name_column: str | None = None,
    metric_breakdown_columns: Sequence[str] = (),
    distribution_drift_fields: Sequence[str] = (),
) -> None:
    """Refuse a parsed column that another setting reads as plain text.

    A parsed column is a JSON document for the whole scan, so it cannot also
    name events, hold the time, version or platform, or be a scalar breakdown
    or drift field. Its properties (``<column>.<path>``) can be all of those.
    """
    parsed = set(json_string_columns)
    if not parsed:
        return
    for role, column in (
        ("event_type_column", event_type_column),
        ("time_column", time_column),
        ("app_version_column", app_version_column),
        ("platform_column", platform_column),
        ("event_name_column", event_name_column),
    ):
        if column and column in parsed:
            msg = f"{column!r} is parsed as JSON (json_string_columns), so it cannot be the {role}"
            raise ValueError(msg)
    for field_name, entries in (
        ("metric_breakdown_columns", metric_breakdown_columns),
        ("distribution_drift_fields", distribution_drift_fields),
    ):
        clashing = sorted(parsed & set(entries))
        if clashing:
            msg = (
                f"{field_name} cannot list {clashing[0]!r}: it is parsed as JSON "
                "(json_string_columns). Pick one of its properties, "
                f"{clashing[0]}.<key>, instead"
            )
            raise ValueError(msg)


def check_json_string_db_type(db_type: str, json_string_columns: Sequence[str]) -> None:
    """Refuse the opt-in on a data source whose adapter cannot parse text."""
    if json_string_columns and str(db_type) not in JSON_STRING_DB_TYPES:
        msg = (
            "Parsing String columns as JSON (json_string_columns) is supported on "
            "ClickHouse, BigQuery, Databricks, Snowflake, Trino and Athena data sources only"
        )
        raise ValueError(msg)


def resolve_json_string_source(
    adapter: BaseAdapter, base_query: str, columns: Sequence[str]
) -> str:
    """The query every warehouse read of this source should run on.

    ``base_query`` itself when nothing is opted in, so a config without the
    setting issues exactly the statements it always did. Otherwise the adapter's
    ``json_string_source`` over the opted-in columns that ARE text: the raw
    source is introspected first, and a column that is missing or already JSON
    (or any other type) is left alone and logged, since the parse would be
    either an error or a no-op.

    Cached on the adapter per ``(base_query, columns)``, so the many call sites
    of one run share a single answer and a single extra introspection. The
    adapter is left introspected on the wrapped query, as the callers expect.
    """
    wanted = tuple(dict.fromkeys(column for column in columns if column))
    if not wanted:
        return base_query
    cache: dict[tuple[str, tuple[str, ...]], str] | None = getattr(adapter, _CACHE_ATTR, None)
    key = (base_query, wanted)
    if cache is not None and key in cache:
        return cache[key]
    if not getattr(adapter, "supports_json_string_columns", False):
        msg = (
            "This data source cannot parse String columns as JSON; only ClickHouse, "
            "BigQuery, Databricks, Snowflake, Trino and Athena can. Clear the scan's "
            "'Parse as JSON' columns."
        )
        raise WarehouseCapabilityError(msg)
    raw_types = {column.name: column.type_name for column in adapter.get_columns(base_query)}
    parsed: list[str] = []
    for column in wanted:
        type_name = raw_types.get(column)
        if type_name is not None and is_string_type(type_name):
            parsed.append(column)
        else:
            logger.warning(
                "json_string_columns: %r is %s, not a String column; not parsed as JSON",
                column,
                type_name or "not in the query",
            )
    source = base_query
    if parsed:
        source = adapter.json_string_source(base_query, parsed)
        adapter.get_columns(source)
    if cache is None:
        cache = {}
        setattr(adapter, _CACHE_ATTR, cache)
    cache[key] = source
    return source


def scan_source_query(adapter: BaseAdapter, config: _ScanSource) -> str:
    """:func:`resolve_json_string_source` for a scan config (or a preview job).

    A config built in memory and never flushed has ``json_string_columns`` unset
    (``None``); a stand-in without the attribute at all parses nothing either.
    """
    columns = getattr(config, "json_string_columns", None) or []
    return resolve_json_string_source(adapter, config.base_query, list(columns))
