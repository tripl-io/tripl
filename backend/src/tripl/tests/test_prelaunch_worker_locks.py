"""Beat-task single-flight locks, and polling ticks that expire instead of piling up.

Every dispatcher takes and releases its Postgres advisory lock through
``worker.utils.advisory_lock``. ``check_metrics_due`` used to carry its own
inline copy, and the shared release logged every failure as the
metric-definition dispatcher's, whichever task's lock it was. A failure while
asking for the lock now closes the connection it opened.

Every polling beat entry expires within its interval: all tasks share one
queue, so a tick that cannot start while long collections hold the workers is
dropped rather than queued behind them, and the next tick does its work.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tripl.worker.celery_app import celery_app
from tripl.worker.tasks import alert_flush, demo_runtime
from tripl.worker.tasks.metrics import freshness_sweep
from tripl.worker.tasks.metrics import schedule as metrics_schedule
from tripl.worker.utils import advisory_lock
from tripl.worker.utils.advisory_lock import release_advisory_lock, try_acquire_advisory_lock


class _FakeLockConnection:
    """Stands in for the AUTOCOMMIT connection a Postgres lock is held on."""

    def __init__(self, *, granted: bool = True, fail: bool = False) -> None:
        self.granted = granted
        self.fail = fail
        self.closed = False
        self.statements: list[str] = []

    def execution_options(self, **_options: object) -> _FakeLockConnection:
        return self

    def execute(self, statement: object, _params: dict[str, int]) -> SimpleNamespace:
        self.statements.append(str(statement))
        if self.fail:
            raise RuntimeError("connection reset")
        return SimpleNamespace(scalar=lambda: self.granted)

    def close(self) -> None:
        self.closed = True


def _postgres_session(conn: _FakeLockConnection) -> Any:
    postgres = SimpleNamespace(name="postgresql")
    engine = SimpleNamespace(connect=lambda: conn)
    return SimpleNamespace(bind=SimpleNamespace(dialect=postgres, engine=engine))


# --------------------------------------------------------------------------- #
# The helpers
# --------------------------------------------------------------------------- #


def test_off_postgres_there_is_no_lock_to_take() -> None:
    engine = create_engine("sqlite://")
    try:
        with Session(engine) as session:
            assert try_acquire_advisory_lock(session, 7) == (None, True)
        release_advisory_lock(None, 7, name="check_metrics_due")
    finally:
        engine.dispose()


def test_a_held_lock_is_released_on_its_own_connection() -> None:
    conn = _FakeLockConnection()

    lock_conn, acquired = try_acquire_advisory_lock(_postgres_session(conn), 7)

    assert acquired and lock_conn is conn and not conn.closed
    release_advisory_lock(lock_conn, 7, name="check_metrics_due")
    assert "pg_advisory_unlock" in conn.statements[-1]
    assert conn.closed


def test_a_lock_another_run_holds_is_not_acquired() -> None:
    conn = _FakeLockConnection(granted=False)

    assert try_acquire_advisory_lock(_postgres_session(conn), 7) == (None, False)
    assert conn.closed


def test_a_failed_lock_query_closes_the_connection_it_opened() -> None:
    conn = _FakeLockConnection(fail=True)

    with pytest.raises(RuntimeError, match="connection reset"):
        try_acquire_advisory_lock(_postgres_session(conn), 7)
    assert conn.closed


def test_a_failed_release_is_logged_under_the_task_that_held_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged: list[str] = []
    monkeypatch.setattr(
        advisory_lock,
        "logger",
        SimpleNamespace(exception=lambda message, *args: logged.append(message % args)),
    )
    conn = _FakeLockConnection(fail=True)

    release_advisory_lock(conn, 4_021_968_019, name="flush_due_alert_digests")

    assert logged == ["flush_due_alert_digests: failed to release advisory lock 4021968019"]
    assert conn.closed


# --------------------------------------------------------------------------- #
# The dispatchers
# --------------------------------------------------------------------------- #


def test_every_dispatcher_locks_through_the_shared_helpers() -> None:
    for module in (metrics_schedule, alert_flush, freshness_sweep, demo_runtime):
        assert module.try_acquire_advisory_lock is advisory_lock.try_acquire_advisory_lock
        assert module.release_advisory_lock is advisory_lock.release_advisory_lock
        assert not hasattr(module, "_try_acquire_advisory_lock"), module.__name__
        assert not hasattr(module, "_release_advisory_lock"), module.__name__


def test_check_metrics_due_skips_a_tick_through_the_shared_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []
    released: list[tuple[object, int, str]] = []
    closed: list[bool] = []

    def taken(_session: object, key: int) -> tuple[None, bool]:
        asked.append(key)
        return None, False

    def release(conn: object, key: int, *, name: str) -> None:
        released.append((conn, key, name))

    monkeypatch.setattr(
        metrics_schedule,
        "_get_sync_session",
        lambda: SimpleNamespace(close=lambda: closed.append(True)),
    )
    monkeypatch.setattr(metrics_schedule, "try_acquire_advisory_lock", taken)
    monkeypatch.setattr(metrics_schedule, "release_advisory_lock", release)

    assert metrics_schedule.check_metrics_due.run() == {"checked": 0, "dispatched": 0}

    key = metrics_schedule._DISPATCH_ADVISORY_LOCK_KEY
    assert asked == [key]
    assert released == [(None, key, "check_metrics_due")]
    assert closed == [True]


# --------------------------------------------------------------------------- #
# Beat
# --------------------------------------------------------------------------- #


def _polls_around_the_clock(schedule: Any) -> bool:
    """A crontab that fires at least once every hour of every day."""
    return (
        len(schedule.hour) == 24
        and len(schedule.day_of_week) == 7
        and len(schedule.day_of_month) == 31
        and len(schedule.month_of_year) == 12
    )


def test_every_polling_beat_entry_expires_within_its_interval() -> None:
    # Community's own entries; an extension's carry their own options.
    community = {
        name: entry
        for name, entry in celery_app.conf.beat_schedule.items()
        if entry["task"].startswith("tripl.worker.")
    }
    for name, entry in community.items():
        expires = entry.get("options", {}).get("expires")
        schedule = entry["schedule"]
        if _polls_around_the_clock(schedule):
            interval = 3600 // len(schedule.minute)
            assert expires is not None and 0 < expires <= interval, name
        else:
            # A late daily or weekly run beats a skipped one.
            assert expires is None, name

    assert community["flush-due-alert-digests"]["options"] == {"expires": 60}
    assert community["send-instant-notification-emails"]["options"] == {"expires": 60}
    assert community["check-metrics-due"]["options"] == {"expires": 300}
