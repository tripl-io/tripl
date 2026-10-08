"""Credentialed value conformance against a real Amazon Athena workgroup.

The same assertions as the Trino gate (``trino_values``), over the same
table-less fixture: Athena engine version 3 is Trino SQL, so what differs is the
connection. Nothing is created; each statement writes its result set to the
workgroup's output location, as every Athena query does.

Not part of the ordinary ``conformance`` CI job: it carries the ``athena_value``
marker that job excludes, and skips without credentials.
``athena-value-conformance.yml`` runs it on release tags once the repository
has an account configured. **It has not yet run against a live account.** By hand:

    TRIPL_CONF_ATHENA_REGION=eu-west-1 TRIPL_CONF_ATHENA_ACCESS_KEY_ID=... \\
    TRIPL_CONF_ATHENA_SECRET_ACCESS_KEY=... TRIPL_CONF_ATHENA_OUTPUT=s3://bucket/tripl/ \\
    TRIPL_ATHENA_VALUE_REQUIRED=1 uv run pytest -q -m athena_value \\
        src/tripl/tests/conformance/test_athena_value_conformance.py
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tripl.core.adapters.athena import AthenaAdapter
from tripl.tests.conformance.trino_live import (
    ATHENA_REQUIRED_ENV,
    BASE,
    new_athena_adapter,
    unavailable,
)
from tripl.tests.conformance.trino_values import *  # noqa: F403 - the shared tests

pytestmark = pytest.mark.athena_value


@pytest.fixture(scope="module")
def engine() -> Iterator[AthenaAdapter]:
    adapter: AthenaAdapter | None = None
    try:
        try:
            adapter = new_athena_adapter()
            adapter.test_connection()
            adapter.get_columns(BASE)
        except Exception as exc:  # noqa: BLE001 - any auth or network failure is unavailable
            unavailable("Athena", ATHENA_REQUIRED_ENV, str(exc))
        yield adapter
    finally:
        if adapter is not None:
            adapter.close()
