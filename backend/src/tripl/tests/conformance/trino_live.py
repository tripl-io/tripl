"""Connection wiring and the table-less fixture for Trino and Athena value conformance.

Nothing here creates a table: the canonical nine-row fixture is rendered as a
``UNION ALL`` of typed ``SELECT`` rows, so the gate needs a coordinator (or an
Athena workgroup) and nothing else — no catalog table, no write permission.
Athena engine version 3 is Trino SQL, so both read the same relation.

Trino (credential-free, the ``trino`` CI job runs ``trinodb/trino`` in Docker):

* ``TRIPL_CONF_TRINO_HOST`` / ``TRIPL_CONF_TRINO_PORT`` — default
  ``localhost:8080``;
* ``TRIPL_CONF_TRINO_CATALOG`` — the session catalog, default ``memory`` (never
  read from);
* ``TRIPL_CONF_TRINO_USER`` — default ``tripl``.

Athena (credentialed, ``athena-value-conformance.yml`` on release tags):

* ``TRIPL_CONF_ATHENA_REGION``, ``TRIPL_CONF_ATHENA_ACCESS_KEY_ID``,
  ``TRIPL_CONF_ATHENA_SECRET_ACCESS_KEY``;
* ``TRIPL_CONF_ATHENA_WORKGROUP`` (default ``primary``) and
  ``TRIPL_CONF_ATHENA_OUTPUT`` (an ``s3://`` location, unless the workgroup
  sets one).

An unreachable coordinator or missing credentials skip; with
``TRIPL_TRINO_VALUE_REQUIRED=1`` / ``TRIPL_ATHENA_VALUE_REQUIRED=1`` (CI sets
them) they are failures instead.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import NoReturn

import pytest

from tripl.core.adapters.athena import AthenaAdapter
from tripl.core.adapters.trino import TrinoAdapter
from tripl.tests.conformance.dataset import ROWS, FixtureRow

TRINO_REQUIRED_ENV = "TRIPL_TRINO_VALUE_REQUIRED"
ATHENA_REQUIRED_ENV = "TRIPL_ATHENA_VALUE_REQUIRED"


def unavailable(engine: str, required_env: str, reason: str) -> NoReturn:
    message = f"real {engine} conformance unavailable: {reason}"
    if os.environ.get(required_env) == "1":
        pytest.fail(message)
    pytest.skip(message)


def new_trino_adapter(*, timeout_seconds: int = 120) -> TrinoAdapter:
    """One adapter on a local, unauthenticated coordinator."""
    return TrinoAdapter(
        host=os.environ.get("TRIPL_CONF_TRINO_HOST", "localhost"),
        port=int(os.environ.get("TRIPL_CONF_TRINO_PORT", "8080")),
        database=os.environ.get("TRIPL_CONF_TRINO_CATALOG", "memory"),
        username=os.environ.get("TRIPL_CONF_TRINO_USER", "tripl"),
        http_scheme="http",
        timeout_seconds=timeout_seconds,
    )


def new_athena_adapter() -> AthenaAdapter:
    """One adapter on a real Athena workgroup, from the environment."""
    region = os.environ.get("TRIPL_CONF_ATHENA_REGION", "").strip()
    key_id = os.environ.get("TRIPL_CONF_ATHENA_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("TRIPL_CONF_ATHENA_SECRET_ACCESS_KEY", "")
    missing = [
        name
        for name, value in (
            ("TRIPL_CONF_ATHENA_REGION", region),
            ("TRIPL_CONF_ATHENA_ACCESS_KEY_ID", key_id),
            ("TRIPL_CONF_ATHENA_SECRET_ACCESS_KEY", secret),
        )
        if not value
    ]
    if missing:
        unavailable("Athena", ATHENA_REQUIRED_ENV, f"missing {', '.join(missing)}")
    return AthenaAdapter(
        host=region,
        port=443,
        database="default",
        username=key_id,
        password=secret,
        region=region,
        work_group=os.environ.get("TRIPL_CONF_ATHENA_WORKGROUP", "").strip() or None,
        s3_output_location=os.environ.get("TRIPL_CONF_ATHENA_OUTPUT", "").strip() or None,
        timeout_seconds=300,
    )


def _string_literal(value: str) -> str:
    """A Trino string literal: no escape sequences, the quote is doubled."""
    return "'" + value.replace("'", "''") + "'"


def _row_select(row: FixtureRow) -> str:
    micros = row.ts.strftime("%Y-%m-%d %H:%M:%S.%f")
    # Milliseconds are TRUNCATED, not rounded, so a row stays in its bucket:
    # 00:14:59.999999 is stored as 00:14:59.999, never as 00:15:00.000.
    millis = micros[:-3]
    amount = "CAST(NULL AS double)" if row.amount is None else f"CAST({row.amount!r} AS double)"
    document = json.dumps(row.doc, ensure_ascii=True, separators=(",", ":"))
    return (
        f'SELECT {row.id} AS "id", '
        f"TIMESTAMP '{micros} UTC' AS \"ts\", "
        f"TIMESTAMP '{millis} UTC' AS \"ts_ms\", "
        f"TIMESTAMP '{micros}' AS \"naive\", "
        f"DATE '{row.ts.date().isoformat()}' AS \"d\", "
        f'CAST({_string_literal(row.event_name)} AS varchar) AS "event_name", '
        f'{amount} AS "amount", '
        f'CAST({_string_literal(row.user_id)} AS varchar) AS "user_id", '
        f'json_parse({_string_literal(document)}) AS "doc", '
        f'CAST({_string_literal(document)} AS varchar) AS "doc_text"'
    )


def render_trino_rows(rows: Iterable[FixtureRow] = ROWS) -> str:
    """The canonical fixture as one typed, table-less Trino relation."""
    rendered = " UNION ALL ".join(_row_select(row) for row in rows)
    if not rendered:
        raise ValueError("Trino conformance rows must not be empty")
    return rendered


BASE = render_trino_rows()
