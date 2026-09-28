"""Email delivery of notifications (#259): instant path, daily/weekly digests.

Driven against a file-backed SQLite database with SMTP replaced by a recorder,
the way ``test_owner_alert_routing`` drives the owner emails. Covers who is
emailed on which path (``email_mode``, ``mentions_email``, defaults without a
prefs row), the members-only check at send time, the at-most-once stamp, what a
missing SMTP relay does (nothing sent, nothing stamped), and a failed send
releasing its claim.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tripl.config import SMTP_SECURITY_STARTTLS
from tripl.models import Base
from tripl.models.notification import Notification
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.models.user_notification_prefs import UserNotificationPrefs
from tripl.services import app_settings_service
from tripl.worker.tasks import alerts, notification_email

BASE_URL = "https://tripl.example.com"


def _email_config(host: str = "relay.example.com") -> app_settings_service.EmailConfig:
    return app_settings_service.EmailConfig(
        smtp_host=host,
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        smtp_security=SMTP_SECURITY_STARTTLS,
        smtp_from_address="tripl@example.com",
    )


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'notification_email.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@dataclass
class Outbox:
    emails: list[dict[str, object]]
    smtp_host: str = "relay.example.com"
    fail_for: str | None = None

    def recipients(self) -> list[str]:
        return [
            str(list(mail["recipients"])[0])  # type: ignore[call-overload]
            for mail in self.emails
        ]


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]) -> Outbox:
    box = Outbox(emails=[])

    def _send(**kwargs: object) -> None:
        recipient = str(list(kwargs["recipients"])[0])  # type: ignore[call-overload]
        if box.fail_for is not None and recipient == box.fail_for:
            raise OSError("relay refused")
        box.emails.append(kwargs)

    monkeypatch.setattr(notification_email, "_get_sync_session", factory)
    monkeypatch.setattr(alerts, "_send_email_message", _send)
    monkeypatch.setattr(
        app_settings_service,
        "get_email_config_sync",
        lambda *_a, **_k: _email_config(box.smtp_host),
    )
    monkeypatch.setattr(
        app_settings_service,
        "get_runtime_config_sync",
        lambda *_a, **_k: app_settings_service.RuntimeConfig(
            app_base_url=BASE_URL, scan_row_limit_default=1000, metrics_row_limit_default=1000
        ),
    )
    return box


@dataclass(frozen=True)
class World:
    project_id: uuid.UUID
    demo_project_id: uuid.UUID
    anna: uuid.UUID
    oleg: uuid.UUID
    ivan: uuid.UUID
    gone: uuid.UUID


def _user(session: Session, email: str, name: str) -> uuid.UUID:
    user = User(id=uuid.uuid4(), email=email, name=name, password_hash="x", role="editor")
    session.add(user)
    session.flush()
    return user.id


def _prefs(session: Session, user_id: uuid.UUID, mode: str, *, mentions: bool = True) -> None:
    session.add(UserNotificationPrefs(user_id=user_id, email_mode=mode, mentions_email=mentions))


def _world(session: Session) -> World:
    project = Project(id=uuid.uuid4(), name="Shop", slug=f"shop-{uuid.uuid4().hex[:8]}")
    demo = Project(id=uuid.uuid4(), name="Demo", slug=f"demo-{uuid.uuid4().hex[:8]}", is_demo=True)
    session.add_all([project, demo])
    session.flush()
    anna = _user(session, "anna@example.com", "Anna")  # instant
    oleg = _user(session, "oleg@example.com", "Oleg")  # no prefs row -> daily, mentions on
    ivan = _user(session, "ivan@example.com", "Ivan")  # weekly, mention emails off
    gone = _user(session, "gone@example.com", "Gone")  # instant, but no longer a member
    for member in (anna, oleg, ivan):
        session.add(ProjectMember(project_id=project.id, user_id=member, role="editor"))
        session.add(ProjectMember(project_id=demo.id, user_id=member, role="editor"))
    _prefs(session, anna, "instant")
    _prefs(session, ivan, "weekly", mentions=False)
    _prefs(session, gone, "instant")
    session.commit()
    return World(
        project_id=project.id, demo_project_id=demo.id, anna=anna, oleg=oleg, ivan=ivan, gone=gone
    )


def _note(
    session: Session,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    kind: str = "comment",
    title: str = "New comment on page_view",
    read: bool = False,
    age: timedelta = timedelta(minutes=5),
) -> uuid.UUID:
    now = datetime.now(UTC)
    row = Notification(
        id=uuid.uuid4(),
        user_id=user_id,
        project_id=project_id,
        kind=kind,
        entity_type="event",
        entity_id=uuid.uuid4(),
        title=title,
        body="Looks like the checkout step lost its property.",
        url="/p/shop/events/tracked/abc",
        read_at=now if read else None,
        created_at=now - age,
    )
    session.add(row)
    session.commit()
    return row.id


def _emailed(factory: sessionmaker[Session], row_id: uuid.UUID) -> datetime | None:
    with factory() as session:
        row = session.get(Notification, row_id)
        assert row is not None
        return row.emailed_at


def test_instant_sends_one_email_per_notification_to_instant_users_only(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    with factory() as session:
        world = _world(session)
        anna_note = _note(session, world.anna, world.project_id)
        oleg_note = _note(session, world.oleg, world.project_id)  # daily: not instant
        ivan_note = _note(session, world.ivan, world.project_id)  # weekly: not instant

    result = notification_email.send_notification_emails.run(
        [str(anna_note), str(oleg_note), str(ivan_note)]
    )

    assert result["sent"] == 1
    assert outbox.recipients() == ["anna@example.com"]
    mail = outbox.emails[0]
    assert mail["subject"] == "[Shop] New comment on page_view"
    assert f"{BASE_URL}/p/shop/events/tracked/abc" in str(mail["body"])
    assert _emailed(factory, anna_note) is not None
    assert _emailed(factory, oleg_note) is None
    assert _emailed(factory, ivan_note) is None


def test_instant_is_idempotent_across_enqueue_and_sweep(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    with factory() as session:
        world = _world(session)
        row = _note(session, world.anna, world.project_id)

    notification_email.send_notification_emails.run([str(row)])
    # The per-minute sweep (no ids) and a redelivered message find it stamped.
    notification_email.send_notification_emails.run(None)
    notification_email.send_notification_emails.run([str(row)])

    assert outbox.recipients() == ["anna@example.com"]


def test_mentions_follow_mentions_email_not_email_mode(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    with factory() as session:
        world = _world(session)
        oleg_mention = _note(session, world.oleg, world.project_id, kind="mention")
        ivan_mention = _note(session, world.ivan, world.project_id, kind="mention")

    notification_email.send_notification_emails.run(None)

    # Oleg (daily, defaults) still gets his mention at once; Ivan turned
    # mention emails off.
    assert outbox.recipients() == ["oleg@example.com"]
    assert _emailed(factory, ivan_mention) is None

    # Mention emails off means off: not in Ivan's weekly digest either.
    notification_email.send_notification_digest.run("weekly")
    assert outbox.recipients() == ["oleg@example.com"]
    assert _emailed(factory, oleg_mention) is not None


def test_non_members_and_demo_projects_are_never_emailed(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    with factory() as session:
        world = _world(session)
        gone_note = _note(session, world.gone, world.project_id)
        demo_note = _note(session, world.anna, world.demo_project_id)

    notification_email.send_notification_emails.run(None)

    assert outbox.emails == []
    assert _emailed(factory, gone_note) is None
    assert _emailed(factory, demo_note) is None


def test_membership_is_checked_at_send_time(factory: sessionmaker[Session], outbox: Outbox) -> None:
    with factory() as session:
        world = _world(session)
        row = _note(session, world.anna, world.project_id)
        membership = session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == world.project_id,
                ProjectMember.user_id == world.anna,
            )
        ).scalar_one()
        session.delete(membership)
        session.commit()

    notification_email.send_notification_emails.run([str(row)])

    assert outbox.emails == []


def test_daily_digest_groups_unread_unemailed_rows_per_user(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    with factory() as session:
        world = _world(session)
        first = _note(session, world.oleg, world.project_id, title="First")
        second = _note(session, world.oleg, world.project_id, title="Second")
        read = _note(session, world.oleg, world.project_id, title="Already read", read=True)
        stale = _note(session, world.oleg, world.project_id, title="Old", age=timedelta(days=9))
        weekly = _note(session, world.ivan, world.project_id, title="For the weekly")

    result = notification_email.send_notification_digest.run("daily")

    assert result["sent"] == 1
    assert outbox.recipients() == ["oleg@example.com"]
    body = str(outbox.emails[0]["body"])
    assert "First" in body and "Second" in body
    assert "Already read" not in body and "Old" not in body
    assert outbox.emails[0]["subject"] == "[tripl] 2 unread notifications"
    assert _emailed(factory, first) is not None
    assert _emailed(factory, second) is not None
    assert _emailed(factory, read) is None
    assert _emailed(factory, stale) is None
    assert _emailed(factory, weekly) is None

    # A second run the same day has nothing left to send.
    notification_email.send_notification_digest.run("daily")
    assert len(outbox.emails) == 1

    notification_email.send_notification_digest.run("weekly")
    assert outbox.recipients() == ["oleg@example.com", "ivan@example.com"]


def test_missing_smtp_skips_silently_and_stamps_nothing(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    outbox.smtp_host = ""
    with factory() as session:
        world = _world(session)
        row = _note(session, world.anna, world.project_id)

    result = notification_email.send_notification_emails.run([str(row)])

    assert result["status"] == "smtp_unavailable"
    assert outbox.emails == []
    assert _emailed(factory, row) is None

    # Configured later: the sweep picks the row up.
    outbox.smtp_host = "relay.example.com"
    notification_email.send_notification_emails.run(None)
    assert outbox.recipients() == ["anna@example.com"]


def test_failed_send_releases_the_claim_for_a_retry(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    outbox.fail_for = "anna@example.com"
    with factory() as session:
        world = _world(session)
        row = _note(session, world.anna, world.project_id)

    result = notification_email.send_notification_emails.run([str(row)])

    assert result["failed"] == 1
    assert _emailed(factory, row) is None

    outbox.fail_for = None
    notification_email.send_notification_emails.run(None)
    assert outbox.recipients() == ["anna@example.com"]
    assert _emailed(factory, row) is not None


def test_invalid_digest_mode_is_refused(outbox: Outbox) -> None:
    assert notification_email.send_notification_digest.run("hourly")["status"] == "invalid_mode"


def test_enqueue_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("broker down")

    monkeypatch.setattr(notification_email.send_notification_emails, "delay", _boom)
    notification_email.enqueue_notification_emails([uuid.uuid4()])
    notification_email.enqueue_notification_emails([])


def test_digest_mails_each_organization_through_its_own_relay(
    factory: sessionmaker[Session], outbox: Outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One user, rows in two organizations: one digest per organization, each
    through that organization's relay; an organization with no relay leaves its
    rows unsent (and unstamped) while the other's go out (F20 PR9)."""
    from tripl.models.organization import DEFAULT_ORG_ID, Organization

    other_org = uuid.uuid4()
    relays: dict[uuid.UUID, str] = {DEFAULT_ORG_ID: "default-relay.example.com", other_org: ""}
    monkeypatch.setattr(
        app_settings_service,
        "get_email_config_sync",
        lambda _session, *, org_id: _email_config(relays[org_id]),
    )
    with factory() as session:
        world = _world(session)
        session.add(Organization(id=other_org, slug="other", name="Other"))
        session.flush()
        elsewhere = Project(
            id=uuid.uuid4(), name="Elsewhere", slug="elsewhere", organization_id=other_org
        )
        session.add(elsewhere)
        session.add(ProjectMember(project_id=elsewhere.id, user_id=world.oleg, role="editor"))
        session.commit()
        here = _note(session, world.oleg, world.project_id, title="Here")
        there = _note(session, world.oleg, elsewhere.id, title="There")

    result = notification_email.send_notification_digest.run("daily")

    assert result["sent"] == 1
    assert [mail["smtp_host"] for mail in outbox.emails] == ["default-relay.example.com"]
    assert "Here" in str(outbox.emails[0]["body"])
    assert "There" not in str(outbox.emails[0]["body"])
    assert _emailed(factory, here) is not None
    assert _emailed(factory, there) is None

    # The other organization sets its relay: its row goes out through it, alone.
    relays[other_org] = "other-relay.example.com"
    notification_email.send_notification_digest.run("daily")
    assert [mail["smtp_host"] for mail in outbox.emails] == [
        "default-relay.example.com",
        "other-relay.example.com",
    ]
    assert "There" in str(outbox.emails[1]["body"])
    assert _emailed(factory, there) is not None
