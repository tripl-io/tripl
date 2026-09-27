"""Shared sync SQLAlchemy engine + session factory for Celery tasks.

Each worker process creates ONE engine on first use (lazy), then every task
checks out a session from that engine's connection pool. This avoids the
overhead of spinning up a fresh engine per task invocation.

Tests do not touch this module directly — they monkey-patch the
``_get_sync_session`` name inside each task module's globals.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from tripl.config import settings
from tripl.db_config import (
    MAX_OVERFLOW,
    POOL_PRE_PING,
    POOL_RECYCLE_SECONDS,
    POOL_SIZE,
    postgres_connect_args,
)

if TYPE_CHECKING:
    from tripl.core.adapters.base import BaseAdapter
    from tripl.models.data_source import DataSource

_engine: Engine | None = None
_session_local: sessionmaker[Session] | None = None


def _ensure_initialized() -> sessionmaker[Session]:
    global _engine, _session_local
    if _session_local is None:
        _engine = create_engine(
            settings.sync_database_url,
            connect_args=postgres_connect_args(settings.sync_database_url),
            echo=settings.debug,
            pool_size=POOL_SIZE,
            max_overflow=MAX_OVERFLOW,
            pool_pre_ping=POOL_PRE_PING,
            pool_recycle=POOL_RECYCLE_SECONDS,
        )
        _session_local = sessionmaker(_engine, expire_on_commit=False)
    return _session_local


def dispose_engine() -> None:
    """Drop the inherited engine + pool so the current process rebuilds its own.

    Fork-safety (canonical SQLAlchemy + Celery prefork pattern): the Celery
    parent (MainProcess) may initialize ``_engine`` before prefork forks the
    worker children (e.g. a startup read through ``SyncSessionLocal``). Forked
    children inherit that engine and its live TCP socket, so tasks running in
    different children would interleave protocol traffic on ONE shared Postgres
    connection and read each other's results. Calling this in each child
    (``worker_process_init``) discards the inherited pool; the next
    ``SyncSessionLocal`` then lazily rebuilds a fresh engine+pool owned by the
    current process.

    ``dispose(close=False)`` releases the pool's references WITHOUT closing the
    inherited fd — the parent still owns that socket, so we must not close it
    from a child. A no-op when the engine was never initialized.
    """
    global _engine, _session_local
    if _engine is not None:
        _engine.dispose(close=False)
    _engine = None
    _session_local = None


def SyncSessionLocal() -> Session:  # noqa: N802  — emulates sessionmaker() call shape
    """Return a new sync Session from the shared engine pool."""
    return _ensure_initialized()()


def _get_sync_session() -> Session:
    """Canonical sync-session factory shared by every Celery task module.

    Task modules import this under the same ``_get_sync_session`` name so tests
    can keep monkey-patching it via each module's globals.
    """
    return SyncSessionLocal()


def _build_adapter(ds: DataSource) -> BaseAdapter:
    """Canonical warehouse-adapter builder shared by Celery task modules."""
    from tripl.core.adapters.registry import build_adapter

    return build_adapter(ds)


async def run_with_async_worker_session(run: Callable[[AsyncSession], Awaitable[None]]) -> None:
    """Async bridge for a Celery task that reuses async services.

    Builds a THROWAWAY ``NullPool`` async engine that lives and dies inside the
    current event loop and is disposed before it closes: asyncpg binds each
    connection to the loop that opened it, so a pooled module-global engine
    cannot be reused across the fresh ``asyncio.run`` loop each task invocation
    creates. Moved here from ``worker.tasks.implementation_tickets`` so the
    health snapshot task (F15) can share it.
    """
    engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args=postgres_connect_args(settings.database_url),
    )
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            await run(session)
    finally:
        await engine.dispose()
