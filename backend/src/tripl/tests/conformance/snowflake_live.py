"""Credential wiring and the table-less fixture for real-Snowflake value conformance.

Nothing here creates a table: the canonical nine-row fixture is rendered as a
``UNION ALL`` of typed ``SELECT`` rows, so the user needs ``USAGE`` on a
warehouse and nothing else. Credentials come from the environment only and are
never written anywhere:

* ``TRIPL_CONF_SF_ACCOUNT`` — the account identifier (``myorg-myaccount``);
* ``TRIPL_CONF_SF_USER`` — the user;
* ``TRIPL_CONF_SF_WAREHOUSE`` — the virtual warehouse statements run on;
* ``TRIPL_CONF_SF_PRIVATE_KEY`` — the user's PEM private key (key-pair sign-in),
  or ``TRIPL_CONF_SF_PASSWORD`` instead;
* ``TRIPL_CONF_SF_DATABASE`` / ``TRIPL_CONF_SF_ROLE`` — optional; the session's
  database and role, which the fixture never reads from.

Without them every test skips; with ``TRIPL_SF_VALUE_REQUIRED=1`` a missing or
unreachable account is a failure instead.

Every column is aliased QUOTED and lower-case: Snowflake upper-cases an unquoted
alias, and the shared fixture's contracts name ``event_name``, ``user_id``...
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import NoReturn

import pytest

from tripl.core.adapters.snowflake import SnowflakeAdapter
from tripl.core.adapters.snowflake_sql import resolve_host
from tripl.tests.conformance.dataset import ROWS, FixtureRow

REQUIRED_ENV = "TRIPL_SF_VALUE_REQUIRED"


def unavailable(reason: str) -> NoReturn:
    message = f"real Snowflake conformance unavailable: {reason}"
    if os.environ.get(REQUIRED_ENV) == "1":
        pytest.fail(message)
    pytest.skip(message)


def new_adapter() -> SnowflakeAdapter:
    """One real adapter with a short statement budget."""
    account = os.environ.get("TRIPL_CONF_SF_ACCOUNT", "").strip()
    user = os.environ.get("TRIPL_CONF_SF_USER", "").strip()
    warehouse = os.environ.get("TRIPL_CONF_SF_WAREHOUSE", "").strip()
    private_key = os.environ.get("TRIPL_CONF_SF_PRIVATE_KEY", "").strip()
    password = os.environ.get("TRIPL_CONF_SF_PASSWORD", "").strip()
    missing = [
        name
        for name, value in (
            ("TRIPL_CONF_SF_ACCOUNT", account),
            ("TRIPL_CONF_SF_USER", user),
            ("TRIPL_CONF_SF_WAREHOUSE", warehouse),
            ("TRIPL_CONF_SF_PRIVATE_KEY or TRIPL_CONF_SF_PASSWORD", private_key or password),
        )
        if not value
    ]
    if missing:
        unavailable(f"missing {', '.join(missing)}")
    target = resolve_host(account)
    return SnowflakeAdapter(
        host=target.hostname,
        port=443,
        database=os.environ.get("TRIPL_CONF_SF_DATABASE", "").strip(),
        username=user,
        password=private_key or password,
        account=target.account,
        warehouse=warehouse,
        auth_type="key_pair" if private_key else "password",
        role=os.environ.get("TRIPL_CONF_SF_ROLE", "").strip() or None,
        # A suspended warehouse needs a few seconds to resume for the first statement.
        timeout_seconds=120,
    )


def _string_literal(value: str) -> str:
    """A Snowflake string literal: backslash escapes, as the adapter's quoting uses."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"


def _row_select(row: FixtureRow) -> str:
    instant = row.ts.strftime("%Y-%m-%d %H:%M:%S.%f")
    amount = "NULL::FLOAT" if row.amount is None else f"{row.amount!r}::FLOAT"
    document = json.dumps(row.doc, ensure_ascii=True, separators=(",", ":"))
    return (
        f'SELECT {row.id} AS "id", '
        f"'{instant} +00:00'::TIMESTAMP_TZ AS \"ts\", "
        f"'{instant}'::TIMESTAMP_NTZ AS \"ntz\", "
        f"'{instant} +00:00'::TIMESTAMP_LTZ AS \"ltz\", "
        f"'{row.ts.date().isoformat()}'::DATE AS \"d\", "
        f'{_string_literal(row.event_name)}::STRING AS "event_name", '
        f'{amount} AS "amount", '
        f'{_string_literal(row.user_id)}::STRING AS "user_id", '
        f'PARSE_JSON({_string_literal(document)}) AS "doc", '
        f'{_string_literal(document)}::STRING AS "doc_text"'
    )


def render_snowflake_rows(rows: Iterable[FixtureRow] = ROWS) -> str:
    """The canonical fixture as one typed, table-less Snowflake relation."""
    rendered = " UNION ALL ".join(_row_select(row) for row in rows)
    if not rendered:
        raise ValueError("Snowflake conformance rows must not be empty")
    return rendered


BASE = render_snowflake_rows()
