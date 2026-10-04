"""The two digest-flush guards that only exist on PostgreSQL.

``test_alert_digest_delivery.py`` runs on SQLite, where both of these are
silent no-ops: ``SELECT ... FOR UPDATE`` emits nothing, and
``_try_acquire_advisory_lock`` returns ``(None, True)`` without asking the
database (``metrics/schedule.py``). So the guards that keep
``flush_due_alert_digests`` safe under concurrency were unproven by the suite
even though every assertion in it passed.

Skipped honestly when no PostgreSQL is reachable, so a local ``pytest`` run
stays green — and CI sets ``TRIPL_TEST_PG_REQUIRED=1``, which turns
"database unreachable" from a skip into a failure, because a gate that can
silently skip itself in CI is not a gate. Same stance as the warehouse
conformance harness (``tests/conformance/conftest.py``).

Point a local run at any empty database:

    docker run -d --name pg -e POSTGRES_USER=tripl -e POSTGRES_PASSWORD=tripl \
        -e POSTGRES_DB=tripl_digest -p 55432:5432 postgres:18
    TRIPL_TEST_PG_URL=postgresql+psycopg://tripl:tripl@localhost:55432/tripl_digest \
        uv run pytest src/tripl/tests/test_alert_digest_concurrency_pg.py
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.event_type import EventType
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.worker.tasks import alert_flush

# Module-wide, so the ordinary backend job can drop it with -m and this file
# is never the reason a local run looks noisy.
pytestmark = pytest.mark.pg_concurrency

_PG_URL = os.environ.get("TRIPL_TEST_PG_URL")
_PG_REQUIRED = os.environ.get("TRIPL_TEST_PG_REQUIRED") == "1"

# Fires every minute, so a watermark in the past is always due.
_ALWAYS_DUE = "* * * * *"


def _engine_or_skip() -> Engine:
    if not _PG_URL:
        if _PG_REQUIRED:
            pytest.fail(
                "TRIPL_TEST_PG_REQUIRED=1 but TRIPL_TEST_PG_URL is unset — the "
                "PostgreSQL concurrency gate cannot be allowed to skip in CI"
            )
        pytest.skip("TRIPL_TEST_PG_URL is not set; PostgreSQL concurrency gate skipped")
    engine = create_engine(_PG_URL, poolclass=None)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        if _PG_REQUIRED:
            pytest.fail(f"TRIPL_TEST_PG_REQUIRED=1 but PostgreSQL is unreachable: {exc}")
        pytest.skip(f"PostgreSQL unreachable ({exc}); concurrency gate skipped")
    return engine


@pytest.fixture
def pg_session_factory() -> Iterator[sessionmaker[Session]]:
    engine = _engine_or_skip()
    # Each test owns a clean schema: these assert on "how many digests exist",
    # which leftovers would quietly falsify.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed(session: Session, *, last_flushed_at: datetime) -> tuple[AlertDestination, uuid.UUID]:
    """A scheduled destination with exactly one buffered alert waiting."""
    project = Project(
        id=uuid.uuid4(),
        name="Digest PG",
        slug=f"digest-pg-{uuid.uuid4().hex[:8]}",
        description="",
        timezone="UTC",
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name=f"DS {uuid.uuid4().hex[:8]}",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Scan",
        base_query="SELECT time, event_name FROM events",
        time_column="time",
        cardinality_threshold=100,
        interval="1h",
    )
    event_type = EventType(
        id=uuid.uuid4(),
        project_id=project.id,
        name="page",
        display_name="Page",
        description="",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="slack",
        name="Daily Slack",
        enabled=True,
        webhook_url_encrypted="secret",
        delivery_schedule_cron=_ALWAYS_DUE,
        last_flushed_at=last_flushed_at,
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Everything",
        enabled=True,
        min_percent_delta=0,
        min_absolute_delta=0,
        min_expected_count=0,
    )
    # Flushed in dependency order rather than in one batch: PostgreSQL enforces
    # the foreign keys immediately, so the parents have to be in the table
    # before the children reference them.
    session.add_all([project, data_source])
    session.flush()
    session.add_all([config, event_type, destination])
    session.flush()
    session.add(rule)
    session.flush()
    item = AlertPendingItem(
        id=uuid.uuid4(),
        project_id=project.id,
        destination_id=destination.id,
        rule_id=rule.id,
        scan_config_id=config.id,
        scope_type="event_type",
        scope_ref=str(event_type.id),
        scope_name="Page",
        event_type_id=event_type.id,
        bucket=datetime.now(UTC),
        direction="spike",
        actual_count=200.0,
        expected_count=10.0,
        correlation_group_id=uuid.uuid4(),
    )
    session.add(item)
    session.commit()
    return destination, item.id


def _stub_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    sent: list[str] = []
    from tripl.worker.tasks import alerts as alerts_module

    monkeypatch.setattr(
        alerts_module.send_alert_delivery, "delay", lambda delivery_id: sent.append(delivery_id)
    )
    return sent


def test_a_second_flusher_skips_the_tick_while_the_first_holds_the_advisory_lock(
    pg_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_try_acquire_advisory_lock` is a real lock here and a no-op on SQLite.

    Without it, a redelivered beat message or a second worker would run a full
    flush pass concurrently with the first. The compare-and-set still makes
    that safe, but the lock is what stops the wasted work — and nothing proved
    it was actually being taken.
    """
    with pg_session_factory() as session:
        _seed(session, last_flushed_at=datetime.now(UTC) - timedelta(hours=2))

    monkeypatch.setattr(alert_flush, "_get_sync_session", pg_session_factory)
    sent = _stub_dispatch(monkeypatch)

    engine = pg_session_factory.kw["bind"]
    holder = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        acquired = holder.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": alert_flush._ALERT_FLUSH_ADVISORY_LOCK_KEY},
        ).scalar()
        assert acquired is True, "the test itself must hold the lock for this to mean anything"

        result = alert_flush.flush_due_alert_digests.run()

        assert result == {"checked": 0, "flushed": 0, "deliveries": 0, "swept": 0}
        assert sent == []
    finally:
        holder.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": alert_flush._ALERT_FLUSH_ADVISORY_LOCK_KEY},
        )
        holder.close()

    # ...and once the lock is free the same tick delivers normally.
    result = alert_flush.flush_due_alert_digests.run()
    assert result["flushed"] == 1
    assert len(sent) == 1


