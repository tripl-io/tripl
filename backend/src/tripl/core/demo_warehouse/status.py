"""The warehouse's state as the instance owner sees it, and which engine serves demos.

:func:`warehouse_status` reads the writer's bookkeeping with the writer's own
credentials (never the reader's: the bookkeeping is hidden from it), with a
short connect timeout, and caches the answer per process for
:data:`CACHE_SECONDS`, so asking on every adapter build or settings page costs
one query a minute at most.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import psycopg
from psycopg import sql

from tripl.config import settings
from tripl.core.demo_warehouse import connection, rows, schema

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60.0
CONNECT_TIMEOUT_SECONDS = 3
#: A demo replays this much history, so the warehouse must hold it to serve one.
READY_HISTORY = timedelta(days=23)
#: How far behind ``written_through`` may fall before demos stop reading it.
STALE_AFTER = timedelta(hours=1)

Engine = Literal["postgres", "memory"]


@dataclass(frozen=True)
class WarehouseStatus:
    """The demo warehouse, for the system-status surface and engine selection."""

    #: ``DEMO_WAREHOUSE_MODE``: ``auto``, ``postgres`` or ``memory``.
    mode: str
    #: The engine a demo source built now runs on.
    engine: Engine
    #: The warehouse database answered and holds the schema.
    reachable: bool
    #: A reader password is available (SECRET_KEY or DEMO_WAREHOUSE_READER_PASSWORD).
    reader_configured: bool
    database: str
    #: Rows exist up to (excluding) this instant.
    written_through: datetime | None
    #: The oldest UTC day retained.
    oldest: datetime | None
    database_bytes: int | None
    retention_days: int
    max_mb: int
    model_version: str | None
    last_run_at: datetime | None
    last_error: str | None
    #: Why demos run in memory, in a sentence; ``None`` on the PostgreSQL engine.
    reason: str | None
    #: Reader-role limits ``migrate`` could not set (``temp_file_limit`` needs a
    #: superuser): the reader runs without them, on the server tripl shares.
    #: Then every function or view it should have taken from the reader and
    #: could not (a signature such as ``set_config(text,text,boolean)``, or
    #: ``pg_catalog.pg_stat_activity``); only the adapter's SQL check refuses
    #: those.
    reader_limits_missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_lock = threading.Lock()
_cached: tuple[float, WarehouseStatus] | None = None


def _read(now: datetime) -> WarehouseStatus:
    mode = settings.demo_warehouse_mode
    writer = connection.writer_connection()
    reader_configured = connection.reader_password() is not None
    base: dict[str, object] = {
        "mode": mode,
        "reader_configured": reader_configured,
        "database": writer.database,
        "retention_days": settings.demo_warehouse_retention_days,
        "max_mb": settings.demo_warehouse_max_mb,
        "written_through": None,
        "oldest": None,
        "database_bytes": None,
        "model_version": None,
        "last_run_at": None,
        "last_error": None,
    }
    if mode == "memory":
        return WarehouseStatus(
            engine="memory",
            reachable=False,
            reason="DEMO_WAREHOUSE_MODE=memory",
            **base,  # type: ignore[arg-type]
        )
    try:
        with psycopg.connect(
            **writer.kwargs(connect_timeout=CONNECT_TIMEOUT_SECONDS),  # type: ignore[arg-type]
            autocommit=True,
        ) as conn:
            row = conn.execute(
                sql.SQL(
                    "SELECT model_version, written_through, oldest, last_run_at, last_error, "
                    "pg_database_size(current_database()) FROM {} WHERE id = 1"
                ).format(sql.SQL(schema.STATE_TABLE))
            ).fetchone()
            missing = _missing_reader_limits(conn)
    except psycopg.Error as exc:
        return WarehouseStatus(
            engine="memory",
            reachable=False,
            reason=f"the warehouse database is not reachable ({type(exc).__name__})",
            **base,  # type: ignore[arg-type]
        )
    if row is None:
        return WarehouseStatus(
            engine="memory",
            reachable=True,
            reason="the writer has not run yet",
            **base,  # type: ignore[arg-type]
        )
    model_version, written_through, oldest, last_run_at, last_error, size = row
    base.update(
        model_version=model_version,
        written_through=written_through,
        oldest=oldest,
        last_run_at=last_run_at,
        last_error=last_error,
        database_bytes=int(size) if size is not None else None,
    )
    reason = _not_ready_reason(
        now,
        reader_configured=reader_configured,
        model_version=model_version,
        written_through=written_through,
        oldest=oldest,
    )
    engine: Engine = "postgres" if reason is None or mode == "postgres" else "memory"
    return WarehouseStatus(
        engine=engine,
        reachable=True,
        reason=reason if engine == "memory" else None,
        reader_limits_missing=missing,
        **base,  # type: ignore[arg-type]
    )


def _missing_reader_limits(conn: psycopg.Connection[tuple[object, ...]]) -> tuple[str, ...]:
    """The reader limits ``migrate`` sets that the reader does not have.

    The role defaults only when ``migrate`` manages the role (a hand-provisioned
    reader is the operator's to configure); the revoked functions and views
    always, since ``migrate`` takes those in the demo database either way.
    """
    from tripl.core.demo_warehouse.provision import (
        READER_ROLE_SETTINGS,
        reader_reachable_session_functions,
    )

    reader = connection.reader_role()
    missing: tuple[str, ...] = ()
    if connection.reader_is_managed():
        row = conn.execute(
            "SELECT s.setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
            "WHERE r.rolname = %s AND s.setdatabase = 0",
            (reader,),
        ).fetchone()
        setconfig = row[0] if row and isinstance(row[0], list) else []
        configured = {str(item).split("=", 1)[0] for item in setconfig}
        missing = tuple(name for name, _ in READER_ROLE_SETTINGS if name not in configured)
    return missing + reader_reachable_session_functions(conn, reader)


def _not_ready_reason(
    now: datetime,
    *,
    reader_configured: bool,
    model_version: str | None,
    written_through: datetime | None,
    oldest: datetime | None,
) -> str | None:
    if not reader_configured:
        return "no reader password (set SECRET_KEY or DEMO_WAREHOUSE_READER_PASSWORD)"
    if model_version != rows.MODEL_VERSION:
        return "the writer has not yet rewritten the warehouse for this version"
    if written_through is None or written_through < now - STALE_AFTER:
        return "the writer is behind (is the worker's beat running?)"
    if oldest is None or oldest > now - READY_HISTORY:
        return "the writer is still backfilling history"
    return None


def warehouse_status(*, refresh: bool = False) -> WarehouseStatus:
    """The warehouse's status, cached per process for :data:`CACHE_SECONDS`."""
    global _cached
    with _lock:
        if not refresh and _cached is not None and time.monotonic() - _cached[0] < CACHE_SECONDS:
            return _cached[1]
    status = _read(datetime.now(UTC))
    with _lock:
        _cached = (time.monotonic(), status)
    return status


_refreshing = threading.Event()


def peek_engine() -> Engine | None:
    """The cached engine, without I/O; a cold or stale cache answers ``None``.

    For a request path (the data source list): it starts one background read,
    so the next answer has the engine.
    """
    with _lock:
        cached = _cached
    if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1].engine
    if settings.demo_warehouse_mode == "memory":
        return "memory"
    if not _refreshing.is_set():
        _refreshing.set()
        threading.Thread(target=_refresh_in_background, daemon=True).start()
    return cached[1].engine if cached is not None else None


