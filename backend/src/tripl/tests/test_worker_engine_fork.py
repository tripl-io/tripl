"""Fork-safety tests for the worker's shared sync engine.

The Celery parent may initialize the module-global sync engine before prefork
forks the worker children (``apply_startup_service_overrides`` reads the DB at
import). Forked children must drop that inherited engine so they don't share one
Postgres socket. ``dispose_engine`` performs the reset and is wired to the
``worker_process_init`` signal in ``tripl.worker.celery_app``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from celery.signals import worker_process_init
from sqlalchemy import Engine, text
from sqlalchemy import create_engine as _sa_create_engine

from tripl.config import settings
from tripl.worker import celery_app as celery_app_module  # noqa: F401  — wires the signal
from tripl.worker import db

TEST_SYNC_DATABASE_URL = "sqlite:///:memory:"


def _sqlite_safe_create_engine(url: Any, **kwargs: Any) -> Engine:
    """A ``create_engine`` shim for the tests' in-memory SQLite target.

    The production builder (``tripl.worker.db._ensure_initialized``) passes
    QueuePool kwargs (``pool_size`` / ``max_overflow``) that are valid for the real
    Postgres engine but rejected by SQLite's ``SingletonThreadPool``. Drop them for
    SQLite so the lazy rebuild under test can construct an engine — exercising the
    dispose/rebuild wiring without a real database.
    """
    if str(url).startswith("sqlite"):
        kwargs.pop("pool_size", None)
        kwargs.pop("max_overflow", None)
    return _sa_create_engine(url, **kwargs)


@pytest.fixture(autouse=True)
def _reset_sync_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Leave the shared engine uninitialized around each test, and make the lazy
    rebuild construct a SQLite-compatible engine."""
    monkeypatch.setattr(db, "create_engine", _sqlite_safe_create_engine)
    db.dispose_engine()
    yield
    db.dispose_engine()


def test_dispose_engine_resets_globals_and_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: point the lazy engine at an in-memory SQLite DB and prime it.
    monkeypatch.setattr(settings, "sync_database_url", TEST_SYNC_DATABASE_URL)
    db.SyncSessionLocal()
    assert db._engine is not None
    assert db._session_local is not None

    # Act: dispose drops the inherited engine + pool.
    db.dispose_engine()

    # Assert: globals reset, and the next call lazily rebuilds a usable engine.
    assert db._engine is None
    assert db._session_local is None
    with db.SyncSessionLocal() as session:
        assert session.execute(text("SELECT 1")).scalar() == 1
    assert db._engine is not None


def test_dispose_engine_is_noop_when_uninitialized() -> None:
    assert db._engine is None
    db.dispose_engine()  # must not raise
    assert db._engine is None
    assert db._session_local is None


def test_worker_process_init_signal_disposes_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: initialize the engine as the parent would before fork.
    monkeypatch.setattr(settings, "sync_database_url", TEST_SYNC_DATABASE_URL)
    db.SyncSessionLocal()
    assert db._engine is not None

    # Act: fire the signal Celery sends inside each freshly forked child.
    worker_process_init.send(sender=None)

    # Assert: the receiver dropped the inherited engine + pool.
    assert db._engine is None
    assert db._session_local is None
