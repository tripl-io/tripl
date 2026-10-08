"""Value conformance against a real Trino coordinator (credential-free, in Docker).

The mocked suites (``test_trino_adapter.py`` and the parity files) pin the SQL
the adapter sends; this module proves Trino accepts it and computes what the
shared reference says. It reads a table-less fixture (``trino_live.BASE``), so
nothing is created or left behind, and the session catalog is never read from.

Not part of the ordinary ``conformance`` CI job: it carries the ``trino_value``
marker that job excludes, and the dedicated ``trino`` job runs it against a
``trinodb/trino`` container. By hand:

    docker run -d --name trino -p 8080:8080 trinodb/trino:483
    TRIPL_TRINO_VALUE_REQUIRED=1 uv run pytest -q -m trino_value \\
        src/tripl/tests/conformance/test_trino_value_conformance.py
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tripl.core.adapters.trino import TrinoAdapter
from tripl.tests.conformance.trino_live import (
    BASE,
    TRINO_REQUIRED_ENV,
    new_trino_adapter,
    unavailable,
)
from tripl.tests.conformance.trino_values import *  # noqa: F403 - the shared tests

pytestmark = pytest.mark.trino_value


@pytest.fixture(scope="module")
def engine() -> Iterator[TrinoAdapter]:
    adapter: TrinoAdapter | None = None
    try:
        try:
            adapter = new_trino_adapter()
            adapter.test_connection()
            adapter.get_columns(BASE)
        except Exception as exc:  # noqa: BLE001 - any network failure is unavailable
            unavailable("Trino", TRINO_REQUIRED_ENV, str(exc))
        yield adapter
    finally:
        if adapter is not None:
            adapter.close()


def test_a_runaway_statement_is_cut_off_as_a_timeout() -> None:
    """``query_max_run_time`` is enforced by the coordinator and reported plainly."""
    try:
        adapter = new_trino_adapter(timeout_seconds=2)
    except Exception as exc:  # noqa: BLE001 - any network failure is unavailable
        unavailable("Trino", TRINO_REQUIRED_ENV, str(exc))
    # CPU-bound and small in memory: ten million rows, each filtering a fresh
    # 10 000-element array. A cross join of large relations would instead
    # materialise its build side and could take the coordinator down with it.
    endless = (
        "SELECT sum(cardinality(filter(sequence(1, 10000), v -> v % (x + y) = 0))) "
        "FROM UNNEST(sequence(1, 10000)) AS a(x) CROSS JOIN UNNEST(sequence(1, 1000)) AS b(y)"
    )
    try:
        with pytest.raises(TimeoutError, match="timeout configured for this data source"):
            adapter._run(endless)
    finally:
        adapter.close()
