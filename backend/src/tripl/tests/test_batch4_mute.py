"""Batch-4: a muted monitor delivers nothing, on either delivery path.

``AlertRule.muted_until`` is read by the worker in two places —
``metrics.dispatch._prepare_alert_deliveries`` on the way in and
``alert_flush._build_digest`` when a scheduled digest is built — and only the
second of them was pinned by a test (``test_alert_digest_delivery.py``'s
"muting a monitor during the hold window"). The first one is the line
tripl-jfm3.99 added after the Mute button shipped writing a column no worker
read; deleting it today left the suite green, which is exactly how that bug
happened the first time. These cases hold the model comment on
``muted_until`` to the code it describes.

The seeding is imported from ``test_alert_digest_delivery`` rather than copied:
a second copy of the project/scan/destination/rule fixture stops matching the
first the moment any of those models gains a column.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_pending_item import AlertPendingItem
from tripl.models.alert_rule import AlertRule
from tripl.tests.test_alert_digest_delivery import _DAILY, _fire_anomaly, _seed
from tripl.worker.tasks.metrics import dispatch as metrics_dispatch


@pytest.fixture
def sync_session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'batch4_mute.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
        Base.metadata.drop_all(engine)
    finally:
        engine.dispose()


def test_a_muted_monitor_mints_no_immediate_delivery(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The Mute button on the Monitors screen has to stop messages.

    Its whole effect is this column, and for one release nothing in the worker
    read it: the UI reported the monitor muted and the alerts
    kept arriving.
    """
    with sync_session_factory() as session:
        config, _destination, rule, event_type = _seed(session, cron=None)
        _fire_anomaly(session, config, event_type, actual=200.0)
        session.get(AlertRule, rule.id).muted_until = datetime.now(UTC) + timedelta(hours=6)
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        assert delivery_ids == []
        assert session.execute(select(AlertDelivery)).scalars().all() == []
        assert session.execute(select(AlertDeliveryItem)).scalars().all() == []


def test_a_muted_monitor_buffers_nothing_for_a_later_digest(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """The mute is checked BEFORE immediate and scheduled destinations part ways.

    A muted rule on a cadence must not quietly pile its alerts into the buffer
    and ship them all at the next flush — the mute would have delayed the
    messages instead of suppressing them. ``_build_digest`` re-checking the
    mute does not cover this: the rows would be gone by the time it ran.
    """
    with sync_session_factory() as session:
        config, _destination, rule, event_type = _seed(session, cron=_DAILY)
        _fire_anomaly(session, config, event_type, actual=200.0)
        session.get(AlertRule, rule.id).muted_until = datetime.now(UTC) + timedelta(hours=6)
        session.commit()

        buffered: list[int] = []
        delivery_ids = metrics_dispatch._prepare_alert_deliveries(
            session,
            config,
            scan_job_id=None,
            buffered=buffered,
        )
        session.commit()

        assert delivery_ids == []
        assert buffered == [0]
        assert session.execute(select(AlertPendingItem)).scalars().all() == []


def test_a_lapsed_rule_mute_delivers_again(
    sync_session_factory: sessionmaker[Session],
) -> None:
    """A past ``muted_until`` is NOT a mute, and neither is the NULL default.

    The rule check is ``is not None and > now`` on purpose. The near-identical
    line in ``_reopen_closed_incidents`` reads a NULL the other way — there it
    is the indefinite inbox mute — and copying that reading onto an AlertRule
    would mute every monitor in the fleet, since NULL is what every rule ever
    created carries.
    """
    with sync_session_factory() as session:
        config, _destination, rule, event_type = _seed(session, cron=None)
        _fire_anomaly(session, config, event_type, actual=200.0)
        session.get(AlertRule, rule.id).muted_until = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

        delivery_ids = metrics_dispatch._prepare_alert_deliveries(session, config, scan_job_id=None)
        session.commit()

        assert len(delivery_ids) == 1
        assert len(session.execute(select(AlertDeliveryItem)).scalars().all()) == 1
