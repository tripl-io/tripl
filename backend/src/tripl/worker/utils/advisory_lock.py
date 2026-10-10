"""Postgres advisory locks that keep a beat task single-flight across workers.

A dispatcher takes its lock for the length of a run, so a redelivered or
overlapping beat message finds it taken and skips the tick instead of doing the
same work twice. Each task has its own key, so no two of them contend.

The lock is held on its own AUTOCOMMIT connection, apart from the task's
session, so the session's commits and rollbacks never release it. On any other
backend (SQLite in tests) there is no lock: acquiring always succeeds and
releasing does nothing.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def try_acquire_advisory_lock(session: Session, key: int) -> tuple[Connection | None, bool]:
    """Try to take advisory lock ``key`` without waiting.

    Returns ``(lock_conn, acquired)``. ``lock_conn`` holds the lock and goes to
    :func:`release_advisory_lock`; it is None when the lock was not taken or the
    backend has no advisory locks. If asking for the lock fails, the connection
    is closed before the error propagates.
    """
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return None, True
    engine = bind if isinstance(bind, Engine) else bind.engine
    lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquired = bool(
            lock_conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        )
    except BaseException:
        lock_conn.close()
        raise
    if not acquired:
        lock_conn.close()
        return None, False
    return lock_conn, True


def release_advisory_lock(lock_conn: Connection | None, key: int, *, name: str) -> None:
    """Release advisory lock ``key`` and close its connection, best effort.

    ``name`` is the task that held the lock. A failed release is logged under
    that name rather than raised, so cleanup never replaces the task's result.
    """
    if lock_conn is None:
        return
    try:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
    except Exception:  # pragma: no cover - best-effort lock release
        logger.exception("%s: failed to release advisory lock %s", name, key)
    finally:
        lock_conn.close()
