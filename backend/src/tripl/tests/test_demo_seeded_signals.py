"""The demo's alert deliveries, sent by the real worker: the flaky sink's story.

A provisioned demo files its alert deliveries ``pending`` (the replay's
dispatcher writes them as a live collection would) and queues them once the
demo is visible. Sent by the real ``send_alert_delivery``, every one of them
goes out through a local demo sink — except the first attempt at the flaky
twin (``Local demo sink (flaky)``), which fails on purpose, so the failed state
and the **Retry** action the docs promise (website/docs/use/alerting.md) are
reachable without leaving a zero-egress demo.

What is pinned: the failed attempt owns the items its header counts, each with
the incident handle the Inbox card lists an incident's deliveries by (an item
without one would hide the Retry), on a signal the main sink's own rule sent;
and the Retry re-renders a message that names every scope it counts and is
still local. ``send_alert_delivery`` re-renders from
``delivery.items`` and overwrites the stored ``rendered_message``, so the retry
is the moment an item list that disagrees with its header becomes visible.

The worker body is sync and the provisioning async: they cannot share
``conftest``'s single pooled in-memory connection, so the demo is provisioned
into a file-backed sqlite that both halves open in turn, under the suite's
outbound-network tripwire.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.config import settings
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.project import Project
from tripl.services.demo import noise
from tripl.services.demo.builders.alerts import DEMO_SINK_NAME, FLAKY_SINK_NAME
from tripl.services.demo_service import create_demo_project
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.worker.tasks import alerts as alerts_task

# The first line of the default plain template: "[tripl] ${matched_count} alerts".
_HEADER_COUNT = re.compile(r"^\[tripl\] (\d+) alerts", re.MULTILINE)


@pytest.fixture
async def demo_file_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[sessionmaker[Session], str]]:
    """A real demo provisioned into a sqlite file, and a sync factory over it.

    The full history: the volume incident the flaky sink's rule alerts on is
    detected at the head of the scoring pass only with the trailing baseline the
    full story gives it (on the suite's two-day default, the sole delivery is
    the field-contract one, which only the main sink's rule takes).
    """
    monkeypatch.setattr(settings, "demo_history_days", noise.DEMO_HISTORY_DAYS)
    db_path = tmp_path / "demo_deliveries.db"
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    enable_sqlite_foreign_keys(async_engine.sync_engine)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_sessionmaker(async_engine, expire_on_commit=False)() as session:
            created = await create_demo_project(session)
            slug = created.slug
    finally:
        # Fully released before the sync engine opens the same file.
        await async_engine.dispose()

    engine = create_engine(f"sqlite:///{db_path}")
    enable_sqlite_foreign_keys(engine)
    yield sessionmaker(engine, expire_on_commit=False), slug
    Base.metadata.drop_all(engine)
    engine.dispose()


def _deliveries(session: Session, slug: str) -> list[tuple[AlertDelivery, str]]:
    project_id = session.scalar(select(Project.id).where(Project.slug == slug))
    assert project_id is not None
    rows = session.execute(
        select(AlertDelivery, AlertDestination.name)
        .join(AlertDestination, AlertDestination.id == AlertDelivery.destination_id)
        .where(AlertDelivery.project_id == project_id)
    ).all()
    return [(delivery, name) for delivery, name in rows]


@pytest.mark.asyncio
async def test_the_flaky_sink_fails_once_inside_an_incident_and_its_retry_lists_every_scope(
    demo_file_db: tuple[sessionmaker[Session], str],
    demo_enqueued_deliveries: list[str],
    monkeypatch: pytest.MonkeyPatch,
    deny_network: None,
) -> None:
    factory, slug = demo_file_db
    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__, "_get_sync_session", factory
    )

    with factory() as session:
        filed = _deliveries(session, slug)
        # Filed as a live collection files them, and every one handed to the
        # broker once the demo was visible; all of them through a local sink.
        assert filed
        assert {delivery.status for delivery, _name in filed} == {AlertDeliveryStatus.pending.value}
        assert {str(delivery.id) for delivery, _name in filed} == set(demo_enqueued_deliveries)
        assert {delivery.channel for delivery, _name in filed} == {
            AlertDestinationType.demo_sink.value
        }
        by_sink = {name for _delivery, name in filed}
        assert {DEMO_SINK_NAME, FLAKY_SINK_NAME} <= by_sink, by_sink
        flaky_ids = [str(delivery.id) for delivery, name in filed if name == FLAKY_SINK_NAME]

    results = {
        delivery_id: alerts_task.send_alert_delivery.run(delivery_id)["status"]
        for delivery_id in demo_enqueued_deliveries
    }
    assert {results[delivery_id] for delivery_id in flaky_ids} == {"failed"}
    assert {status for delivery_id, status in results.items() if delivery_id not in flaky_ids} == {
        "sent"
    }

    with factory() as session:
        failed = session.get(AlertDelivery, uuid.UUID(flaky_ids[0]))
        assert failed is not None
        assert failed.status == AlertDeliveryStatus.failed.value
        assert failed.error_message == alerts_task.SIMULATED_FAILURE_MESSAGE
        # The failed attempt owns one item per signal its header counts...
        assert len(failed.items) == failed.matched_count > 0
        scopes = sorted({item.scope_name for item in failed.items})
        # ...every item names its incident (the Inbox card lists an incident's
        # deliveries by that handle, so an item without one hides the Retry)...
        assert None not in {item.correlation_group_id for item in failed.items}
        # ...and the main sink's rule caught the same signal and sent it. Not in
        # the same incident: a handle is one rule's, one scope's and one
        # direction's (``dispatch._correlation_group_id``), and each sink has a
        # rule of its own.
        failed_signals = {
            (item.scope_type, item.scope_ref, item.bucket, item.direction) for item in failed.items
        }
        sent_signals = {
            (item.scope_type, item.scope_ref, item.bucket, item.direction)
            for item in session.scalars(
                select(AlertDeliveryItem)
                .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
                .where(
                    AlertDelivery.project_id == failed.project_id,
                    AlertDelivery.status == AlertDeliveryStatus.sent.value,
                )
            ).all()
        }
        assert failed_signals & sent_signals, (failed_signals, sent_signals)
        claimed = failed.matched_count

        # Exactly what ``_alerting_deliveries.retry_delivery`` does to the row
        # before handing it to the worker: pending, no stale error, and a clean
        # attempt budget for the reaper. Done here rather than through the
        # service so the async and sync halves never hold the file at once.
        failed.status = AlertDeliveryStatus.pending.value
        failed.error_message = None
        failed.sent_at = None
        failed.dispatch_attempts = 0
        session.commit()

    assert alerts_task.send_alert_delivery.run(flaky_ids[0])["status"] == "sent"

    with factory() as session:
        retried = session.get(AlertDelivery, uuid.UUID(flaky_ids[0]))
        assert retried is not None
        assert retried.status == AlertDeliveryStatus.sent.value
        assert retried.error_message is None
        assert retried.payload_snapshot is not None
        # Still local and still simulated: a demo retry must not start claiming
        # an external send just because it now has something to say.
        assert retried.payload_snapshot["is_local"] is True
        text = str(retried.payload_snapshot["rendered_message"])

    header = _HEADER_COUNT.search(text)
    assert header is not None, f"unexpected rendered message shape: {text!r}"
    assert int(header.group(1)) == claimed
    # The default plain template separates the header block from ``items_text``
    # with one blank line, so everything after it is the list the header counts.
    _summary, separator, body = text.partition("\n\n")
    assert separator, "the rendered message has no item section at all"
    missing = [name for name in scopes if name not in body]
    assert not missing, f"the retry's header claims {claimed} signals but never names {missing}"
