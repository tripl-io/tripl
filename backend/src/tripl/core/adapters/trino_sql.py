"""Small Trino SQL building blocks shared by the Trino and Athena adapters and their tests.

Kept apart from ``trino.py`` so that nothing here imports a driver: the
``trino`` client and ``pyathena`` are imported lazily (:func:`import_trino`,
``athena_sql.import_driver``), the way the registry imports every adapter, so a
process that never builds one never loads them.

**Values are rendered as quoted literals, never bound.** That is a decision,
not an omission. Trino's client does take ``?`` parameters, but it binds them by
rendering each value as a quoted literal into an ``EXECUTE ... USING`` clause
(``trino.dbapi.Cursor._format_prepared_param``: ``"'%s'" % value.replace("'",
"''")``), which is exactly :func:`quote_literal` below with an extra round trip
and positional markers that must be consumed in the order they appear, while
the generated SQL reuses one expression in several places. Athena's
``ExecutionParameters`` likewise take each value as SQL literal TEXT, quotes
included. So both engines end at the same literal, and rendering it here keeps
one spelling for both. The rendering is complete for Trino SQL: a string
literal has no escape sequences at all, a backslash is data, and a single quote
is written twice — there is nothing else a value could use to leave the literal.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from types import ModuleType

from tripl.core.bucketing import to_utc

#: A catalog or schema name as the connection form accepts it.
OBJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,254}$")

# Cap on how much generated SQL reaches a log line; statements go to DEBUG.
_SQL_LOG_MAX_CHARS = 300

_TS_LITERAL_FMT = "%Y-%m-%d %H:%M:%S.%f"
_DATE_LITERAL_FMT = "%Y-%m-%d"


def truncate_sql(sql: str) -> str:
    return sql[:_SQL_LOG_MAX_CHARS] + ("..." if len(sql) > _SQL_LOG_MAX_CHARS else "")


def quote_ident(name: str) -> str:
    """A double-quoted Trino identifier; a double quote inside is doubled."""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: object) -> str:
    """A Trino ``varchar`` literal holding ``str(value)``.

    Trino string literals have no escape sequences: the quote is doubled and
    nothing else is special, so this is the whole of the escaping (see the
    module docstring for why values are not bound instead).
    """
    return "'" + str(value).replace("'", "''") + "'"


def utc_timestamp_tz_literal(value: datetime) -> str:
    """A UTC instant as ``timestamp(6) with time zone`` at UTC."""
    return f"TIMESTAMP '{to_utc(value).strftime(_TS_LITERAL_FMT)} UTC'"


def utc_timestamp_literal(value: datetime) -> str:
    """A UTC instant as a zone-less ``timestamp(6)`` holding its UTC wall clock."""
    return f"TIMESTAMP '{to_utc(value).strftime(_TS_LITERAL_FMT)}'"


def utc_date_literal(value: datetime) -> str:
    return f"DATE '{to_utc(value).strftime(_DATE_LITERAL_FMT)}'"


def import_trino() -> ModuleType:
    """``trino.dbapi``, imported on first use."""
    import trino.dbapi

    return trino.dbapi


# --------------------------------------------------------------------------- #
# column types
# --------------------------------------------------------------------------- #


def _norm(type_name: str) -> str:
    return type_name.strip().lower()


def is_zoned_type(type_name: str) -> bool:
    """``timestamp(p) with time zone``: an instant carrying its zone."""
    name = _norm(type_name)
    return name.startswith("timestamp") and name.endswith("with time zone")


def is_date_type(type_name: str) -> bool:
    return _norm(type_name) == "date"


def is_text_type(type_name: str) -> bool:
    name = _norm(type_name)
    return name.startswith(("varchar", "char"))


def is_float_type(type_name: str) -> bool:
    return _norm(type_name) in ("double", "real")


def is_container_type(type_name: str) -> bool:
    """``array(...)``, ``map(...)`` and ``row(...)``: values with no ``varchar`` cast."""
    return _norm(type_name).startswith(("array", "map", "row"))


def is_array_type(type_name: str) -> bool:
    return _norm(type_name).startswith("array")


def is_json_type(type_name: str) -> bool:
    return _norm(type_name) == "json"


# --------------------------------------------------------------------------- #
# result cells
# --------------------------------------------------------------------------- #


def as_utc_bucket(value: object) -> object:
    """One ``_bucket`` cell as an aware UTC ``datetime``.

    Every bucket expression returns a zone-less ``timestamp(3)`` holding the UTC
    wall clock, which the drivers hand back naive (pyathena may hand it back as
    text); it is stamped as UTC. ``datetime`` is tested before ``date`` because
    it is a subclass of it.
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


def count_cell(value: object) -> int:
    """A COUNT cell as an int; an aggregate over no rows is 0, never NULL."""
    if value is None:
        return 0
    if isinstance(value, int | float | str | Decimal):
        return int(value)
    msg = f"Trino: expected a count, got {type(value).__name__}"
    raise ValueError(msg)


def decode_json_list(value: object) -> object:
    """A grouped key list (``json_format`` of a sorted array) back as a list."""
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if not isinstance(value, str):
        msg = f"Trino: expected a JSON array string, got {type(value).__name__}"
        raise ValueError(msg)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        msg = f"Trino: could not decode a grouped key list {value!r}: {exc}"
        raise ValueError(msg) from exc
    if decoded is not None and not isinstance(decoded, list):
        msg = f"Trino: a grouped key list decoded to {type(decoded).__name__}, not a list"
        raise ValueError(msg)
    return decoded


def decode_array_value(value: object) -> object:
    """An ARRAY regular column as a list (pyathena may return it as text)."""
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded if isinstance(decoded, list) else value
    return value
