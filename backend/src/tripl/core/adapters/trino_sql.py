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

from datetime import datetime
from types import ModuleType

from tripl.core.bucketing import to_utc

_TS_LITERAL_FMT = "%Y-%m-%d %H:%M:%S.%f"
_DATE_LITERAL_FMT = "%Y-%m-%d"


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


class LiteralParams:
    """A Trino statement's value binder: each value becomes a quoted literal.

    The shared statement builders hand every value to a binder; on Trino the
    binding is the rendering itself (see the module docstring), so nothing is
    left to send beside the statement.
    """

    def bind(self, value: str) -> str:
        return quote_literal(value)


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


def is_text_type(type_name: str) -> bool:
    name = _norm(type_name)
    return name.startswith(("varchar", "char"))


def is_float_type(type_name: str) -> bool:
    return _norm(type_name) in ("double", "real")


def is_container_type(type_name: str) -> bool:
    """``array(...)``, ``map(...)`` and ``row(...)``: values with no ``varchar`` cast."""
    return _norm(type_name).startswith(("array", "map", "row"))


def is_json_type(type_name: str) -> bool:
    return _norm(type_name) == "json"