def test_the_flush_waits_for_an_in_flight_collection_instead_of_skipping_its_row(
    pg_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_build_digest` takes a plain FOR UPDATE, deliberately NOT SKIP LOCKED.

    The only transaction that can hold one of these row locks is a
    `collect_metrics` mid-upsert — and it holds it from the moment it writes
    until it commits, minutes later. SKIP LOCKED would silently drop exactly
    the scope that is firing hardest out of the digest, and the operator would
    never know it was missing. Waiting is bounded by that commit and yields the
    freshest numbers instead.

    Proven by holding the row lock, showing the flush BLOCKS rather than
    returning an empty digest, then releasing it.
    """
    with pg_session_factory() as session:
        _seed(session, last_flushed_at=datetime.now(UTC) - timedelta(hours=2))

    monkeypatch.setattr(alert_flush, "_get_sync_session", pg_session_factory)
    sent = _stub_dispatch(monkeypatch)

    finished = threading.Event()
    outcome: dict[str, object] = {}

    def flush() -> None:
        try:
            outcome["result"] = alert_flush.flush_due_alert_digests.run()
        except Exception as exc:  # noqa: BLE001  (surfaced by the assertions below)
            outcome["error"] = exc
        finally:
            finished.set()

    blocker = pg_session_factory()
    try:
        # Stand in for a collection that has written the row and not committed.
        held = blocker.execute(select(AlertPendingItem).with_for_update()).scalars().one_or_none()
        assert held is not None

        worker = threading.Thread(target=flush, daemon=True)
        worker.start()

        # The flush must be STUCK on the row lock. If it were SKIP LOCKED it
        # would sail past and finish here with an empty digest.
        assert not finished.wait(timeout=3.0), (
            "the flush did not block on the held row — SKIP LOCKED would drop this "
            "alert from the digest silently"
        )
        assert sent == [], "nothing may be dispatched while the row is still being written"
    finally:
        blocker.rollback()
        blocker.close()

    assert finished.wait(timeout=30.0), "the flush never resumed after the lock was released"
    assert "error" not in outcome, outcome.get("error")
    assert outcome["result"]["flushed"] == 1  # type: ignore[index]

    with pg_session_factory() as session:
        deliveries = session.execute(select(AlertDelivery)).scalars().all()
        assert len(deliveries) == 1
        assert deliveries[0].matched_count == 1
        # Claimed and delivered exactly once.
        assert session.execute(select(AlertPendingItem)).scalars().all() == []
    assert len(sent) == 1


def test_two_flushes_of_one_window_produce_one_digest_against_real_postgres(
    pg_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compare-and-set is the guard correctness actually rests on.

    Covered on SQLite too, but repeated here because this is the one assertion
    that must hold on the database production runs, where `last_flushed_at`
    round-trips through a real timestamptz rather than SQLite's naive text.
    """
    with pg_session_factory() as session:
        destination, _item_id = _seed(
            session, last_flushed_at=datetime.now(UTC) - timedelta(hours=2)
        )
        destination_id = destination.id

    monkeypatch.setattr(alert_flush, "_get_sync_session", pg_session_factory)
    sent = _stub_dispatch(monkeypatch)

    first = alert_flush.flush_due_alert_digests.run()
    second = alert_flush.flush_due_alert_digests.run()

    assert first["flushed"] == 1
    assert second["flushed"] == 0
    assert len(sent) == 1

    with pg_session_factory() as session:
        assert len(session.execute(select(AlertDelivery)).scalars().all()) == 1
        refreshed = session.get(AlertDestination, destination_id)
        assert refreshed is not None
        assert refreshed.last_flushed_at is not None
        assert refreshed.last_flushed_at.tzinfo is not None, (
            "last_flushed_at must come back tz-aware; every comparison against it is aware"
        )
