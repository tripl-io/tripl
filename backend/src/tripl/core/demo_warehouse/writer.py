"""The writer: keeps the PostgreSQL warehouse filled up to now, and bounded.

One run (the ``write_demo_warehouse`` beat task, every five minutes):

1. takes a session advisory lock IN TRIPL'S MAIN DATABASE, so two runs never
   interleave (a run that finds it held skips). Not in the warehouse database:
   the reader role connects there, and a visitor's query could take the same
   key and stall every run;
2. ensures the schema and a day partition for every day it will write;
3. rewrites everything when the model changed (:data:`~.rows.MODEL_VERSION`);
4. writes every row from where the last run stopped (``written_through``) to
   the end of the current slice (:func:`~.rows.available_through`), backfilling
   up to the retention window on the first run or after downtime. Each day is
   one transaction that also advances ``written_through``, so a crash leaves
   no duplicates and the next run resumes where the last commit ended;
5. drops the day partitions older than the retention window, then, while the
   database is over ``DEMO_WAREHOUSE_MAX_MB``, the oldest days beyond
   :data:`MIN_RETAINED_DAYS` (logging an error: the cap is a backstop, not the
   plan);
6. logs one line.

Its DDL (creating and dropping partitions) waits at most
:data:`DDL_LOCK_TIMEOUT` for a reader's query to finish: a partition change
locks the parent table, and every new reader query would queue behind a
waiting one. A run that times out stops there and the next run retries.

Skips silently (returns ``None``) when the warehouse is not provisioned or not
reachable: demos then run on the in-memory engine.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from tripl.config import settings
from tripl.core.demo_warehouse import connection, rows, schema

logger = logging.getLogger(__name__)

#: ``pg_try_advisory_lock`` key of a writer run ("tripldwh" as an int64).
ADVISORY_LOCK_KEY = int.from_bytes(b"tripldwh", "big") - 2**63
#: Days the size cap never drops: a demo replays this much history.
MIN_RETAINED_DAYS = 23
CONNECT_TIMEOUT_SECONDS = 5
#: How long the writer's DDL waits for the parent table's lock.
DDL_LOCK_TIMEOUT = "5s"
_DAY = timedelta(days=1)
_PARTITION_RE = re.compile(r"_p(\d{8})$")


@dataclass(frozen=True)
class WriteRun:
    """What one run did."""

    rows_written: int
    written_through: datetime
    oldest: datetime | None
    database_bytes: int
    dropped_days: int
    rewritten: bool


def _day_floor(at: datetime) -> datetime:
    """The UTC midnight of ``at`` (an aware instant in any zone)."""
    return at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def retention_start(now: datetime) -> datetime:
    """The oldest UTC day the warehouse keeps at ``now``."""
    return _day_floor(now - timedelta(days=settings.demo_warehouse_retention_days))


def _partitions(conn: psycopg.Connection[tuple[object, ...]], table: str) -> list[datetime]:
    found = conn.execute(
        "SELECT c.relname FROM pg_inherits i "
        "JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent "
        "JOIN pg_namespace n ON n.oid = p.relnamespace "
        "WHERE n.nspname = %s AND p.relname = %s",
        (schema.WAREHOUSE_SCHEMA, table),
    ).fetchall()
    days: list[datetime] = []
    for (name,) in found:
        match = _PARTITION_RE.search(str(name))
        if match:
            days.append(datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=UTC))
    return sorted(days)


def _drop_day(conn: psycopg.Connection[tuple[object, ...]], day: datetime) -> None:
    with conn.transaction():
        for table in schema.TABLE_COLUMNS:
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(
                    sql.Identifier(schema.WAREHOUSE_SCHEMA, schema.partition_name(table, day))
                )
            )


def _database_bytes(conn: psycopg.Connection[tuple[object, ...]]) -> int:
    row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    return int(row[0]) if row else 0  # type: ignore[call-overload]


def _copy_rows(conn: psycopg.Connection[tuple[object, ...]], start: datetime, end: datetime) -> int:
    written = 0
    with conn.cursor() as cur:
        for table, columns in schema.TABLE_COLUMNS.items():
            names = [name for name, _, _ in columns]
            json_columns = schema.JSON_COLUMNS[table]
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(schema.WAREHOUSE_SCHEMA, table),
                sql.SQL(", ").join(sql.Identifier(name) for name in names),
            )
            with cur.copy(statement) as copy:
                for row in rows.rows_between(table, start, end):
                    copy.write_row(
                        [Jsonb(row[name]) if name in json_columns else row[name] for name in names]
                    )
                    written += 1
    return written


def _state(conn: psycopg.Connection[tuple[object, ...]]) -> tuple[str | None, datetime | None]:
    row = conn.execute(
        sql.SQL("SELECT model_version, written_through FROM {} WHERE id = 1").format(
            sql.SQL(schema.STATE_TABLE)
        )
    ).fetchone()
    if row is None:
        return None, None
    written_through = row[1]
    if isinstance(written_through, datetime):
        written_through = written_through.astimezone(UTC)
    return (str(row[0]) if row[0] is not None else None), written_through  # type: ignore[return-value]


def _save_state(conn: psycopg.Connection[tuple[object, ...]], **values: object) -> None:
    assignments = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder(name)) for name in values
    )
    conn.execute(
        sql.SQL("UPDATE {} SET {} WHERE id = 1").format(sql.SQL(schema.STATE_TABLE), assignments),
        values,
    )


def _write(conn: psycopg.Connection[tuple[object, ...]], now: datetime) -> WriteRun:
    conn.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(DDL_LOCK_TIMEOUT)))
    for statement in schema.schema_ddl():
        conn.execute(statement)
    conn.execute(
        sql.SQL(
            "INSERT INTO {} (id, model_version) VALUES (1, %s) ON CONFLICT (id) DO NOTHING"
        ).format(sql.SQL(schema.STATE_TABLE)),
        (rows.MODEL_VERSION,),
    )
    model_version, written_through = _state(conn)
    rewritten = model_version != rows.MODEL_VERSION
    if rewritten:
        # Rows of another model: start over rather than mix two in one table.
        for day in _partitions(conn, "events"):
            _drop_day(conn, day)
        written_through = None
        _save_state(conn, model_version=rows.MODEL_VERSION, written_through=None)

    target = rows.available_through(now)
    keep_from = retention_start(now)
    start = written_through if written_through and written_through > keep_from else keep_from

    day = _day_floor(start)
    while day <= _day_floor(target):
        for table in schema.TABLE_COLUMNS:
            conn.execute(schema.partition_ddl(table, day))
        day += _DAY

    written = 0
    cursor = start
    while cursor < target:
        day_end = min(_day_floor(cursor) + _DAY, target)
        with conn.transaction():
            written += _copy_rows(conn, cursor, day_end)
            _save_state(conn, written_through=day_end)
        cursor = day_end

    dropped = 0
    for day in _partitions(conn, "events"):
        if day < keep_from:
            _drop_day(conn, day)
            dropped += 1

    size = _database_bytes(conn)
    cap = settings.demo_warehouse_max_mb * 1024 * 1024
    floor_day = _day_floor(now) - timedelta(days=MIN_RETAINED_DAYS)
    if size > cap:
        for day in _partitions(conn, "events"):
            if size <= cap or day >= floor_day:
                break
            _drop_day(conn, day)
            dropped += 1
            size = _database_bytes(conn)
        logger.error(
            "demo_warehouse.write over DEMO_WAREHOUSE_MAX_MB=%s: %.0f MB after dropping the "
            "oldest days beyond %s retained; raise the cap or lower the retention",
            settings.demo_warehouse_max_mb,
            size / 1024 / 1024,
            MIN_RETAINED_DAYS,
        )

    remaining = _partitions(conn, "events")
    oldest = remaining[0] if remaining else None
    _save_state(
        conn,
        oldest=oldest,
        # The wall clock, not ``now``: a caller may pass a floored clock (a
        # demo build passes its seed hour), and the status reads this as "when
        # the writer last ran".
        last_run_at=datetime.now(UTC),
        last_run_rows=written,
        last_error=None,
    )
    return WriteRun(
        rows_written=written,
        written_through=max(cursor, start),
        oldest=oldest,
        database_bytes=size,
        dropped_days=dropped,
        rewritten=rewritten,
    )


def _record_error(conn: psycopg.Connection[tuple[object, ...]], exc: Exception) -> None:
    """Best effort: leave the failure where the status surface reads it."""
    try:
        _save_state(conn, last_error=f"{type(exc).__name__}: {exc}"[:500])
    except psycopg.Error:
        logger.debug("demo_warehouse.write could not record its error", exc_info=True)


def write(now: datetime | None = None, *, wait_seconds: int | None = None) -> WriteRun | None:
    """One writer run; ``None`` when skipped (no warehouse, or another run holds the lock).

    ``wait_seconds`` makes the run WAIT up to that long for a run already in
    progress (the beat's backfill, say) instead of skipping, and then bring the
    warehouse current itself. That is what a demo build needs before it reads:
    a cold warehouse is still being backfilled, and a skipped run would let the
    build replay three weeks of empty days. Timing out skips, as a held lock
    does without it.
    """
    if settings.demo_warehouse_mode == "memory":
        return None
    current = now if now is not None else datetime.now(UTC)
    writer = connection.writer_connection()
    try:
        lock_conn = psycopg.connect(
            **connection.main_connection().kwargs(connect_timeout=CONNECT_TIMEOUT_SECONDS),  # type: ignore[arg-type]
            autocommit=True,
        )
    except psycopg.OperationalError as exc:
        logger.debug("demo_warehouse.write skipped: tripl's database not reachable (%s)", exc)
        return None
    with lock_conn:
        if not _take_lock(lock_conn, wait_seconds):
            return None
        try:
            run = _write_locked(writer, current)
        finally:
            if not lock_conn.closed:
                lock_conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    if run is None:
        return None
    logger.info(
        "demo_warehouse.write rows=%s written_through=%s oldest=%s size_mb=%.1f dropped_days=%s%s",
        run.rows_written,
        run.written_through.isoformat(),
        run.oldest.date().isoformat() if run.oldest else None,
        run.database_bytes / 1024 / 1024,
        run.dropped_days,
        " rewritten=true" if run.rewritten else "",
    )
    return run


def _take_lock(conn: psycopg.Connection[tuple[object, ...]], wait_seconds: int | None) -> bool:
    """The writer's advisory lock on ``conn`` (tripl's main database)."""
    if wait_seconds is not None:
        conn.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{wait_seconds}s")))
        try:
            conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        except psycopg.errors.LockNotAvailable:
            logger.warning(
                "demo_warehouse.write skipped: another run held the lock for over %ss",
                wait_seconds,
            )
            return False
        conn.execute("RESET lock_timeout")
        return True
    locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()
    if not locked or not locked[0]:
        logger.info("demo_warehouse.write skipped: another run holds the lock")
        return False
    return True


def _write_locked(writer: connection.WarehouseConnection, now: datetime) -> WriteRun | None:
    try:
        conn = psycopg.connect(
            **writer.kwargs(connect_timeout=CONNECT_TIMEOUT_SECONDS),  # type: ignore[arg-type]
            autocommit=True,
        )
    except psycopg.OperationalError as exc:
        logger.debug("demo_warehouse.write skipped: warehouse not reachable (%s)", exc)
        return None
    with conn:
        try:
            return _write(conn, now)
        except psycopg.errors.InsufficientPrivilege as exc:
            logger.warning("demo_warehouse.write skipped: %s", exc)
            return None
        except psycopg.errors.LockNotAvailable:
            # A reader's query held the parent table past DDL_LOCK_TIMEOUT. What
            # was committed stays; the next run carries on from there.
            logger.warning(
                "demo_warehouse.write stopped: a partition change waited over %s for a "
                "reader's query; the next run retries",
                DDL_LOCK_TIMEOUT,
            )
            return None
        except Exception as exc:
            _record_error(conn, exc)
            raise
