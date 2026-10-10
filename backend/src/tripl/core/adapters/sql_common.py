"""Building blocks every SQL adapter shares, whatever its dialect.

The identifier grammar, the log cap, the catalog-browse budget and the result
decoders used to be copied into each adapter (and the Trino and Snowflake
adapters imported theirs from a Databricks module). They live here once, so a
change to any of them is one change. Nothing here imports a driver or knows a
dialect's spelling; that stays in each engine's own ``*_sql`` module.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

from tripl.core.bucketing import to_utc

#: The adapters' identifier grammar: a column, optionally dot-qualified.
IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
#: One path segment (or one plain column name).
IDENTIFIER_PART_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: Hard cap on catalog rows pulled for SQL-editor autocomplete, so a warehouse
#: with thousands of wide tables cannot blow up the response. Introspection
#: spans every schema in scope in one statement, so the cap is generous.
SCHEMA_ROW_LIMIT = 50000

#: Catalog introspection is a CAP, not a default: a source configuring a shorter
#: ``timeout_seconds`` still wins (see ``BaseAdapter._query_deadline``).
SCHEMA_QUERY_TIMEOUT_SECONDS = 30

# Cap on how much user-supplied SQL (which may embed warehouse credentials or
# PII column names) ever reaches a log line. Query logs go to DEBUG, so the full
# statement never lands at INFO.
_SQL_LOG_MAX_CHARS = 300


def truncate_sql(sql: str) -> str:
    return sql[:_SQL_LOG_MAX_CHARS] + ("..." if len(sql) > _SQL_LOG_MAX_CHARS else "")


def validate_identifier_column(column: str, allowed_columns: set[str]) -> str:
    """The column, if it is a plain identifier the introspected result holds."""
    if not IDENTIFIER_RE.match(column):
        msg = f"Invalid column name: {column}"
        raise ValueError(msg)
    if allowed_columns and column not in allowed_columns:
        msg = f"Column {column!r} not found in query result"
        raise ValueError(msg)
    return column


def json_path_parts(path: str) -> list[str]:
    """A dotted JSON path as its segments, each held to the identifier grammar.

    Holding every segment to the grammar before a dialect spells it is what
    keeps a quote or a bracket out of the path text.
    """
    parts = [part for part in path.split(".") if part]
    if not parts or any(not IDENTIFIER_PART_RE.match(part) for part in parts):
        raise ValueError(f"Unsupported JSON path: {path}")
    return parts


def is_array_type(type_name: str) -> bool:
    """An ARRAY column, however the dialect cases it (``array(...)``, ``ARRAY<...>``)."""
    return type_name.strip().lower().startswith("array")


# --------------------------------------------------------------------------- #
# result cells
# --------------------------------------------------------------------------- #


def as_utc_bucket(value: object) -> object:
    """One ``_bucket`` cell as an aware UTC ``datetime``.

    Every bucket expression returns a zone-less timestamp holding the UTC wall
    clock (or a timestamp computed in a session pinned to UTC), which the
    drivers hand back naive, and pyathena may hand back as text; it is stamped
    as UTC. ``datetime`` is tested before ``date`` because it is a subclass of
    it.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value


def count_cell(value: object, engine: str) -> int:
    """A COUNT cell as an int; an aggregate over no rows is 0, never NULL."""
    if value is None:
        return 0
    if isinstance(value, int | float | str | Decimal):
        return int(value)
    msg = f"{engine}: expected a count, got {type(value).__name__}"
    raise ValueError(msg)


def decode_json_list(value: object, engine: str) -> object:
    """A grouped key list (a JSON array rendered as text) back as the list it stands for."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if not isinstance(value, str):
        msg = f"{engine}: expected a JSON array string, got {type(value).__name__}"
        raise ValueError(msg)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        msg = f"{engine}: could not decode a grouped key list {value!r}: {exc}"
        raise ValueError(msg) from exc
    if decoded is not None and not isinstance(decoded, list):
        msg = f"{engine}: a grouped key list decoded to {type(decoded).__name__}, not a list"
        raise ValueError(msg)
    return decoded


def decode_array_value(value: object) -> object:
    """An ARRAY regular column as a list; drivers may hand ARRAY values back as JSON text."""
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, list) else value
    return value
