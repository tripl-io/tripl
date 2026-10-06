"""Credential wiring and the table-less fixture for real-Databricks value conformance.

Nothing here creates a table: the canonical nine-row fixture is rendered as a
``UNION ALL`` of typed ``SELECT`` rows, so the identity needs ``CAN USE`` on a
SQL warehouse and nothing in Unity Catalog. Credentials come from the
environment only and are never written anywhere:

* ``TRIPL_CONF_DBX_HOST`` — the workspace hostname;
* ``TRIPL_CONF_DBX_HTTP_PATH`` — the SQL warehouse's HTTP path;
* ``TRIPL_CONF_DBX_TOKEN`` — a personal access token, or, with
  ``TRIPL_CONF_DBX_CLIENT_ID`` set, a service principal's OAuth secret;
* ``TRIPL_CONF_DBX_CATALOG`` — optional; the session's catalog (``workspace``
  when unset), which the fixture never reads from.

Without them every test skips; with ``TRIPL_DBX_VALUE_REQUIRED=1`` a missing or
unreachable warehouse is a failure instead.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import NoReturn

import pytest

from tripl.core.adapters.databricks import DatabricksAdapter
from tripl.tests.conformance.dataset import ROWS, FixtureRow

REQUIRED_ENV = "TRIPL_DBX_VALUE_REQUIRED"


def unavailable(reason: str) -> NoReturn:
    message = f"real Databricks conformance unavailable: {reason}"
    if os.environ.get(REQUIRED_ENV) == "1":
        pytest.fail(message)
    pytest.skip(message)


def new_adapter() -> DatabricksAdapter:
    """One real adapter with a short statement budget."""
    host = os.environ.get("TRIPL_CONF_DBX_HOST", "").strip()
    http_path = os.environ.get("TRIPL_CONF_DBX_HTTP_PATH", "").strip()
    token = os.environ.get("TRIPL_CONF_DBX_TOKEN", "").strip()
    client_id = os.environ.get("TRIPL_CONF_DBX_CLIENT_ID", "").strip()
    catalog = os.environ.get("TRIPL_CONF_DBX_CATALOG", "").strip() or "workspace"
    missing = [
        name
        for name, value in (
            ("TRIPL_CONF_DBX_HOST", host),
            ("TRIPL_CONF_DBX_HTTP_PATH", http_path),
            ("TRIPL_CONF_DBX_TOKEN", token),
        )
        if not value
    ]
    if missing:
        unavailable(f"missing {', '.join(missing)}")
    return DatabricksAdapter(
        host=host,
        port=443,
        database=catalog,
        username=client_id,
        password=token,
        http_path=http_path,
        auth_type="oauth_m2m" if client_id else "pat",
        # A stopped serverless warehouse needs a while for its first statement.
        timeout_seconds=120,
    )


def _string_literal(value: str) -> str:
    """A Databricks string literal: backslash escapes, as the adapter's quoting uses."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"


def _props_literal(row: FixtureRow) -> str:
    """The ``doc``'s city, id and tags again as a STRUCT, for declared-path access."""
    user = row.doc.get("user")
    address = user.get("address") if isinstance(user, dict) else None
    city = address.get("city") if isinstance(address, dict) else None
    city_literal = _string_literal(city) if isinstance(city, str) else "CAST(NULL AS STRING)"
    tags = row.doc.get("tags")
    tag_values = [str(value) for value in tags] if isinstance(tags, list) else []
    tags_literal = (
        f"array({', '.join(_string_literal(tag) for tag in tag_values)})"
        if tag_values
        else "CAST(array() AS ARRAY<STRING>)"
    )
    return (
        f"named_struct('address', named_struct('city', {city_literal}), "
        f"'id', {row.id}, 'tags', {tags_literal})"
    )


def _row_select(row: FixtureRow) -> str:
    instant = row.ts.strftime("%Y-%m-%d %H:%M:%S.%f")
    amount = "CAST(NULL AS DOUBLE)" if row.amount is None else f"CAST({row.amount!r} AS DOUBLE)"
    document = json.dumps(row.doc, ensure_ascii=True, separators=(",", ":"))
    return (
        f"SELECT {row.id} AS id, "
        f"TIMESTAMP '{instant}+00:00' AS ts, "
        f"TIMESTAMP_NTZ '{instant}' AS ntz, "
        f"DATE '{row.ts.date().isoformat()}' AS d, "
        f"{_string_literal(row.event_name)} AS event_name, "
        f"{amount} AS amount, "
        f"{_string_literal(row.user_id)} AS user_id, "
        f"parse_json({_string_literal(document)}) AS doc, "
        f"{_string_literal(document)} AS doc_text, "
        f"{_props_literal(row)} AS props"
    )


def render_databricks_rows(rows: Iterable[FixtureRow] = ROWS) -> str:
    """The canonical fixture as one typed, table-less Databricks relation."""
    rendered = " UNION ALL ".join(_row_select(row) for row in rows)
    if not rendered:
        raise ValueError("Databricks conformance rows must not be empty")
    return rendered


BASE = render_databricks_rows()
