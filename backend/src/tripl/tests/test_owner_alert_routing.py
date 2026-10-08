"""Route alerts to the owners of the affected event type (F07, #260).

Two halves. The WORKER half drives ``alerts.send_alert_delivery`` (and the
scheduled digest's ``send_alert_digest``) against a file-backed SQLite
database, then runs the ``notify_owners`` follow-up it enqueued, with SMTP
replaced by a recorder: who is an owner per scope (event type, event -> its
type, catalog metric owner, project total -> nobody), who is dropped
(non-members, no usable address), what a missing SMTP relay does (rows
``skipped``, the rule's own delivery still ``sent``), and that nothing is sent
twice for one delivery.

The API half covers the payloads (``owners`` on inbox cards and signals,
``owner_notifications`` on the delivery detail, ``notify_owners`` on rules)
and the two manual "Notify owners" routes with their membership / editor gates.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.api.deps import get_current_user
from tripl.config import SMTP_SECURITY_STARTTLS
from tripl.main import app
from tripl.models import Base
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_owner_notification import AlertOwnerNotification
from tripl.models.alert_rule import AlertRule
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.domain_enums import (
    MetricAggregation,
    MetricComposition,
    MetricKind,
    MetricStatus,
)
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.organization import DEFAULT_ORG_ID, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.scan_config import ScanConfig
from tripl.models.user import User
from tripl.services import alert_owner_routing, app_settings_service
from tripl.tests._members import persisted_member_user
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import alert_digest_send, alert_owner_notify, alerts

BUCKET = datetime(2026, 9, 27, 9, tzinfo=UTC)


def _email_config(host: str = "relay.example.com") -> app_settings_service.EmailConfig:
    return app_settings_service.EmailConfig(
        smtp_host=host,
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_security=SMTP_SECURITY_STARTTLS,
        smtp_from_address="alerts@example.com",
    )


# --- worker world ---------------------------------------------------------------


@dataclass(frozen=True)
class World:
    project_id: uuid.UUID
    scan_config_id: uuid.UUID
    destination_id: uuid.UUID
    rule_id: uuid.UUID
    event_type_id: uuid.UUID
    unowned_type_id: uuid.UUID
    event_id: uuid.UUID
    metric_id: uuid.UUID
    anna: uuid.UUID
    oleg: uuid.UUID
    stranger: uuid.UUID
    nomail: uuid.UUID


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'owner_routing.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _user(session: Session, email: str, name: str | None) -> uuid.UUID:
    user = User(id=uuid.uuid4(), email=email, name=name, password_hash="x")
    session.add(user)
    session.flush()
    return user.id


def _build_world(
    session: Session, *, notify_owners: bool = True, email_domain: str = "example.com"
) -> World:
    # ``email_domain`` lets a test build a second world in the same database:
    # ``users.email`` is unique, so the default addresses can exist only once.
    project = Project(id=uuid.uuid4(), name="Shop", slug=f"shop-{uuid.uuid4().hex[:8]}")
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
    session.add_all([project, data_source])
    session.flush()
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
    owned_type = EventType(id=uuid.uuid4(), project_id=project.id, name="page", display_name="Page")
    unowned_type = EventType(
        id=uuid.uuid4(), project_id=project.id, name="misc", display_name="Misc"
    )
    session.add_all([config, owned_type, unowned_type])
    session.flush()
    event = Event(id=uuid.uuid4(), project_id=project.id, event_type_id=owned_type.id, name="view")
    session.add(event)

    anna = _user(session, f"anna@{email_domain}", "Anna")
    oleg = _user(session, f"oleg@{email_domain}", "Oleg")
    stranger = _user(session, f"stranger@{email_domain}", "Stranger")
    nomail = _user(session, f"no-address-{uuid.uuid4().hex[:8]}", "Nomail")
    for member in (anna, oleg, nomail):
        # A row counts only for a member of the project's organization.
        session.add(
            OrganizationMember(organization_id=DEFAULT_ORG_ID, user_id=member, role="member")
        )
        session.add(ProjectMember(project_id=project.id, user_id=member, role="editor"))
    # ``stranger`` owns the type but is not (or no longer) a member.
    for owner in (anna, stranger, nomail):
        session.add(EventTypeOwner(event_type_id=owned_type.id, user_id=owner))
    metric = MetricDefinition(
        id=uuid.uuid4(),
        project_id=project.id,
        name="conv",
        display_name="Conversions",
        kind=MetricKind.fact.value,
        aggregation=MetricAggregation.count.value,
        composition=MetricComposition.single.value,
        config={},
        interval="1h",
        status=MetricStatus.active.value,
        owner_id=oleg,
    )
    destination = AlertDestination(
        id=uuid.uuid4(),
        project_id=project.id,
        type="slack",
        name="Main Slack",
        enabled=True,
        webhook_url_encrypted="secret",
    )
    session.add_all([metric, destination])
    session.flush()
    rule = AlertRule(
        id=uuid.uuid4(),
        destination_id=destination.id,
        name="Everything",
        enabled=True,
        include_metrics=True,
        notify_owners=notify_owners,
        min_percent_delta=0,
    )
    session.add(rule)
    session.commit()
    return World(
        project_id=project.id,
        scan_config_id=config.id,
        destination_id=destination.id,
        rule_id=rule.id,
        event_type_id=owned_type.id,
        unowned_type_id=unowned_type.id,
        event_id=event.id,
        metric_id=metric.id,
        anna=anna,
        oleg=oleg,
        stranger=stranger,
        nomail=nomail,
    )


def _item(
    delivery_id: uuid.UUID, scope_type: str, scope_ref: str, **extra: object
) -> AlertDeliveryItem:
    return AlertDeliveryItem(
        id=uuid.uuid4(),
        delivery_id=delivery_id,
        scope_type=scope_type,
        scope_ref=scope_ref,
        scope_name=f"{scope_type}:{scope_ref[:8]}",
        bucket=BUCKET,
        direction="drop",
        actual_count=10,
        expected_count=40,
        absolute_delta=30,
        percent_delta=75,
        correlation_group_id=uuid.uuid4(),
        **extra,
    )


def _delivery(
    session: Session,
    world: World,
    items: list[tuple[str, str, dict[str, object]]],
    *,
    rule_id: uuid.UUID | None = None,
    digest: bool = False,
) -> str:
    delivery = AlertDelivery(
        id=uuid.uuid4(),
        project_id=world.project_id,
        scan_config_id=world.scan_config_id,
        destination_id=world.destination_id,
        rule_id=rule_id or world.rule_id,
        channel="slack",
        status="pending",
        matched_count=len(items),
        payload_snapshot={"digest": True} if digest else {},
    )
    session.add(delivery)
    session.flush()
    for scope_type, scope_ref, extra in items:
        session.add(_item(delivery.id, scope_type, scope_ref, **extra))
    session.commit()
    return str(delivery.id)


def _full_delivery(session: Session, world: World) -> str:
    return _delivery(
        session,
        world,
        [
            ("event_type", str(world.event_type_id), {"event_type_id": world.event_type_id}),
            # No event_type_id on the item: the event row names its type.
            ("event", str(world.event_id), {"event_id": world.event_id}),
            ("metric", str(world.metric_id), {}),
            ("project_total", str(world.scan_config_id), {}),
            ("event_type", str(world.unowned_type_id), {"event_type_id": world.unowned_type_id}),
        ],
    )


@dataclass
class Harness:
    slack: list[str]
    emails: list[dict[str, object]]
    enqueued: list[str]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]) -> Harness:
    recorded = Harness(slack=[], emails=[], enqueued=[])
    monkeypatch.setattr(alerts, "_get_sync_session", factory)
    monkeypatch.setattr(alert_digest_send, "_get_sync_session", factory)
    monkeypatch.setattr(alert_owner_notify, "_get_sync_session", factory)
    monkeypatch.setattr(
        alerts, "_resolve_slack_webhook", lambda destination: "https://hooks.slack.com/x"
    )
    monkeypatch.setattr(
        alerts,
        "_send_slack_message",
        lambda url, text, *, message_format: recorded.slack.append(text),
    )
    monkeypatch.setattr(
        alerts, "_send_email_message", lambda **kwargs: recorded.emails.append(kwargs)
    )
    monkeypatch.setattr(
        app_settings_service, "get_email_config_sync", lambda *_a, **_k: _email_config()
    )
    monkeypatch.setattr(alert_owner_notify, "enqueue_owner_notifications", recorded.enqueued.append)
    return recorded


def _rows(factory: sessionmaker[Session], delivery_id: str) -> list[AlertOwnerNotification]:
    with factory() as session:
        return list(
            session.execute(
                select(AlertOwnerNotification)
                .where(AlertOwnerNotification.delivery_id == uuid.UUID(delivery_id))
                .order_by(AlertOwnerNotification.email)
            ).scalars()
        )


def test_owners_are_resolved_per_scope_and_emailed_once_each(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)

    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "sent"
    assert harness.slack, "the rule's own destination still gets its message"
    assert harness.enqueued == [delivery_id]

    result = alert_owner_notify.notify_owners.run(delivery_id)
    assert result["status"] == "done"
    assert result["sent"] == 2

    by_recipient = {tuple(mail["recipients"])[0]: mail for mail in harness.emails}  # type: ignore[arg-type]
    # anna owns the type: its event_type item AND the event item (event -> type).
    # oleg owns the metric. stranger is no member; "no-address" has no email.
    assert set(by_recipient) == {"anna@example.com", "oleg@example.com"}
    anna_body = str(by_recipient["anna@example.com"]["body"])
    assert "2 signal(s) you own" in anna_body
    assert "Hi Anna" in anna_body
    assert "1 signal(s) you own" in str(by_recipient["oleg@example.com"]["body"])
    assert by_recipient["anna@example.com"]["from_address"] == "alerts@example.com"

    rows = _rows(factory, delivery_id)
    assert [(row.email, row.status, row.source) for row in rows] == [
        ("anna@example.com", "sent", "rule"),
        ("oleg@example.com", "sent", "rule"),
    ]
    assert all(row.sent_at is not None for row in rows)


def test_a_repeat_run_or_a_retried_delivery_never_re_sends(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)
    alert_owner_notify.notify_owners.run(delivery_id)
    assert len(harness.emails) == 2

    again = alert_owner_notify.notify_owners.run(delivery_id)
    assert again["sent"] == 0
    assert again["already"] == 2
    # The main delivery re-run is "already_sent"; it re-queues the follow-up
    # (in case the sending run died before queueing it), which sends nothing.
    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "already_sent"
    assert harness.enqueued == [delivery_id, delivery_id]
    assert alert_owner_notify.notify_owners.run(delivery_id)["already"] == 2
    assert len(harness.emails) == 2
    assert len(_rows(factory, delivery_id)) == 2


def test_unowned_scopes_and_rules_without_the_switch_behave_as_before(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        unowned = _delivery(
            session,
            world,
            [
                ("project_total", str(world.scan_config_id), {}),
                (
                    "event_type",
                    str(world.unowned_type_id),
                    {"event_type_id": world.unowned_type_id},
                ),
            ],
        )
    assert alerts.send_alert_delivery.run(unowned)["status"] == "sent"
    assert alert_owner_notify.notify_owners.run(unowned)["status"] == "no_owners"
    assert harness.emails == []
    assert _rows(factory, unowned) == []

    with factory() as session:
        quiet_world = _build_world(
            session, notify_owners=False, email_domain=f"quiet-{uuid.uuid4().hex[:8]}.example.com"
        )
        quiet = _full_delivery(session, quiet_world)
    assert alerts.send_alert_delivery.run(quiet)["status"] == "sent"
    assert quiet not in harness.enqueued
    # Even if a message arrived anyway, the switch is re-read and nothing goes out.
    assert alert_owner_notify.notify_owners.run(quiet)["status"] == "disabled"
    assert harness.emails == []


def test_missing_smtp_records_skipped_rows_and_keeps_the_delivery_sent(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], harness: Harness
) -> None:
    monkeypatch.setattr(
        app_settings_service, "get_email_config_sync", lambda *_a, **_k: _email_config(host="")
    )
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)

    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "sent"
    result = alert_owner_notify.notify_owners.run(delivery_id)
    assert result["skipped"] == 2
    assert harness.emails == []
    rows = _rows(factory, delivery_id)
    assert {row.status for row in rows} == {"skipped"}
    assert {row.error for row in rows} == {alert_owner_routing.SMTP_NOT_CONFIGURED}
    with factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None and delivery.status == "sent"


def test_a_failed_owner_email_is_recorded_and_does_not_touch_the_delivery(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], harness: Harness
) -> None:
    def refuse(**kwargs: object) -> None:
        raise ValueError("SMTP refused 1 of 1 recipients")

    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)
    monkeypatch.setattr(alerts, "_send_email_message", refuse)

    assert alert_owner_notify.notify_owners.run(delivery_id)["failed"] == 2
    rows = _rows(factory, delivery_id)
    assert {row.status for row in rows} == {"failed"}
    assert all("refused" in (row.error or "") for row in rows)
    with factory() as session:
        delivery = session.get(AlertDelivery, uuid.UUID(delivery_id))
        assert delivery is not None and delivery.status == "sent"


def test_a_rule_muted_after_its_delivery_sends_no_owner_email(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)
    with factory() as session:
        rule = session.get(AlertRule, world.rule_id)
        assert rule is not None
        rule.muted_until = datetime.now(UTC) + timedelta(hours=1)
        session.commit()

    assert alert_owner_notify.notify_owners.run(delivery_id)["status"] == "muted"
    assert harness.emails == []


def test_the_digest_path_notifies_the_owners_of_its_items(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        other_rule = AlertRule(
            id=uuid.uuid4(),
            destination_id=world.destination_id,
            name="Quiet",
            enabled=True,
            notify_owners=False,
        )
        session.add(other_rule)
        session.commit()
        owned = _delivery(
            session,
            world,
            [("event_type", str(world.event_type_id), {"event_type_id": world.event_type_id})],
            digest=True,
        )
        quiet = _delivery(
            session,
            world,
            [("event_type", str(world.event_type_id), {"event_type_id": world.event_type_id})],
            rule_id=other_rule.id,
            digest=True,
        )

    result = alert_digest_send.send_alert_digest.run([owned, quiet])
    assert result["sent"] == 2
    assert len(harness.slack) == 1, "two rules on one destination still make one message"
    assert harness.enqueued == [owned]

    assert alert_owner_notify.notify_owners.run(owned)["sent"] == 1
    assert [tuple(mail["recipients"]) for mail in harness.emails] == [("anna@example.com",)]  # type: ignore[arg-type]


def test_a_rule_disabled_after_its_delivery_sends_no_owner_email(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)
    with factory() as session:
        rule = session.get(AlertRule, world.rule_id)
        assert rule is not None
        rule.enabled = False
        session.commit()

    assert alert_owner_notify.notify_owners.run(delivery_id)["status"] == "muted"
    assert harness.emails == []
    assert _rows(factory, delivery_id) == []


def test_a_concurrent_insert_claim_counts_as_already_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)
    assert alert_owner_notify.notify_owners.run(delivery_id)["sent"] == 2

    # A run that read "no rows yet" just before the other run's commit: its
    # INSERT hits the unique key, and the IntegrityError is the lost claim.
    monkeypatch.setattr(alert_owner_notify, "_existing_rows", lambda *_a, **_k: {})
    again = alert_owner_notify.notify_owners.run(delivery_id)
    assert again["already"] == 2
    assert again["sent"] == 0
    assert len(harness.emails) == 2
    assert {row.status for row in _rows(factory, delivery_id)} == {"sent"}


def test_one_owner_failing_to_render_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], harness: Harness
) -> None:
    real_build = alert_owner_routing.build_owner_email

    def build(**kwargs: str) -> tuple[str, str]:
        if kwargs["owner_name"] == "Anna":
            raise RuntimeError("template exploded")
        return real_build(**kwargs)

    monkeypatch.setattr(alert_owner_routing, "build_owner_email", build)
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)

    result = alert_owner_notify.notify_owners.run(delivery_id)
    assert (result["sent"], result["failed"]) == (1, 1)
    assert [tuple(mail["recipients"]) for mail in harness.emails] == [("oleg@example.com",)]  # type: ignore[arg-type]
    by_email = {row.email: row for row in _rows(factory, delivery_id)}
    assert by_email["anna@example.com"].status == "failed"
    assert "template exploded" in (by_email["anna@example.com"].error or "")
    assert by_email["oleg@example.com"].status == "sent"


def test_skipped_rows_are_sent_once_smtp_is_configured(
    monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session], harness: Harness
) -> None:
    monkeypatch.setattr(
        app_settings_service, "get_email_config_sync", lambda *_a, **_k: _email_config(host="")
    )
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)
    assert alert_owner_notify.notify_owners.run(delivery_id)["skipped"] == 2
    assert harness.emails == []

    monkeypatch.setattr(
        app_settings_service, "get_email_config_sync", lambda *_a, **_k: _email_config()
    )
    result = alert_owner_notify.notify_owners.run(delivery_id)
    assert result["sent"] == 2
    assert len(harness.emails) == 2
    rows = _rows(factory, delivery_id)
    assert len(rows) == 2, "re-claimed in place, not duplicated"
    assert {(row.status, row.error) for row in rows} == {("sent", None)}


def test_a_stale_pending_claim_is_reclaimed_and_a_fresh_one_is_respected(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
    alerts.send_alert_delivery.run(delivery_id)
    now = datetime.now(UTC)
    with factory() as session:
        for user_id, email, touched in (
            # A worker died mid-send long ago: its lease is over.
            (world.anna, "anna@example.com", now - timedelta(minutes=20)),
            # A worker is sending right now.
            (world.oleg, "oleg@example.com", now - timedelta(minutes=1)),
        ):
            session.add(
                AlertOwnerNotification(
                    project_id=world.project_id,
                    delivery_id=uuid.UUID(delivery_id),
                    user_id=user_id,
                    email=email,
                    status="pending",
                    created_at=touched,
                    updated_at=touched,
                )
            )
        session.commit()

    result = alert_owner_notify.notify_owners.run(delivery_id)
    assert (result["sent"], result["already"]) == (1, 1)
    assert [tuple(mail["recipients"]) for mail in harness.emails] == [("anna@example.com",)]  # type: ignore[arg-type]
    by_email = {row.email: row.status for row in _rows(factory, delivery_id)}
    assert by_email == {"anna@example.com": "sent", "oleg@example.com": "pending"}


def test_an_already_sent_redelivery_still_queues_the_owner_follow_up(
    factory: sessionmaker[Session], harness: Harness
) -> None:
    with factory() as session:
        world = _build_world(session)
        delivery_id = _full_delivery(session, world)
        digest_id = _delivery(
            session,
            world,
            [("event_type", str(world.event_type_id), {"event_type_id": world.event_type_id})],
            digest=True,
        )
    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "sent"
    # The sending run died before its enqueue reached the broker: nothing queued.
    harness.enqueued.clear()

    assert alerts.send_alert_delivery.run(delivery_id)["status"] == "already_sent"
    assert harness.enqueued == [delivery_id]
    assert alert_owner_notify.notify_owners.run(delivery_id)["sent"] == 2

    assert alert_digest_send.send_alert_digest.run([digest_id])["sent"] == 1
    assert alert_digest_send.send_alert_digest.run([digest_id])["status"] == "already_sent"
    assert harness.enqueued == [delivery_id, digest_id, digest_id]
    assert alert_owner_notify.notify_owners.run(digest_id)["sent"] == 1
    assert alert_owner_notify.notify_owners.run(digest_id)["already"] == 1
    assert len(harness.emails) == 3


def test_resolution_drops_owners_who_lost_membership(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        world = _build_world(session)
        scopes = [
            alert_owner_routing.OwnedScope("event_type", str(world.event_type_id)),
            alert_owner_routing.OwnedScope("event", str(world.event_id)),
            alert_owner_routing.OwnedScope("metric", str(world.metric_id)),
            alert_owner_routing.OwnedScope("project_total", str(world.scan_config_id)),
        ]
        before = alert_owner_routing.resolve_owners_sync(session, world.project_id, scopes)
        assert [[owner.user_id for owner in owners] for owners in before] == [
            [world.anna],
            [world.anna],
            [world.oleg],
            [],
        ]
        membership = session.execute(
            select(ProjectMember).where(ProjectMember.user_id == world.anna)
        ).scalar_one()
        session.delete(membership)
        session.commit()
        after = alert_owner_routing.resolve_owners_sync(session, world.project_id, scopes)
        assert [[owner.user_id for owner in owners] for owners in after] == [
            [],
            [],
            [world.oleg],
            [],
        ]


# --- API -------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiWorld:
    slug: str
    project_id: uuid.UUID
    group_id: uuid.UUID
    delivery_id: uuid.UUID
    metric_id: uuid.UUID
    owner_id: uuid.UUID


async def _api_world(client: AsyncClient, slug: str) -> ApiWorld:
    created = await client.post(
        "/api/v1/projects", json={"name": slug, "slug": slug, "description": ""}
    )
    assert created.status_code == 201, created.text
    project_id = uuid.UUID(created.json()["id"])
    event_type = await client.post(
        f"/api/v1/projects/{slug}/event-types", json={"name": "track", "display_name": "Track"}
    )
    assert event_type.status_code == 201, event_type.text
    event_type_id = uuid.UUID(event_type.json()["id"])
    owner = await persisted_member_user(
        project_id, role="editor", email=f"owner-{slug}@example.com"
    )
    group_id = uuid.uuid4()
    async with TestSessionLocal() as session:
        stranger = User(
            id=uuid.uuid4(), email=f"stranger-{slug}@example.com", name="S", password_hash="x"
        )
        data_source = DataSource(
            id=uuid.uuid4(),
            name=f"DS {slug}",
            db_type="clickhouse",
            host="localhost",
            port=8123,
            database_name="default",
            username="default",
            password_encrypted="",
        )
        session.add_all([stranger, data_source])
        await session.flush()
        scan_config = ScanConfig(
            id=uuid.uuid4(),
            data_source_id=data_source.id,
            project_id=project_id,
            name="Scan",
            base_query="SELECT * FROM events",
            time_column="created_at",
            cardinality_threshold=100,
            interval="1h",
        )
        destination = AlertDestination(
            id=uuid.uuid4(),
            project_id=project_id,
            type="slack",
            name="Slack",
            enabled=True,
            webhook_url_encrypted="secret",
        )
        metric = MetricDefinition(
            id=uuid.uuid4(),
            project_id=project_id,
            name="conv",
            display_name="Conversions",
            kind=MetricKind.fact.value,
            aggregation=MetricAggregation.count.value,
            composition=MetricComposition.single.value,
            config={},
            interval="1h",
            status=MetricStatus.active.value,
            owner_id=owner.id,
        )
        session.add_all(
            [
                scan_config,
                destination,
                metric,
                EventTypeOwner(event_type_id=event_type_id, user_id=owner.id),
                EventTypeOwner(event_type_id=event_type_id, user_id=stranger.id),
            ]
        )
        await session.flush()
        rule = AlertRule(id=uuid.uuid4(), destination_id=destination.id, name="Rule", enabled=True)
        session.add(rule)
        await session.flush()
        delivery = AlertDelivery(
            id=uuid.uuid4(),
            project_id=project_id,
            scan_config_id=scan_config.id,
            destination_id=destination.id,
            rule_id=rule.id,
            channel="slack",
            status="sent",
            matched_count=1,
            payload_snapshot={},
            sent_at=BUCKET,
        )
        session.add(delivery)
        await session.flush()
        session.add(
            AlertDeliveryItem(
                id=uuid.uuid4(),
                delivery_id=delivery.id,
                scope_type="event_type",
                scope_ref=str(event_type_id),
                scope_name="Track",
                event_type_id=event_type_id,
                bucket=BUCKET,
                direction="drop",
                actual_count=10,
                expected_count=40,
                absolute_delta=30,
                percent_delta=75,
                correlation_group_id=group_id,
            )
        )
        session.add(
            MetricAnomaly(
                id=uuid.uuid4(),
                scan_config_id=None,
                scope_type="metric",
                scope_ref=str(metric.id),
                bucket=BUCKET,
                actual_count=3,
                expected_count=12,
                stddev=1,
                z_score=-9,
                direction="drop",
            )
        )
        session.add(
            AlertOwnerNotification(
                project_id=project_id,
                delivery_id=delivery.id,
                user_id=owner.id,
                email=owner.email,
                status="sent",
                sent_at=BUCKET,
            )
        )
        await session.commit()
    return ApiWorld(
        slug=slug,
        project_id=project_id,
        group_id=group_id,
        delivery_id=delivery.id,
        metric_id=metric.id,
        owner_id=owner.id,
    )


@pytest.fixture
def mail_org_ids() -> list[uuid.UUID | None]:
    """The ``org_id`` each email-config lookup asked for, in call order."""
    return []


@pytest.fixture
def api_mail(
    monkeypatch: pytest.MonkeyPatch, mail_org_ids: list[uuid.UUID | None]
) -> list[dict[str, object]]:
    sent: list[dict[str, object]] = []

    async def config(
        _session: object, *, org_id: uuid.UUID | None
    ) -> app_settings_service.EmailConfig:
        mail_org_ids.append(org_id)
        return _email_config()

    monkeypatch.setattr(app_settings_service, "get_email_config", config)
    monkeypatch.setattr(alerts, "_send_email_message", lambda **kwargs: sent.append(kwargs))
    return sent


async def test_rule_payloads_carry_notify_owners(client: AsyncClient) -> None:
    slug = "owner-rule"
    await client.post("/api/v1/projects", json={"name": slug, "slug": slug, "description": ""})
    destination = await client.post(
        f"/api/v1/projects/{slug}/alert-destinations",
        json={
            "type": "webhook",
            "name": "Hook",
            "target_url": "https://example.com/hook",
        },
    )
    assert destination.status_code == 201, destination.text
    rules_url = f"/api/v1/projects/{slug}/alert-destinations/{destination.json()['id']}/rules"
    created = await client.post(rules_url, json={"name": "Owners", "notify_owners": True})
    assert created.status_code == 201, created.text
    assert created.json()["notify_owners"] is True

    patched = await client.patch(
        f"{rules_url}/{created.json()['id']}", json={"notify_owners": False}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["notify_owners"] is False
    refused = await client.patch(
        f"{rules_url}/{created.json()['id']}", json={"notify_owners": None}
    )
    assert refused.status_code == 422

    default = await client.post(rules_url, json={"name": "Plain"})
    assert default.json()["notify_owners"] is False


async def test_inbox_cards_and_delivery_detail_show_owners(client: AsyncClient) -> None:
    world = await _api_world(client, "owner-inbox")

    inbox = await client.get(f"/api/v1/projects/{world.slug}/alert-inbox")
    assert inbox.status_code == 200, inbox.text
    (card,) = inbox.json()["items"]
    # The stranger owns the type but is no member; names only, no addresses.
    assert card["owners"] == [{"user_id": str(world.owner_id), "name": "Member"}]

    one = await client.get(f"/api/v1/projects/{world.slug}/alert-inbox/{world.group_id}")
    assert one.json()["owners"] == card["owners"]

    detail = await client.get(f"/api/v1/projects/{world.slug}/alert-deliveries/{world.delivery_id}")
    assert detail.status_code == 200, detail.text
    (notification,) = detail.json()["owner_notifications"]
    assert notification["user_id"] == str(world.owner_id)
    assert notification["status"] == "sent"
    assert notification["email"] == f"owner-{world.slug}@example.com"


async def _project_org_id(project_id: uuid.UUID) -> uuid.UUID:
    async with TestSessionLocal() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        return project.organization_id


async def test_manual_incident_notify_emails_owners_and_is_audited(
    client: AsyncClient,
    api_mail: list[dict[str, object]],
    mail_org_ids: list[uuid.UUID | None],
) -> None:
    world = await _api_world(client, "owner-notify")

    resp = await client.post(
        f"/api/v1/projects/{world.slug}/alert-inbox/{world.group_id}/notify-owners"
    )
    assert resp.status_code == 200, resp.text
    # The mail relay is the project's organization's, not the instance default.
    assert mail_org_ids == [await _project_org_id(world.project_id)]
    (owner,) = resp.json()["owners"]
    assert owner["user_id"] == str(world.owner_id)
    assert owner["status"] == "sent"
    assert [mail["recipients"] for mail in api_mail] == [[f"owner-{world.slug}@example.com"]]
    assert "Track" in str(api_mail[0]["body"])

    async with TestSessionLocal() as session:
        manual = (
            await session.execute(
                select(AlertOwnerNotification).where(AlertOwnerNotification.source == "manual")
            )
        ).scalar_one()
        assert manual.delivery_id is None
        assert manual.correlation_group_id == world.group_id
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "alert_inbox.notify_owners")
            )
        ).scalar_one()
        assert audit.target_id == world.group_id

    missing = await client.post(
        f"/api/v1/projects/{world.slug}/alert-inbox/{uuid.uuid4()}/notify-owners"
    )
    assert missing.status_code == 404


async def test_manual_notify_without_smtp_records_skipped(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _api_world(client, "owner-nosmtp")

    asked: list[uuid.UUID | None] = []

    async def no_smtp(
        _session: object, *, org_id: uuid.UUID | None
    ) -> app_settings_service.EmailConfig:
        asked.append(org_id)
        return _email_config(host="")

    monkeypatch.setattr(app_settings_service, "get_email_config", no_smtp)
    resp = await client.post(
        f"/api/v1/projects/{world.slug}/alert-inbox/{world.group_id}/notify-owners"
    )
    assert resp.status_code == 200, resp.text
    (owner,) = resp.json()["owners"]
    assert owner["status"] == "skipped"
    assert owner["error"] == alert_owner_routing.SMTP_NOT_CONFIGURED
    assert asked == [await _project_org_id(world.project_id)]


async def test_signal_notify_reaches_the_metric_owner(
    client: AsyncClient, api_mail: list[dict[str, object]]
) -> None:
    world = await _api_world(client, "owner-signal")
    url = f"/api/v1/projects/{world.slug}/signals/notify-owners"
    body = {
        "scope_type": "metric",
        "scope_ref": str(world.metric_id),
        "scan_config_id": None,
        "bucket": BUCKET.isoformat(),
    }

    resp = await client.post(url, json=body)
    assert resp.status_code == 200, resp.text
    assert [owner["user_id"] for owner in resp.json()["owners"]] == [str(world.owner_id)]
    assert "Conversions" in str(api_mail[0]["body"])

    unknown = await client.post(url, json={**body, "bucket": "2020-01-01T00:00:00Z"})
    assert unknown.status_code == 404


async def test_manual_notify_needs_an_editor_member(
    client: AsyncClient, api_mail: list[dict[str, object]]
) -> None:
    world = await _api_world(client, "owner-gates")
    viewer = await persisted_member_user(
        world.project_id, role="viewer", email="viewer-owner@example.com"
    )
    async with TestSessionLocal() as session:
        outsider = User(
            id=uuid.uuid4(), email="outsider-owner@example.com", name="O", password_hash="x"
        )
        session.add(outsider)
        await session.commit()

    url = f"/api/v1/projects/{world.slug}/alert-inbox/{world.group_id}/notify-owners"

    def _as(user: User) -> Callable[[], Awaitable[User]]:
        # A parameterless override: FastAPI introspects an override's signature,
        # so a ``user: User = user`` default would be read as a request field.
        async def _as_user() -> User:
            return user

        return _as_user

    for user, expected in ((viewer, 403), (outsider, 404)):
        app.dependency_overrides[get_current_user] = _as(user)
        try:
            resp = await client.post(url)
        finally:
            app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == expected, resp.text
    assert api_mail == []


async def test_manual_notify_has_a_per_owner_cooldown(
    client: AsyncClient, api_mail: list[dict[str, object]]
) -> None:
    world = await _api_world(client, "owner-cooldown")
    url = f"/api/v1/projects/{world.slug}/alert-inbox/{world.group_id}/notify-owners"

    first = await client.post(url)
    assert first.json()["owners"][0]["status"] == "sent"
    second = await client.post(url)
    assert second.status_code == 200, second.text
    (owner,) = second.json()["owners"]
    assert owner["status"] == "skipped"
    assert owner["error"] == "notified 1 minute ago"
    assert len(api_mail) == 1

    async with TestSessionLocal() as session:
        manual = list(
            (
                await session.execute(
                    select(AlertOwnerNotification).where(
                        AlertOwnerNotification.source == "manual",
                        AlertOwnerNotification.project_id == world.project_id,
                    )
                )
            ).scalars()
        )
        # The cooled-down click is reported, not recorded.
        assert len(manual) == 1
        manual[0].sent_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()

    third = await client.post(url)
    assert third.json()["owners"][0]["status"] == "sent"
    assert len(api_mail) == 2


async def test_manual_notify_contacts_at_most_twenty_owners(
    client: AsyncClient,
    api_mail: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _api_world(client, "owner-cap")
    async with TestSessionLocal() as session:
        crowd = [
            User(
                id=uuid.uuid4(),
                email=f"crowd-{index}@example.com",
                name=f"Crowd {index}",
                password_hash="x",
            )
            for index in range(25)
        ]
        session.add_all(crowd)
        await session.commit()
    contacts = [
        alert_owner_routing.OwnerContact(user_id=user.id, name=user.name or "", email=user.email)
        for user in crowd
    ]

    async def many_owners(*_args: object, **_kwargs: object) -> list[list[object]]:
        return [contacts]

    monkeypatch.setattr(alert_owner_routing, "resolve_owners", many_owners)
    resp = await client.post(
        f"/api/v1/projects/{world.slug}/alert-inbox/{world.group_id}/notify-owners"
    )
    assert resp.status_code == 200, resp.text
    owners = resp.json()["owners"]
    assert len(owners) == 20
    assert [owner["user_id"] for owner in owners] == [str(c.user_id) for c in contacts[:20]]
    assert len(api_mail) == 20
