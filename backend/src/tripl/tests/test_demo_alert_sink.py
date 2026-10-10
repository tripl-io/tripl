"""Zero-outbound-network guarantees for the demo ``demo_sink`` alert path.

The single invariant this suite protects: a demo project's alerting — create,
tick/dispatch, simulate and retry — must never make an outbound network call.
A hard guard replaces ``socket.socket`` (and, defensively, ``httpx`` sends and
``smtplib.SMTP``) with a tripwire that raises on any use, so if a demo alert path
ever tried to send, the exercised call would fail loudly instead of silently
"succeeding".
"""

from __future__ import annotations

import smtplib
import socket
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from tripl.crypto import encrypt_value
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination, AlertDestinationType
from tripl.models.alert_rule import AlertRule
from tripl.models.data_source import DataSource
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services import alerting_service
from tripl.services.demo_service import create_demo_project
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alerts as alerts_task


class NetworkAccessError(RuntimeError):
    """Raised by the test's network tripwire on any outbound attempt."""


def _install_network_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make ANY outbound network use raise; return the list of attempts made.

    Patches the low-level ``socket.socket`` (which urllib — the alert send
    transport — is built on) plus the higher-level ``httpx`` client sends and
    ``smtplib.SMTP``/``SMTP_SSL`` (the email channel). In-memory sqlite and the
    ASGI in-process transport never open a real socket, so this only trips on a
    genuine outbound send.

    The returned list records every attempt, which lets a caller tell "refused
    before dialling" apart from "dialled, and the tripwire stopped it" — the
    difference between a real guard and a delivery that merely happened to fail.
    """
    attempts: list[str] = []

    def _blocked(*_args: object, **_kwargs: object) -> object:
        attempts.append("outbound")
        raise NetworkAccessError("outbound network attempted from a demo alert path")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(smtplib, "SMTP", _blocked)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _blocked)
    monkeypatch.setattr(httpx.Client, "send", _blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", _blocked)
    return attempts


def _seed_demo_sink_delivery(
    session,
    *,
    status: str,
    destination_type: str = AlertDestinationType.demo_sink.value,
    webhook_url: str | None = None,
) -> str:
    """Seed a demo project's destination + rule + one pending delivery.

    Mirrors the ORM shapes the live dispatch pipeline writes, so the worker task
    renders + records against a realistic row set. Defaults to the local
    ``demo_sink``; pass ``destination_type``/``webhook_url`` to seed the row a
    demo must never be able to send through. Returns the delivery id.
    """
    project = Project(
        id=uuid.uuid4(),
        name="Demo Sink Runtime",
        slug="demo-sink-runtime",
        description="",
        is_demo=True,
    )
    data_source = DataSource(
        id=uuid.uuid4(),
        name="Runtime DS",
        db_type="clickhouse",
        host="localhost",
        port=8123,
        database_name="default",
        username="default",
        password_encrypted="",
    )
    scan_config = ScanConfig(
        id=uuid.uuid4(),
        data_source_id=data_source.id,
        project_id=project.id,
        name="Runtime Scan",
        base_query="SELECT * FROM events",
        time_column="created_at",
        cardinality_threshold=100,
        interval="1h",
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type=destination_type,
        name="Local demo sink",
        enabled=True,
        webhook_url_encrypted=encrypt_value(webhook_url) if webhook_url else None,
    )
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Spike & drift watch (demo)",
        enabled=True,
        message_format="plain",
    )
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=project.id,
        scan_config_id=scan_config.id,
        destination_id=destination.id,
        rule_id=rule.id,
        channel=destination_type,
        status=status,
        matched_count=1,
        payload_snapshot={"preview": "one alert"},
    )
    item = AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=delivery.id,
        scope_type="event",
        scope_ref="event-1",
        scope_name="Home Screen View",
        bucket=datetime(2026, 4, 11, 9, tzinfo=UTC),
        direction="spike",
        actual_count=120,
        expected_count=40,
        absolute_delta=80,
        percent_delta=200,
    )
    session.add_all([project, data_source, scan_config, destination, rule, delivery, item])
    session.commit()
    return str(delivery.id)


def test_demo_sink_dispatch_and_retry_take_local_no_network_path(
    tmp_path,
    monkeypatch,
) -> None:
    """Dispatch AND retry of a demo_sink delivery render + record locally, no net.

    Both the first dispatch and the re-dispatched (retry) run of the worker task
    take the same local branch: they render the message, stamp local/simulated
    markers, mark the delivery ``sent`` and open no connection. The network guard
    is installed for the whole test, so any outbound attempt would surface as a
    ``failed`` result instead of ``sent``.
    """
    _install_network_guard(monkeypatch)

    engine = create_engine(f"sqlite:///{tmp_path / 'demo_sink.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    with factory() as session:
        delivery_id = _seed_demo_sink_delivery(session, status="pending")

    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        factory,
    )

    # First dispatch (tick).
    result = alerts_task.send_alert_delivery.run(delivery_id)
    assert result["status"] == "sent"

    with factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        assert delivery.status == AlertDeliveryStatus.sent.value
        assert delivery.error_message is None
        snapshot = delivery.payload_snapshot
        assert snapshot is not None
        # Rendered locally and clearly marked as a local, simulated delivery.
        assert snapshot["rendered_message"]
        assert snapshot["delivery_mode"] == "local_sink"
        assert snapshot["is_local"] is True
        assert snapshot["simulated"] is True
        # Never claims a real external success (no ticket / webhook echo).
        assert "external_issue_id" not in snapshot
        assert "external_issue_key" not in snapshot
        assert delivery.channel == AlertDestinationType.demo_sink.value
        # Reset to pending exactly as retry_delivery does before re-enqueue.
        delivery.status = AlertDeliveryStatus.pending.value
        delivery.error_message = None
        delivery.sent_at = None
        delivery.dispatch_attempts = 0
        session.commit()

    # Retry: the re-dispatched worker body must take the same no-network path.
    retry_result = alerts_task.send_alert_delivery.run(delivery_id)
    assert retry_result["status"] == "sent"

    with factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        assert delivery.status == AlertDeliveryStatus.sent.value
        assert delivery.error_message is None
        assert delivery.payload_snapshot is not None
        assert delivery.payload_snapshot["is_local"] is True

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_demo_sink_seeded_scenario_is_local_and_no_network(monkeypatch) -> None:
    """Create + simulate + retry over a seeded demo emit zero outbound network.

    Seeds a full demo project under the network guard, then asserts the alerting
    surface is explorable and clearly local: a ``demo_sink`` destination badged
    ``is_local``, a firing + a healthy monitor, a locally-recorded delivery with
    rendered detail, an explorable inbox group, a render-only simulate, and a
    retry that resets to pending and re-enqueues without opening a connection.
    """
    _install_network_guard(monkeypatch)

    async with TestSessionLocal() as session:
        project = await create_demo_project(session)
        slug = project.slug

        # --- destinations: the local sink is present and badged local ---
        destinations = await alerting_service.list_destinations(session, slug)
        demo_sinks = [d for d in destinations if d.type == AlertDestinationType.demo_sink]
        assert len(demo_sinks) == 1
        demo_sink = demo_sinks[0]
        assert demo_sink.is_local is True
        # No secrets were stored for the local sink.
        assert demo_sink.webhook_set is False
        assert demo_sink.bot_token_set is False
        assert demo_sink.jira_api_token_set is False
        assert demo_sink.linear_api_key_set is False
        # The optional disabled external example carries no credentials.
        disabled = [d for d in destinations if not d.enabled]
        assert disabled and all(d.webhook_set is False for d in disabled)

        rules = demo_sink.rules
        assert len(rules) >= 2
        firing_rule = next(r for r in rules if r.include_metrics and r.include_schema_drifts)

        # --- monitors: shows both a firing and a healthy example ---
        summary = await alerting_service.get_monitors_summary(session, slug)
        assert summary.firing_count >= 1
        assert summary.healthy_count >= 1

        # --- delivery history + detail: populated, rendered, and local ---
        deliveries = await alerting_service.list_deliveries(session, slug)
        assert deliveries.total >= 1
        local_delivery = next(
            d for d in deliveries.items if d.channel == AlertDestinationType.demo_sink
        )
        assert local_delivery.is_local is True
        assert local_delivery.is_simulated is True
        assert local_delivery.status == AlertDeliveryStatus.sent
        detail = await alerting_service.get_delivery(session, slug, local_delivery.id)
        assert detail.items, "delivery detail should carry rendered items"
        assert detail.payload_snapshot is not None
        assert detail.payload_snapshot["rendered_message"]
        assert detail.payload_snapshot["is_local"] is True

        # --- inbox is explorable, and the card counts BOTH attempts ---
        inbox = await alerting_service.list_alert_inbox(session, slug)
        assert inbox.total >= 1
        # The seeded incident is one delivery that succeeded and one earlier
        # attempt that failed, and the failed one owns its own copy of the same
        # items under the same correlation group — which is the
        # shape live dispatch writes, and what keeps the failed row and its
        # Retry reachable from the card. So the card reads two deliveries and
        # twice the items, exactly as it does for any scope that fires twice.
        # Seed the failed row without items again and this drops to 1x/1.
        incident = next(
            group for group in inbox.items if any(ref.id == firing_rule.id for ref in group.rules)
        )
        assert incident.delivery_count == 2
        assert incident.item_count == 2 * len(detail.items)

        # --- simulate: render-only, no network, produces firings + a message ---
        simulation = await alerting_service.simulate_rule(
            session,
            slug,
            demo_sink.id,
            firing_rule.id,
            days=30,
        )
        assert simulation.anomalies_considered >= 1
        assert simulation.firings
        assert simulation.rendered_message

        # --- retry: resets to pending and re-enqueues without any network ---
        failed = await session.scalar(
            select(AlertDelivery).where(AlertDelivery.id == local_delivery.id)
        )
        assert failed is not None
        failed.status = AlertDeliveryStatus.failed.value
        failed.error_message = "simulated prior failure"
        await session.commit()

        enqueued: list[str] = []
        monkeypatch.setattr(
            alerts_task.send_alert_delivery,
            "delay",
            lambda delivery_id: enqueued.append(delivery_id),
        )
        retried = await alerting_service.retry_delivery(session, slug, local_delivery.id)
        # retry_delivery itself performs no network: it resets the row and hands
        # the delivery to the worker via .delay (whose local no-network body is
        # proven by the sync test above).
        assert retried.status == AlertDeliveryStatus.pending
        assert enqueued == [str(local_delivery.id)]


@pytest.mark.asyncio
async def test_demo_sink_creation_is_demo_only_and_credential_free(client) -> None:
    """A demo_sink is creatable only on a demo project and never stores secrets.

    Also verifies the create API rejects any attempt to attach credentials to a
    demo_sink, and that a real project cannot host one. (No network guard here:
    this exercises the in-process ASGI client — which itself rides httpx — and
    the zero-network guarantee is proven by the two tests above; these CRUD
    guards perform no I/O.)
    """
    # A real (non-demo) project rejects a demo_sink.
    resp = await client.post(
        "/api/v1/projects",
        json={"name": "Real Project", "slug": "real-proj"},
    )
    assert resp.status_code == 201
    resp = await client.post(
        "/api/v1/projects/real-proj/alert-destinations",
        json={"type": "demo_sink", "name": "Sneaky local sink"},
    )
    assert resp.status_code == 422

    # A demo project accepts a credential-free demo_sink...
    async with TestSessionLocal() as session:
        demo = await create_demo_project(session)
    demo_slug = demo.slug
    resp = await client.post(
        f"/api/v1/projects/{demo_slug}/alert-destinations",
        json={"type": "demo_sink", "name": "Extra local sink"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["is_local"] is True
    assert body["webhook_set"] is False

    # ...but rejects one that tries to smuggle in credentials.
    resp = await client.post(
        f"/api/v1/projects/{demo_slug}/alert-destinations",
        json={
            "type": "demo_sink",
            "name": "Not really local",
            "target_url": "https://hooks.example.com/abc",
        },
    )
    assert resp.status_code == 422


def test_demo_project_never_delivers_to_an_external_destination(tmp_path, monkeypatch) -> None:
    """Dispatch REFUSES an external destination on a demo project, before dialling.

    Defence in depth for the zero-egress promise. The API now refuses to create
    or enable an external destination on a demo project, but a row can still
    exist (the seeded disabled Slack example, a database written before that
    guard, direct SQL), and every dispatch path — scan dispatch, manual retry,
    stale-pending re-dispatch — funnels through this one task. So the task itself
    must refuse.

    The webhook is a REAL encrypted, validation-passing Slack URL: without the
    guard the task would decrypt it, validate it and POST, so ``attempts`` would
    be non-empty. Asserting the delivery merely ``failed`` would prove nothing —
    the tripwire fails it either way. Asserting nothing was ever dialled is what
    pins the guard.
    """
    attempts = _install_network_guard(monkeypatch)

    engine = create_engine(f"sqlite:///{tmp_path / 'demo_external.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    with factory() as session:
        delivery_id = _seed_demo_sink_delivery(
            session,
            status="pending",
            destination_type=AlertDestinationType.slack.value,
            webhook_url="https://hooks.slack.com/services/T000/B000/xxxxxxxx",
        )

    monkeypatch.setitem(
        alerts_task.send_alert_delivery.run.__globals__,
        "_get_sync_session",
        factory,
    )

    result = alerts_task.send_alert_delivery.run(delivery_id)

    assert result["status"] == "failed"
    assert attempts == [], "a demo project must not even attempt an outbound alert"

    with factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None
        assert delivery.status == AlertDeliveryStatus.failed.value
        # Refused by the demo guard, not by a network error / bad credential.
        assert "disabled for demo projects" in (delivery.error_message or "")

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_demo_project_rejects_external_destinations_via_api(client) -> None:
    """A demo project may not gain, enable or credential an external destination.

    Covers the configuration route into the egress bug: a demo user opening
    Alerting and wiring up Slack/Telegram/webhook/email/Jira/Linear, or simply
    switching on the disabled Slack example the demo ships as an illustration.
    """
    async with TestSessionLocal() as session:
        demo = await create_demo_project(session)
    demo_slug = demo.slug

    # Every external channel is refused at create time.
    for payload in (
        {
            "type": "slack",
            "name": "Real Slack",
            "webhook_url": "https://hooks.slack.com/services/T/B/x",
        },
        {"type": "webhook", "name": "Real webhook", "target_url": "https://example.com/hook"},
        {"type": "telegram", "name": "Real TG", "bot_token": "123:abc", "chat_id": "42"},
        {"type": "email", "name": "Real email", "email_recipients": "ops@example.com"},
    ):
        resp = await client.post(
            f"/api/v1/projects/{demo_slug}/alert-destinations",
            json=payload,
        )
        assert resp.status_code == 422, f"{payload['type']} must be refused on a demo project"
        assert "cannot send external alerts" in resp.json()["detail"]

    # The seeded disabled Slack example stays disabled and credential-free.
    resp = await client.get(f"/api/v1/projects/{demo_slug}/alert-destinations")
    assert resp.status_code == 200
    example = next(d for d in resp.json() if d["type"] == "slack")
    assert example["enabled"] is False

    resp = await client.patch(
        f"/api/v1/projects/{demo_slug}/alert-destinations/{example['id']}",
        json={"enabled": True},
    )
    assert resp.status_code == 422

    resp = await client.patch(
        f"/api/v1/projects/{demo_slug}/alert-destinations/{example['id']}",
        json={"webhook_url": "https://hooks.slack.com/services/T000/B000/xxxxxxxx"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_demo_rule_cannot_arm_the_ai_explanation(client) -> None:
    """Arming a demo rule's AI explanation is refused, not silently ignored.

    Building the explanation is an outbound LLM call, so the worker skips it for
    demo projects; the API refuses the toggle so it cannot look armed while doing
    nothing.
    """
    async with TestSessionLocal() as session:
        demo = await create_demo_project(session)
        slug = demo.slug
        destinations = await alerting_service.list_destinations(session, slug)

    sink = next(d for d in destinations if d.type == AlertDestinationType.demo_sink)
    rule = sink.rules[0]

    resp = await client.patch(
        f"/api/v1/projects/{slug}/alert-destinations/{sink.id}/rules/{rule.id}",
        json={"ai_explanation_enabled": True},
    )
    assert resp.status_code == 422
    assert "AI explanations are disabled for demo projects" in resp.json()["detail"]
