"""Small Databricks SQL building blocks shared by the adapter and its tests.

Kept apart from ``databricks.py`` so that nothing here imports the driver:
``databricks-sql-connector`` is imported lazily (:func:`import_driver`), the way
the registry imports every adapter, so a process that never builds a Databricks
adapter never loads it.
"""

from __future__ import annotations

import logging
from types import ModuleType


def quote_ident(name: str) -> str:
    """A back-quoted Databricks identifier; a back-quote inside is doubled."""
    return "`" + name.replace("`", "``") + "`"


def json_text(expr: str) -> str:
    """Any value as JSON text: ``"abc"``, ``42``, ``{"a":1}``, ``null``.

    ``to_json`` only takes a STRUCT, ARRAY, MAP or VARIANT, so a scalar is
    wrapped in a one-element array and the brackets are cut off again. The
    result is what ClickHouse's ``toJSONString`` and BigQuery's
    ``TO_JSON_STRING`` hand back for the same value.
    """
    return f"regexp_extract(to_json(array({expr})), '^\\\\[(.*)\\\\]$', 1)"


class Params:
    """The named parameters of one statement.

    Every value that came from data or from an analyst is bound here and
    referenced as ``:pN``; the driver sends them separately (native parameters),
    so no such value is ever part of the SQL text.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def bind(self, value: str) -> str:
        name = f"p{len(self.values)}"
        self.values[name] = str(value)
        return f":{name}"


class _DropPyarrowNotice(logging.Filter):
    """The driver warns on import that pyarrow is absent; tripl does not use it."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "pyarrow is not installed" not in record.getMessage()


def import_driver() -> ModuleType:
    """``databricks.sql``, imported on first use, without its pyarrow notice.

    pyarrow is deliberately not installed: it only serves Arrow fetches and
    cloud fetch, and cloud fetch is switched off (see ``DatabricksAdapter``).
    """
    logging.getLogger("databricks.sql.client").addFilter(_DropPyarrowNotice())
    from databricks import sql

    return sql
