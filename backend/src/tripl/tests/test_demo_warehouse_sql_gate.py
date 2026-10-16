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
        # Functions that run a query given as text.
        ("SELECT query_to_xml('select 1', true, true, '')", "query_to_xml"),
        (
            "SELECT query_to_xml_and_xmlschema('select 1', true, true, '')",
            "query_to_xml_and_xmlschema",
        ),
        ("SELECT cursor_to_xml('c', 1, true, true, '')", "cursor_to_xml"),
        ("SELECT table_to_xmlschema('events', true, true, '')", "table_to_xmlschema"),
        ("SELECT schema_to_xml('public', true, true, '')", "schema_to_xml"),
        ("SELECT database_to_xml(true, true, '')", "database_to_xml"),
        ("SELECT * FROM ts_stat('select 1')", "ts_stat"),
        ("SELECT ts_rewrite('a'::tsquery, 'select 1')", "ts_rewrite"),
        # A name inside a literal is refused too: query_to_xml would run it, and
        # the probes below make a literal's bounds a matter of opinion.
        (
            "SELECT query_to_xml('select pg_terminate_backend(pid) from pg_stat_activity', "
            "true, true, '')",
            "query_to_xml",
        ),
        ("SELECT count(*) FROM events WHERE event_name = 'set_config(x)'", "set_config"),
        ("SELECT count(*) FROM events WHERE screen_name = 'pg_stat_activity'", "pg_stat_activity"),
        # A quote inside a comment or a dollar quote opens no literal for
        # PostgreSQL, but it does for a scanner that skips literals.
        ("SELECT 1 /* ' */, pg_terminate_backend(123) -- '", "pg_terminate_backend"),
        ("SELECT 1 -- '\n, pg_terminate_backend(123) -- '", "pg_terminate_backend"),
        ("SELECT $$'$$, pg_cancel_backend(1), $$'$$", "pg_cancel_backend"),
        ("SELECT $a$'$a$ AS x, set_config('statement_timeout','0',false), $a$'$a$", "set_config"),
    ],
)
def test_a_session_reaching_call_is_refused(statement: str, named: str) -> None:
    with pytest.raises(ValueError, match=f"does not allow {named} anywhere in a query"):
        check_demo_warehouse_sql(statement)


@pytest.mark.parametrize(
    ("statement", "refused"),
    [
        ('SELECT U&"\\0070g_sleep"(1)', "identifiers"),
        ("SELECT $$x$$", "dollar-quoted"),
        ("SELECT $body$x$body$ AS b", "dollar-quoted"),
        ("SELECT 1 -- note", "comments"),
        ("SELECT 1 /* note */", "comments"),
        ("SELECT 1 */", "comments"),
    ],
)
def test_text_that_hides_text_is_refused(statement: str, refused: str) -> None:
    with pytest.raises(ValueError, match=refused):
        check_demo_warehouse_sql(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM events",
        "SELECT current_setting('statement_timeout')",
        # Ordinary literals, and one ``$`` (a regex anchor) is not a dollar quote.
        "SELECT count(*) FROM events WHERE event_name = 'Purchase Completed'",
        "SELECT count(*) FROM events WHERE amount ~ '^[0-9]+$' AND x - 1 > 0",
        # Longer names that merely contain a denylisted one.
        "SELECT my_set_config, set_configs FROM t",
        'SELECT "event_name" FROM "events"',
        "SELECT U&'d\\0061ta'",
    ],
)
def test_ordinary_sql_passes(statement: str) -> None:
    check_demo_warehouse_sql(statement)