def _refresh_in_background() -> None:
    try:
        warehouse_status(refresh=True)
    except Exception:
        logger.debug("demo_warehouse.status background read failed", exc_info=True)
    finally:
        _refreshing.clear()


def mark_unreachable(reason: str) -> None:
    """Remember for :data:`CACHE_SECONDS` that the reader could not connect."""
    global _cached
    with _lock:
        previous = _cached[1] if _cached is not None else warehouse_status_unknown()
        _cached = (
            time.monotonic(),
            WarehouseStatus(
                **{**previous.as_dict(), "engine": "memory", "reason": reason}  # type: ignore[arg-type]
            ),
        )


def warehouse_status_unknown() -> WarehouseStatus:
    writer = connection.writer_connection()
    return WarehouseStatus(
        mode=settings.demo_warehouse_mode,
        engine="memory",
        reachable=False,
        reader_configured=connection.reader_password() is not None,
        database=writer.database,
        written_through=None,
        oldest=None,
        database_bytes=None,
        retention_days=settings.demo_warehouse_retention_days,
        max_mb=settings.demo_warehouse_max_mb,
        model_version=None,
        last_run_at=None,
        last_error=None,
        reason="not checked yet",
    )


def clear_cache() -> None:
    global _cached
    with _lock:
        _cached = None
