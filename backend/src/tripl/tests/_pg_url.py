"""Shared conversion for PostgreSQL migration tests using asyncpg."""

import pytest

from tripl.tests.test_alert_digest_concurrency_pg import _PG_URL


def asyncpg_url() -> str:
    assert _PG_URL is not None
    prefix = "postgresql+psycopg://"
    if not _PG_URL.startswith(prefix):
        pytest.skip("TRIPL_TEST_PG_URL must use postgresql+psycopg for asyncpg tests")
    return "postgresql+asyncpg://" + _PG_URL.removeprefix(prefix)
