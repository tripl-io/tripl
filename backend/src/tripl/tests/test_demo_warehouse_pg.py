"""The demo warehouse on a real PostgreSQL: provisioning, the reader's confinement,
the writer, and the two engines agreeing.

Needs ``TRIPL_TEST_PG_URL`` with a role that may create databases and roles (CI's
``tripl`` superuser); skips without it and fails when ``TRIPL_TEST_PG_REQUIRED=1``
finds no database, like every PostgreSQL gate here. It creates the warehouse
database ``<test database>_demo`` and the role ``<that database>_reader``, and
drops both when it is done.

The reader assertions are the security contract of the feature: the role can
read the two warehouse tables and nothing else, cannot write, cannot create a
temporary table, cannot lift ``temp_file_limit``, cannot reach other sessions
(cancel, terminate, advisory locks, ``pg_stat_activity``), and cannot carry a
session setting from one statement into the next — and the adapter refuses
those calls before they are sent.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql

from tripl.config import settings
from tripl.core.adapters.base import AggregateSpec, BaseAdapter
from tripl.core.adapters.demo_memory import DemoMemoryAdapter
from tripl.core.adapters.demo_postgres import DemoPostgresAdapter
from tripl.core.demo_warehouse import connection, engine, provision, rows, schema, status, writer
from tripl.models.domain_enums import MetricAggregation
from tripl.tests.test_alert_digest_concurrency_pg import _PG_URL, _engine_or_skip

pytestmark = pytest.mark.postgres

_PROBE_TABLE = "demo_warehouse_probe"
_SECRET = "demo-warehouse-test-secret-key-0123456789"


def _superuser() -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(**connection.main_connection().kwargs(), autocommit=True)  # type: ignore[arg-type]


def _drop_warehouse() -> None:
    database = connection.writer_connection().database
    with _superuser() as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
        )
        conn.execute(
            sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(connection.reader_role()))
        )
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(_PROBE_TABLE)))


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def warehouse(monkeypatch: pytest.MonkeyPatch, now: datetime) -> Iterator[datetime]:
    """A freshly provisioned warehouse holding two days, written through ``now``."""
    _engine_or_skip().dispose()
    assert _PG_URL is not None
    monkeypatch.setattr(settings, "sync_database_url", _PG_URL)
    monkeypatch.setattr(settings, "secret_key", _SECRET)
    monkeypatch.setattr(settings, "demo_warehouse_url", "")
    monkeypatch.setattr(settings, "demo_warehouse_reader_password", "")
    monkeypatch.setattr(settings, "demo_warehouse_mode", "auto")
    # Two days instead of thirty keep the test fast; the field's own floor (23)
    # applies to configuration, which this bypasses.
    monkeypatch.setattr(settings, "demo_warehouse_retention_days", 2)
    monkeypatch.setattr(status, "READY_HISTORY", timedelta(days=1))
    status.clear_cache()
    rows.clear_cache()
    _drop_warehouse()
    with _superuser() as conn:
        conn.execute(sql.SQL("CREATE TABLE {} (secret text)").format(sql.Identifier(_PROBE_TABLE)))
        conn.execute(
            sql.SQL("INSERT INTO {} VALUES ('main database')").format(sql.Identifier(_PROBE_TABLE))
        )
    try:
        result = provision.provision()
        assert result.ready, result.warnings
        run = writer.write(now=now)
        assert run is not None and run.rows_written > 0
        yield now
    finally:
        status.clear_cache()
        _drop_warehouse()


def _reader() -> psycopg.Connection[tuple[object, ...]]:
    """A direct reader connection, without the adapter's per-statement pins."""
    reader = connection.reader_connection()
    assert reader is not None
    return psycopg.connect(**reader.kwargs(), autocommit=True)  # type: ignore[arg-type]


# -- provisioning and the writer ------------------------------------------------


