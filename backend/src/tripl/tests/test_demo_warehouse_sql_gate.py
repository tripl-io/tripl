"""The demo warehouse's SQL denylist (``check_demo_warehouse_sql``).

``DemoPostgresAdapter`` runs it on every statement before sending it; the
PostgreSQL side (revoked functions, re-armed settings) is covered by
``test_demo_warehouse_pg``.
"""

from __future__ import annotations

import pytest

from tripl.core.adapters.measure_validator import check_demo_warehouse_sql


@pytest.mark.parametrize(
    ("statement", "named"),
    [
        ("SELECT set_config('statement_timeout', '0', false)", "set_config"),
        ("select PG_SLEEP (1)", "pg_sleep"),
        ("SELECT pg_sleep_for('1 minute')", "pg_sleep_for"),
        ("SELECT pg_try_advisory_xact_lock(1)", "pg_try_advisory_xact_lock"),
        ("SELECT pg_advisory_lock(1, 2)", "pg_advisory_lock"),
        ("SELECT pg_cancel_backend(42)", "pg_cancel_backend"),
        ("SELECT pg_terminate_backend(42)", "pg_terminate_backend"),
        ("SELECT * FROM pg_stat_activity", "pg_stat_activity"),
        ("SELECT pg_stat_get_activity(NULL)", "pg_stat_get_activity"),
        ("SELECT pg_stat_get_backend_pid(1)", "pg_stat_get_backend_pid"),
        ("SELECT lo_get(16384)", "lo_get"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT pg_ls_dir('.')", "pg_ls_dir"),
        ("SELECT pg_notify('c', 'x')", "pg_notify"),
        # A quoted name names the same function, qualified or not.
        ('SELECT "pg_sleep"(1)', "pg_sleep"),
        ("SELECT pg_catalog.\"set_config\"('work_mem', '2GB', false)", "set_config"),
        # A comment between the name and its parenthesis is still a call.
        ("SELECT pg_sleep/* x */(1)", "pg_sleep"),
        ("SELECT pg_sleep -- x\n(1)", "pg_sleep"),
    ],
)
def test_a_session_reaching_call_is_refused(statement: str, named: str) -> None:
    with pytest.raises(ValueError, match=f"does not allow {named} in a query"):
        check_demo_warehouse_sql(statement)


def test_a_unicode_escape_identifier_is_refused() -> None:
    with pytest.raises(ValueError, match="identifiers"):
        check_demo_warehouse_sql('SELECT U&"\\0070g_sleep"(1)')


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM events",
        "SELECT current_setting('statement_timeout')",
        # A name inside a string literal is data.
        "SELECT count(*) FROM events WHERE event_name = 'set_config(x)'",
        "SELECT count(*) FROM events WHERE screen_name = 'pg_stat_activity'",
        # Longer names that merely contain a denylisted one.
        "SELECT my_set_config, set_configs FROM t",
        'SELECT "event_name" FROM "events"',
        "SELECT U&'d\\0061ta'",
    ],
)
def test_ordinary_sql_passes(statement: str) -> None:
    check_demo_warehouse_sql(statement)
