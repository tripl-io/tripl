"""Which engine a ``demo`` data source runs on, and building it.

``DEMO_WAREHOUSE_MODE``:

* ``auto`` (default): the PostgreSQL warehouse when it is ready
  (:func:`~.status.warehouse_status`), else the in-memory engine. A reader
  connection that fails falls back to memory and is remembered for a minute,
  so a down warehouse costs one connect attempt a minute, not one per scan;
* ``postgres``: always the warehouse; a failure is the scan's error;
* ``memory``: always the in-memory engine (tests, or no warehouse wanted).

Falling back is safe because both engines serve the same rows: the model is
deterministic on the global clock, so a demo that switches engines between
two scans reads the same history either way.
"""

from __future__ import annotations

import logging
from datetime import datetime

import psycopg

from tripl.config import settings
from tripl.core.adapters.base import BaseAdapter
from tripl.core.demo_warehouse import connection, status

logger = logging.getLogger(__name__)


def build_postgres_adapter(timeout_seconds: int | None = None) -> BaseAdapter:
    """The PostgreSQL engine as the reader role. Raises when it cannot connect."""
    from tripl.core.adapters.demo_postgres import DemoPostgresAdapter

    reader = connection.reader_connection()
    if reader is None:
        msg = "The demo warehouse has no reader password (set SECRET_KEY)"
        raise RuntimeError(msg)
    return DemoPostgresAdapter(
        host=reader.host,
        port=reader.port,
        database=reader.database,
        username=reader.user,
        password=reader.password,
        timeout_seconds=timeout_seconds,
        sslmode=reader.sslmode,
    )


def build_memory_adapter(now: datetime | None = None) -> BaseAdapter:
    from tripl.core.adapters.demo_memory import DemoMemoryAdapter

    return DemoMemoryAdapter(now=now)


def build_demo_adapter(
    *, timeout_seconds: int | None = None, now: datetime | None = None
) -> BaseAdapter:
    """The adapter a ``demo`` source runs on now.

    ``now`` pins the in-memory engine's clock (tests); the PostgreSQL engine
    serves what the writer has written.
    """
    mode = settings.demo_warehouse_mode
    if mode == "memory":
        return build_memory_adapter(now)
    if mode == "postgres":
        return build_postgres_adapter(timeout_seconds)
    if status.warehouse_status().engine != "postgres":
        return build_memory_adapter(now)
    try:
        return build_postgres_adapter(timeout_seconds)
    except (psycopg.Error, RuntimeError) as exc:
        logger.warning(
            "demo_warehouse.engine the reader could not connect (%s); demos use the "
            "in-memory engine for now",
            type(exc).__name__,
        )
        status.mark_unreachable(f"the reader could not connect ({type(exc).__name__})")
        return build_memory_adapter(now)


def engine_of(adapter: BaseAdapter) -> str:
    """``postgres`` or ``memory``: which engine ``adapter`` is (``unknown`` otherwise)."""
    return str(getattr(adapter, "engine", "unknown"))


def connection_message(adapter: BaseAdapter) -> str:
    """A ``demo`` source's connection-status line: which engine serves it, and why."""
    if engine_of(adapter) == "postgres":
        return "Connected to the demo warehouse (PostgreSQL)"
    reason = status.warehouse_status().reason
    suffix = f": {reason}" if reason else ""
    return f"Demo warehouse in memory (the PostgreSQL warehouse is not in use{suffix})"