def test_provisioning_is_idempotent(warehouse: datetime) -> None:
    again = provision.provision()
    assert again.ready, again.warnings
    assert not again.warnings
    # A superuser sets every reader limit, temp_file_limit included.
    assert status.warehouse_status(refresh=True).reader_limits_missing == ()


def test_the_reader_password_is_never_sent_in_plain_text(warehouse: datetime) -> None:
    with _superuser() as conn:
        stored = conn.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (connection.reader_role(),)
        ).fetchone()
    assert stored is not None and str(stored[0]).startswith("SCRAM-SHA-256$")
    password = connection.reader_password()
    assert password is not None and password not in str(stored[0])
    # And the verifier logs in.
    with _reader() as conn:
        assert conn.execute("SELECT current_user").fetchone() == (connection.reader_role(),)


def test_the_writer_writes_each_row_once_and_resumes(warehouse: datetime) -> None:
    through = rows.available_through(warehouse)
    keep_from = writer.retention_start(warehouse)
    state = status.warehouse_status(refresh=True)
    assert state.written_through == through
    assert state.oldest == keep_from
    assert state.engine == "postgres"

    # Same instant: nothing left to write.
    repeat = writer.write(now=warehouse)
    assert repeat is not None and repeat.rows_written == 0

    # Half an hour later: exactly the model's rows for that half hour.
    later = warehouse + timedelta(minutes=30)
    resumed = writer.write(now=later)
    assert resumed is not None
    expected = sum(
        len(list(rows.rows_between(table, through, rows.available_through(later))))
        for table in schema.TABLE_COLUMNS
    )
    assert resumed.rows_written == expected

    with psycopg.connect(**connection.writer_connection().kwargs()) as conn:  # type: ignore[arg-type]
        stored = conn.execute(
            "SELECT count(*), count(DISTINCT (event_time, event_name, user_id, platform)) "
            "FROM warehouse.events WHERE event_time >= %s AND event_time < %s",
            (keep_from, rows.available_through(later)),
        ).fetchone()
    model = list(rows.rows_between("events", keep_from, rows.available_through(later)))
    assert stored is not None and stored[0] == len(model)


def test_retention_and_the_size_cap_drop_whole_days(
    warehouse: datetime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "demo_warehouse_retention_days", 1)
    run = writer.write(now=warehouse)
    assert run is not None and run.dropped_days >= 1
    assert run.oldest == writer.retention_start(warehouse)

    monkeypatch.setattr(settings, "demo_warehouse_max_mb", 1)
    monkeypatch.setattr(writer, "MIN_RETAINED_DAYS", 0)
    with caplog.at_level(logging.ERROR, logger=writer.__name__):
        capped = writer.write(now=warehouse)
    assert capped is not None and capped.dropped_days >= 1
    assert any("DEMO_WAREHOUSE_MAX_MB" in record.getMessage() for record in caplog.records)


