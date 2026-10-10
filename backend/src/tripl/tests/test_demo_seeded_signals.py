"""What the seeded demo claims about its own signals.

Two seeder defects, both on the surface a new operator reads first.

The seeded failed delivery must survive its own Retry.
The local demo sink cannot fail on its own, so the demo
deliberately seeds one FAILED delivery — otherwise the failed-delivery state and
the **Retry** action the docs promise (website/docs/use/alerting.md) are
unreachable without leaving a zero-egress demo.

Retry re-dispatches that row through the real ``send_alert_delivery``, which
re-renders the message from ``delivery.items`` and OVERWRITES
``payload_snapshot["rendered_message"]`` with the result. While the seeded
failed row owned no items, the one retry the demo exists to demonstrate replaced
the seeded message with a header counting ``matched_count`` signals above an
empty list, and left the row reading "sent" with nothing in it.

The retry test runs the REAL worker body rather than stubbing ``.delay`` —
which is precisely what let this ship past
``test_demo_project.py::test_demo_seeds_a_retryable_failed_delivery`` — against
a file-backed sqlite copy of a genuinely seeded demo, under the suite's
outbound-network tripwire. A file is needed because the seeding half is async
and the worker body is sync: they cannot share ``conftest``'s single pooled
in-memory connection.

The "Injected demo spike" chart marker must name the bucket
the spike was injected into. The warehouse builder writes the spike into the
newest stored hour, but the marker was dated ``ctx.now``: the still-open hour,
one hourly bucket further on, which no series has a point for. The marker
therefore explained a bucket the chart does not plot — snapped onto the dashed
forecast point on the default hourly view, and onto an ordinary hour right after
the spike once the demo runtime had appended that hour for real.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_destination import AlertDestinationType
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services import alerting_service
from tripl.services.demo.builders.warehouse import SPIKE_EVENT_NAME
from tripl.services.demo_service import create_demo_project
from tripl.tests._sqlite import enable_sqlite_foreign_keys
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts as alerts_task

# The first line of the default plain template: "[tripl] ${matched_count} alerts".
_HEADER_COUNT = re.compile(r"^\[tripl\] (\d+) alerts", re.MULTILINE)

# The label the alerts builder writes on the marker that explains the injected
# spike. Selected on by label rather than as "the project's only annotation", so
# a recipe that later seeds a second marker does not silently retarget the test.
_SPIKE_ANNOTATION_LABEL = "Injected demo spike"

# How much taller the injected spike must be than the same hour one week earlier
# for the test to accept that the marked bucket really carries it. ``noise``
# textures a series off (seed, weekday, hour) and never off the week, so that
# earlier bucket is this one's spike-free twin up to the slow upward drift; the
# real ratio is ``DEMO_SPIKE_MULTIPLIER`` (3.0x, measured 3.02-3.06 across a year
# of seed clocks) and an ordinary hour would score ~1.0. The floor is deliberately
# loose: this asserts "a spike is here", not the seeder's arithmetic.
_SPIKE_RATIO_FLOOR = 2.0


def _naive(value: datetime) -> datetime:
    """Drop tzinfo so stored and freshly-built buckets compare.

    sqlite returns naive datetimes for ``DateTime(timezone=True)`` columns while
    rows that never round-tripped stay tz-aware; the demo seeds in UTC throughout.
    """
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


@pytest.mark.asyncio
async def test_the_seeded_failed_delivery_owns_the_incidents_items() -> None:
    """The failed demo delivery carries one item per signal its header counts.

    Two separate claims, both of which the demo makes on screen:

    * ``matched_count`` is what the rendered header announces, so a row whose
      item list is shorter than it is a row that cannot render truthfully; and
    * the failed attempt belongs to the SAME incident as the delivery that
      succeeded. That is not cosmetic — the Inbox card's "show what was sent"
      list asks the API for deliveries having an item in the group
      (``list_deliveries(correlation_group_id=...)``, which the frontend's
      ``IncidentDeliveries`` calls), so items with a NULL group would hide the
      failed row, and its Retry, from the incident it is an attempt at.
    """
    async with TestSessionLocal() as session:
        created = await create_demo_project(session)
        slug = created.slug
        project = await session.scalar(select(Project).where(Project.slug == slug))
        assert project is not None

        deliveries = (
            (
                await session.execute(
                    select(AlertDelivery).where(
                        AlertDelivery.project_id == project.id,
                        AlertDelivery.channel == AlertDestinationType.demo_sink.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        sent = [row for row in deliveries if row.status == AlertDeliveryStatus.sent.value]
        failed = [row for row in deliveries if row.status == AlertDeliveryStatus.failed.value]
        assert len(sent) == 1
        assert len(failed) == 1

        # Read off the delivery that succeeded: that row has always carried its
        # items, so it is a premise of this test rather than part of its claim.
        incident_scopes = sorted(item.scope_name for item in sent[0].items)
        assert len(incident_scopes) >= 2, (
            "fixture guard: the seeded incident must span several scopes, or an "
            "empty item list would be indistinguishable from a correct one"
        )

        assert sorted(item.scope_name for item in failed[0].items) == incident_scopes
        assert len(failed[0].items) == failed[0].matched_count

        groups = {item.correlation_group_id for item in failed[0].items}
        assert None not in groups
        assert groups == {item.correlation_group_id for item in sent[0].items}
        assert len(groups) == 1

        incident = await alerting_service.list_deliveries(
            session, slug, correlation_group_id=next(iter(groups))
        )
        assert {row.status for row in incident.items} == {
            AlertDeliveryStatus.sent,
            AlertDeliveryStatus.failed,
        }


@pytest.mark.asyncio
async def test_retrying_the_demo_failed_delivery_lists_every_scope_it_counts(
    tmp_path,
    monkeypatch,
    deny_network: None,
) -> None:
    """Retrying the seeded failed delivery re-renders a message that still lists.

    The regression this pins: ``send_alert_delivery`` re-renders from
    ``delivery.items`` and overwrites the stored ``rendered_message``, so the
    retry is the moment a seeded row's missing items become visible — as a
    delivery marked ``sent`` whose header counts N signals over nothing.

    The header count is asserted to be the premise (it is ``matched_count``, and
    it reads the same either way); the claim is that every scope the incident is
    about is NAMED in the body underneath it. Revert the builder's second item
    copy and the body renders empty, so the ``missing`` assertion below fails
    with the scope names that vanished.
    """
    db_path = tmp_path / "demo_retry.db"

    # Seed a REAL demo, the async way the API provisions one.
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    enable_sqlite_foreign_keys(async_engine.sync_engine)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_local = async_sessionmaker(async_engine, expire_on_commit=False)
    try:
        async with session_local() as session:
            created = await create_demo_project(session)
            slug = created.slug
    finally:
        # Fully released before the sync engine opens the same file.
        await async_engine.dispose()

    engine = create_engine(f"sqlite:///{db_path}")
    enable_sqlite_foreign_keys(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    with factory() as session:
        project = session.scalar(select(Project).where(Project.slug == slug))
        assert project is not None
        deliveries = (
            session.execute(
                select(AlertDelivery).where(
                    AlertDelivery.project_id == project.id,
                    AlertDelivery.channel == AlertDestinationType.demo_sink.value,
                )
            )
            .scalars()
            .all()
        )
        sent = [row for row in deliveries if row.status == AlertDeliveryStatus.sent.value]
        failed = [row for row in deliveries if row.status == AlertDeliveryStatus.failed.value]
        assert len(sent) == 1
        assert len(failed) == 1

        incident_scopes = sorted({item.scope_name for item in sent[0].items})
        assert len(incident_scopes) >= 2, "fixture guard: the incident must span several scopes"

        delivery_id = str(failed[0].id)
        claimed = failed[0].matched_count

        # Exactly what ``_alerting_deliveries.retry_delivery`` does to the row
        # before handing it to the worker: pending, no stale error, and a clean
        # attempt budget for the reaper. Done here rather than through the
        # service so the async and sync halves never hold the file at once.
        failed[0].status = AlertDeliveryStatus.pending.value
        failed[0].error_message = None
        failed[0].sent_at = None
        failed[0].dispatch_attempts = 0
        session.commit()

    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        factory,
    )

    result = alerts_task.send_alert_delivery.run(delivery_id)
    assert result["status"] == "sent"

    with factory() as session:
        retried = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert retried is not None
        assert retried.status == AlertDeliveryStatus.sent.value
        assert retried.payload_snapshot is not None
        # Still local and still simulated: a demo retry must not start claiming
        # an external send just because it now has something to say.
        assert retried.payload_snapshot["is_local"] is True
        text = retried.payload_snapshot["rendered_message"]

    header = _HEADER_COUNT.search(text)
    assert header is not None, f"unexpected rendered message shape: {text!r}"
    assert int(header.group(1)) == claimed

    # The default plain template separates the header block from ``items_text``
    # with one blank line, so everything after it is the list the header counts.
    _summary, separator, body = text.partition("\n\n")
    assert separator, "the rendered message has no item section at all"
    missing = [name for name in incident_scopes if name not in body]
    assert not missing, (
        f"the retried delivery's header claims {claimed} signals but its body never names {missing}"
    )

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_the_spike_marker_names_the_bucket_the_spike_was_injected_into() -> None:
    """The "Injected demo spike" marker sits on a bucket the chart actually plots.

    The seeded spike is written into the newest hourly bucket, which is one hour
    before the demo's clock — the hourly grid stops before the still-open hour,
    so no series ever has a point at ``now``. A marker dated ``now`` therefore
    falls off the end of the series it explains. The chart snaps an annotation to
    the nearest bucket it renders, and on the default hourly view the nearest one
    to ``now`` is the dashed one-step forecast point the chart appends past the
    last real bucket; where no forecast is drawn the marker is simply dropped,
    being past the last point. Once the demo runtime has appended the ``now`` hour
    for real, the marker labels an ordinary bucket right after the spike.

    Three independent claims, so a wrong bucket cannot pass by coincidence: the
    marked bucket is one the series stores, it is the bucket carrying the
    injected spike, and it is the bucket the seeded event-scope anomaly — the
    signal the demo's alert is about — was detected in. Revert
    ``ChartAnnotation(bucket=ctx.spike_bucket or ctx.now)`` in
    ``builders/alerts.py`` to ``bucket=ctx.now`` and all three go red, starting
    with the "no point for" assertion.
    """
    async with TestSessionLocal() as session:
        created = await create_demo_project(session)
        project = await session.scalar(select(Project).where(Project.slug == created.slug))
        assert project is not None

        scan_config_id = await session.scalar(
            select(ScanConfig.id).where(ScanConfig.project_id == project.id)
        )
        spike_event_id = await session.scalar(
            select(Event.id)
            .join(PlanBranch, Event.branch_id == PlanBranch.id)
            .where(
                Event.project_id == project.id,
                Event.name == SPIKE_EVENT_NAME,
                PlanBranch.kind == BranchKind.main,
            )
        )
        assert scan_config_id is not None
        assert spike_event_id is not None, f"fixture guard: the demo seeds no {SPIKE_EVENT_NAME}"

        # The series the marker is overlaid on: the spiked event's own per-event
        # rows (the per-type aggregates carry a NULL event_id).
        series = {
            _naive(bucket): count
            for bucket, count in (
                await session.execute(
                    select(EventMetric.bucket, EventMetric.count).where(
                        EventMetric.scan_config_id == scan_config_id,
                        EventMetric.event_id == spike_event_id,
                    )
                )
            ).all()
        }
        annotations = [
            row
            for row in (
                (
                    await session.execute(
                        select(ChartAnnotation).where(ChartAnnotation.project_id == project.id)
                    )
                )
                .scalars()
                .all()
            )
            if row.label == _SPIKE_ANNOTATION_LABEL
        ]
        anomaly_buckets = {
            _naive(bucket)
            for bucket in (
                (
                    await session.execute(
                        select(MetricAnomaly.bucket).where(
                            MetricAnomaly.scan_config_id == scan_config_id,
                            MetricAnomaly.event_id == spike_event_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        }

    assert len(annotations) == 1
    marker = _naive(annotations[0].bucket)
    # Premise, not a claim: the marker is event-scoped, so the chart it has to
    # line up with is exactly the series read above.
    assert annotations[0].scope_ref == str(spike_event_id)

    assert len(series) > 24 * 7, "fixture guard: the demo must seed a real hourly history"
    newest = max(series)

    assert marker in series, (
        f"the spike marker is dated {marker}, a bucket the series has no point for "
        f"(its newest stored bucket is {newest}) — the chart can only snap such a "
        f"marker onto the forecast point, or drop it"
    )
    assert marker == newest, (
        f"the spike is injected into the newest stored bucket ({newest}) but the "
        f"marker names {marker}"
    )

    week_earlier = marker - timedelta(days=7)
    assert week_earlier in series, "fixture guard: the history must span over a week"
    assert series[marker] >= _SPIKE_RATIO_FLOOR * series[week_earlier], (
        f"the marked bucket holds {series[marker]} against {series[week_earlier]} for the "
        f"same hour a week earlier, so it is not the bucket the spike was injected into"
    )

    assert anomaly_buckets, "fixture guard: the demo must seed an event-scope anomaly"
    assert marker in anomaly_buckets, (
        f"the marker names {marker} but the seeded anomaly the demo's alert is about "
        f"was detected in {sorted(anomaly_buckets)}"
    )
