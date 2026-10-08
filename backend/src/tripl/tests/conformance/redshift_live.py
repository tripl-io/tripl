"""Credential wiring and the table-less fixture for real-Redshift value conformance.

Nothing here creates a table: the canonical nine-row fixture is rendered as a
``UNION ALL`` of typed ``SELECT`` rows, so the user needs to be able to sign in
and nothing else. Credentials come from the environment only and are never
written anywhere:

* ``TRIPL_CONF_RS_HOST`` — the cluster or Serverless workgroup endpoint;
* ``TRIPL_CONF_RS_PORT`` — optional, default 5439;
* ``TRIPL_CONF_RS_DATABASE`` — optional, default ``dev``;
* ``TRIPL_CONF_RS_USER`` / ``TRIPL_CONF_RS_PASSWORD`` — a database user.

Without them every test skips; with ``TRIPL_RS_VALUE_REQUIRED=1`` a missing or
unreachable endpoint is a failure instead.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import NoReturn

import pytest

from tripl.core.adapters.redshift import RedshiftAdapter
from tripl.tests.conformance.dataset import ROWS, FixtureRow

REQUIRED_ENV = "TRIPL_RS_VALUE_REQUIRED"


def unavailable(reason: str) -> NoReturn:
    message = f"real Redshift conformance unavailable: {reason}"
    if os.environ.get(REQUIRED_ENV) == "1":
        pytest.fail(message)
    pytest.skip(message)


def new_adapter() -> RedshiftAdapter:
    """One real adapter with a short statement budget."""
    host = os.environ.get("TRIPL_CONF_RS_HOST", "").strip()
    user = os.environ.get("TRIPL_CONF_RS_USER", "").strip()
    password = os.environ.get("TRIPL_CONF_RS_PASSWORD", "")
    missing = [
        name
        for name, value in (
            ("TRIPL_CONF_RS_HOST", host),
            ("TRIPL_CONF_RS_USER", user),
            ("TRIPL_CONF_RS_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        unavailable(f"missing {', '.join(missing)}")
    return RedshiftAdapter(
        host=host,
        port=int(os.environ.get("TRIPL_CONF_RS_PORT", "5439")),
        database=os.environ.get("TRIPL_CONF_RS_DATABASE", "").strip() or "dev",
        username=user,
        password=password,
        # A Serverless workgroup scaled to zero takes a while to answer the first one.
        timeout_seconds=120,
    )


def _string_literal(value: str) -> str:
    """A Redshift string literal: a backslash escapes, so it is doubled too."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _row_select(row: FixtureRow) -> str:
    instant = row.ts.strftime("%Y-%m-%d %H:%M:%S.%f")
    amount = "NULL::double precision" if row.amount is None else f"{row.amount!r}::double precision"
    return (
        f'SELECT {row.id} AS "id", '
        f"CAST('{instant}+00' AS TIMESTAMPTZ) AS \"ts\", "
        f"CAST('{instant}' AS TIMESTAMP) AS \"ts_naive\", "
        f'CAST({_string_literal(row.event_name)} AS VARCHAR(64)) AS "event_name", '
        f'{amount} AS "amount", '
        f'CAST({_string_literal(row.user_id)} AS VARCHAR(64)) AS "user_id"'
    )


def render_redshift_rows(rows: Iterable[FixtureRow] = ROWS) -> str:
    """The canonical fixture as one typed, table-less Redshift relation."""
    rendered = " UNION ALL ".join(_row_select(row) for row in rows)
    if not rendered:
        raise ValueError("Redshift conformance rows must not be empty")
    return rendered


BASE = render_redshift_rows()