def test_the_size_cap_never_drops_the_retained_floor(
    warehouse: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = warehouse.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    with psycopg.connect(**connection.writer_connection().kwargs(), autocommit=True) as conn:  # type: ignore[arg-type]
        before = writer._partitions(conn, "events")
    assert today - timedelta(days=2) in before  # the fixture keeps two days back
    # Over any cap, with a one-day floor: the day before yesterday goes, the
    # floor (yesterday, today) stays however far over the cap the database is.
    monkeypatch.setattr(settings, "demo_warehouse_max_mb", 1)
    monkeypatch.setattr(writer, "MIN_RETAINED_DAYS", 1)
    run = writer.write(now=warehouse)
    assert run is not None
    with psycopg.connect(**connection.writer_connection().kwargs(), autocommit=True) as conn:  # type: ignore[arg-type]
        after = writer._partitions(conn, "events")
    assert today - timedelta(days=2) not in after
    assert yesterday in after and today in after


def test_two_writers_never_interleave(warehouse: datetime) -> None:
    # The lock lives in tripl's main database, where no reader goes.
    with psycopg.connect(**connection.main_connection().kwargs(), autocommit=True) as holder:  # type: ignore[arg-type]
        holder.execute("SELECT pg_advisory_lock(%s)", (writer.ADVISORY_LOCK_KEY,))
        assert writer.write(now=warehouse + timedelta(minutes=10)) is None


def test_a_reader_holding_the_old_lock_key_does_not_stall_the_writer(warehouse: datetime) -> None:
    # The key in the WAREHOUSE database is not the writer's lock any more (and
    # the reader may not take advisory locks there at all, see below).
    with psycopg.connect(**connection.writer_connection().kwargs(), autocommit=True) as holder:  # type: ignore[arg-type]
        holder.execute("SELECT pg_advisory_lock(%s)", (writer.ADVISORY_LOCK_KEY,))
        assert writer.write(now=warehouse + timedelta(minutes=10)) is not None


# -- the reader's confinement ------------------------------------------------------


def test_the_reader_cannot_write_even_after_lifting_read_only(warehouse: datetime) -> None:
    with _reader() as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        conn.execute("SET default_transaction_read_only = off")
        for statement in (
            "INSERT INTO warehouse.events (event_time, event_type, event_name, platform, "
            "app_version, country, user_id, session_id) "
            "VALUES (now(), 'x', 'x', 'ios', '1', 'US', 'u', 's')",
            "DELETE FROM warehouse.orders",
            "CREATE TABLE warehouse.mine (a int)",
            "CREATE TABLE public.mine (a int)",
            "CREATE TEMP TABLE mine (a int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_the_reader_sees_only_the_two_warehouse_tables(warehouse: datetime) -> None:
    with _reader() as conn:
        for statement in (
            f"SELECT * FROM {schema.STATE_TABLE}",
            "SELECT * FROM warehouse.events_p" + f"{warehouse:%Y%m%d}",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
    adapter = engine.build_postgres_adapter()
    try:
        assert sorted(t.name for t in adapter.get_schema_tables()) == ["events", "orders"]
    finally:
        adapter.close()

    # tripl's own database: the reader may connect (PUBLIC's default) and read nothing.
    main = connection.main_connection()
    reader = connection.reader_connection()
    assert reader is not None
    into_main = connection.WarehouseConnection(
        host=main.host,
        port=main.port,
        database=main.database,
        user=reader.user,
        password=reader.password,
        sslmode=main.sslmode,
    )
    with (
        psycopg.connect(**into_main.kwargs(), autocommit=True) as conn,  # type: ignore[arg-type]
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        # Schema-qualified: the reader's role-level search_path (``warehouse``)
        # follows it into every database, so a bare name would not resolve here
        # and the refusal under test would never be reached.
        conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier("public", _PROBE_TABLE)))


def test_the_reader_cannot_lift_its_temp_file_limit(warehouse: datetime) -> None:
    with _reader() as conn:
        assert conn.execute("SHOW temp_file_limit").fetchone() == ("256MB",)
        assert conn.execute("SHOW statement_timeout").fetchone() == ("15s",)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SET temp_file_limit = '10GB'")


def test_the_reader_cannot_reach_other_sessions(warehouse: datetime) -> None:
    # Revoked from PUBLIC inside the demo database (a superuser provisioned it).
    with _reader() as conn:
        for statement in (
            "SELECT pg_cancel_backend(pg_backend_pid())",
            "SELECT pg_terminate_backend(pg_backend_pid())",
            "SELECT pg_try_advisory_lock(1)",
            "SELECT pg_advisory_xact_lock(1)",
            "SELECT query FROM pg_stat_activity",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def test_the_adapter_refuses_session_calls_before_sending_them(warehouse: datetime) -> None:
    adapter = engine.build_postgres_adapter(timeout_seconds=1)
    assert isinstance(adapter, DemoPostgresAdapter)
    try:
        with adapter._conn.cursor() as cur:
            for statement in (
                "SELECT set_config('statement_timeout', '0', false)",
                "SELECT pg_sleep(10)",
                "SELECT pg_try_advisory_lock(1)",
                "SELECT pg_cancel_backend(1)",
                "SELECT count(*) FROM pg_stat_activity",
            ):
                with pytest.raises(ValueError, match="demo warehouse does not allow"):
                    cur.execute(statement)
            # The name inside a string literal is data, not a call.
            cur.execute("SELECT count(*) FROM events WHERE event_name = 'set_config(x)'")
            assert cur.fetchone() == (0,)
    finally:
        adapter.close()


def test_a_session_setting_cannot_outlive_its_statement(warehouse: datetime) -> None:
    adapter = engine.build_postgres_adapter(timeout_seconds=1)
    assert isinstance(adapter, DemoPostgresAdapter)
    try:
        # Past the adapter's denylist (a plain cursor), as if a call slipped it.
        with psycopg.Cursor(adapter._conn) as raw:
            raw.execute(
                "SELECT set_config('statement_timeout', '0', false), "
                "set_config('standard_conforming_strings', 'off', false), "
                "set_config('search_path', 'public', false), "
                "set_config('work_mem', '2GB', false), "
                "set_config('max_parallel_workers_per_gather', '8', false), "
                "set_config('idle_in_transaction_session_timeout', '0', false)"
            )
        with adapter._conn.cursor() as cur:
            for setting, pinned in (
                ("statement_timeout", "1s"),
                ("standard_conforming_strings", "on"),
                ("work_mem", "16MB"),
                ("max_parallel_workers_per_gather", "0"),
                ("idle_in_transaction_session_timeout", "30s"),
            ):
                cur.execute(sql.SQL("SHOW {}").format(sql.Identifier(setting)))
                assert cur.fetchone() == (pinned,), setting
            cur.execute("SELECT count(*) > 0 FROM events")
            assert cur.fetchone() == (True,)
            started = datetime.now(UTC)
            # CPU only: a set-returning call in the select list is evaluated
            # row by row, so it never spills into temp_file_limit first.
            with pytest.raises(psycopg.errors.QueryCanceled):
                cur.execute("SELECT count(*) FROM (SELECT generate_series(1, 2000000000)) AS s")
            assert datetime.now(UTC) - started < timedelta(seconds=5)
    finally:
        adapter.close()


# -- the engines --------------------------------------------------------------------


def _normalize(value: object) -> object:
    if isinstance(value, Decimal | float):
        return round(float(value), 2)
    if isinstance(value, list):
        return tuple(value)
    return value


def _rows(out: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    normalized = [tuple(_normalize(value) for value in row) for row in out]
    return sorted(normalized, key=repr)


def test_the_two_engines_answer_alike(warehouse: datetime) -> None:
    status.clear_cache()
    postgres = engine.build_demo_adapter()
    assert isinstance(postgres, DemoPostgresAdapter)
    memory: BaseAdapter = DemoMemoryAdapter(now=warehouse)
    t1 = warehouse.replace(minute=0, second=0, microsecond=0)
    t0 = t1 - timedelta(hours=6)
    try:
        for adapter in (postgres, memory):
            adapter.get_columns(schema.EVENTS_SCAN_SQL)
        assert [(c.name, c.type_name) for c in postgres.get_columns(schema.EVENTS_SCAN_SQL)] == [
            (c.name, c.type_name) for c in memory.get_columns(schema.EVENTS_SCAN_SQL)
        ]

        def both(method: str, *args: object, **kwargs: object) -> tuple[object, object]:
            return (
                getattr(postgres, method)(*args, **kwargs),
                getattr(memory, method)(*args, **kwargs),
            )

        reg = ["event_type", "event_name", "platform", "app_version", "country", "currency"]
        pg, mem = both(
            "get_time_bucketed_counts",
            schema.EVENTS_SCAN_SQL,
            "event_time",
            "1h",
            reg,
            ["properties"],
            {"properties": list(schema.SUGGESTED_JSON_VALUE_PATHS)},
            t0,
            t1,
        )
        assert pg[0] == mem[0] and pg[1] == mem[1]  # type: ignore[index]
        assert _rows(pg[2]) == _rows(mem[2])  # type: ignore[index]

        pg, mem = both(
            "get_time_bucketed_breakdown_counts_multi",
            schema.EVENTS_SCAN_SQL,
            "event_time",
            "1h",
            ["platform", "properties.source"],
            # A breakdown is a scan column or a property of one, as a
            # collection passes it: the scalar one is among the regular columns.
            ["event_type", "platform"],
            ["properties"],
            None,
            t0,
            t1,
            values_limit=4,
        )
        assert _rows(pg[2]) == _rows(mem[2])  # type: ignore[index]

        pg, mem = both(
            "get_full_breakdown",
            schema.EVENTS_SCAN_SQL,
            ["event_name", "amount"],
            ["properties"],
            {"properties": ["payment_method"]},
            "event_time",
            t0,
            t1,
        )
        assert _rows(pg[3]) == _rows(mem[3])  # type: ignore[index]

        revenue = [
            AggregateSpec(key="revenue", aggregation=MetricAggregation.sum, column="amount"),
            AggregateSpec(
                key="refunded",
                aggregation=MetricAggregation.count,
                column=None,
                filter_sql="status = 'refunded'",
            ),
        ]
        # A fact query is introspected before it is aggregated, as a collection
        # does: the adapters check every column against the last introspection.
        for adapter in (postgres, memory):
            adapter.get_columns(schema.ORDERS_SQL)
        pg, mem = both(
            "get_time_bucketed_multi_aggregate_breakdown",
            schema.ORDERS_SQL,
            "created_at",
            "1d",
            "country",
            revenue,
            t1 - timedelta(days=1),
            t1,
            values_limit=5,
        )
        assert _rows(pg[1]) == _rows(mem[1])  # type: ignore[index]

        day = t1.replace(hour=0) - timedelta(days=1)
        for adapter in (postgres, memory):
            adapter.get_columns(schema.ACTIVE_SESSIONS_SQL)
        pg, mem = both(
            "get_preview_rows",
            schema.ACTIVE_SESSIONS_SQL,
            10,
            time_column="ts",
            time_from=day,
            time_to=day + timedelta(hours=1),
        )
        assert pg == mem
    finally:
        postgres.close()


def test_a_reader_that_cannot_connect_falls_back_to_memory(
    warehouse: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    status.clear_cache()
    assert status.warehouse_status().engine == "postgres"
    monkeypatch.setattr(connection, "reader_password", lambda: "not-the-password")
    adapter = engine.build_demo_adapter()
    assert isinstance(adapter, DemoMemoryAdapter)
    fallback = status.warehouse_status()
    assert fallback.engine == "memory" and fallback.reason
    assert "in memory" in engine.connection_message(adapter)


def test_the_warehouse_holds_about_what_the_model_promises(warehouse: datetime) -> None:
    """Two days on disk, to project the thirty-day size in the docs and the report."""
    with psycopg.connect(**connection.writer_connection().kwargs()) as conn:  # type: ignore[arg-type]
        counted = conn.execute("SELECT count(*) FROM warehouse.events").fetchone()
        size = conn.execute(
            "SELECT sum(pg_total_relation_size(c.oid)) FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'events'"
        ).fetchone()
    assert counted is not None and size is not None
    per_row = int(size[0]) / int(counted[0])
    by_day = collections.Counter(
        row["event_time"].date()  # type: ignore[attr-defined]
        for row in rows.rows_between("events", warehouse - timedelta(days=1), warehouse)
    )
    projected_mb = per_row * sum(by_day.values()) * 30 / 1024 / 1024
    # Comfortably inside the 1 GB default cap.
    assert projected_mb < settings.demo_warehouse_max_mb * 0.75, projected_mb
