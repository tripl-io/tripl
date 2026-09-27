"""Owner/notification fan-out is scoped to the project's own organization (F20 PR4).

Three fan-outs decide "who still reaches this project" without a request: the
alert-owner routing, the in-app notification recipients and the notification
email sweep. All three go through ``project_access.project_member_clause``: a
``project_members`` row, or owner/admin of the organization that owns the
project. An owner of ANOTHER organization, a plain member of the project's
organization without a row, and a platform admin who is no member at all get
nothing.

Driven against a file-backed SQLite database like ``test_owner_alert_routing``
and ``test_notification_email``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from tripl.config import SMTP_SECURITY_STARTTLS
from tripl.models import Base
from tripl.models.domain_enums import OrganizationRole
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.notification import Notification
from tripl.models.organization import Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.models.user_notification_prefs import UserNotificationPrefs
from tripl.services import alert_owner_routing, app_settings_service, notification_service
from tripl.worker.tasks import alerts, notification_email

BASE_URL = "https://tripl.example.com"


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(f"sqlite:///{tmp_path / 'org_role_fanout.db'}")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine, expire_on_commit=False)
    finally:
        engine.dispose()


@dataclass(frozen=True)
class World:
    project_id: uuid.UUID
    event_type_id: uuid.UUID
    a_admin: uuid.UUID  # admin of org A (the project's org), no project row
    a_owner: uuid.UUID  # owner of org A, no project row
    a_member: uuid.UUID  # plain member of org A, no project row
    a_editor: uuid.UUID  # plain member of org A with an editor row
    b_owner: uuid.UUID  # owner of org B only
    platform: uuid.UUID  # platform admin, member of no organization

    @property
    def everyone(self) -> list[uuid.UUID]:
        return [
            self.a_admin,
            self.a_owner,
            self.a_member,
            self.a_editor,
            self.b_owner,
            self.platform,
        ]

    @property
    def eligible(self) -> set[uuid.UUID]:
        return {self.a_admin, self.a_owner, self.a_editor}


def _org(session: Session, slug: str) -> uuid.UUID:
    org = Organization(id=uuid.uuid4(), slug=slug, name=slug.upper())
    session.add(org)
    session.flush()
    return org.id


def _user(
    session: Session,
    email: str,
    *,
    org_id: uuid.UUID | None = None,
    org_role: OrganizationRole | None = None,
    platform_admin: bool = False,
) -> uuid.UUID:
    user = User(
        id=uuid.uuid4(),
        email=email,
        name=email.split("@")[0],
        password_hash="x",
        is_platform_admin=platform_admin,
    )
    session.add(user)
    session.flush()
    if org_id is not None and org_role is not None:
        session.add(
            OrganizationMember(organization_id=org_id, user_id=user.id, role=org_role.value)
        )
    # Everyone wants instant email, so the sweep's membership check is the only filter.
    session.add(UserNotificationPrefs(user_id=user.id, email_mode="instant", mentions_email=True))
    session.flush()
    return user.id


def _world(session: Session) -> World:
    org_a = _org(session, f"a-{uuid.uuid4().hex[:6]}")
    org_b = _org(session, f"b-{uuid.uuid4().hex[:6]}")
    project = Project(
        id=uuid.uuid4(),
        name="Shop",
        slug=f"shop-{uuid.uuid4().hex[:8]}",
        organization_id=org_a,
    )
    other = Project(
        id=uuid.uuid4(),
        name="Other",
        slug=f"other-{uuid.uuid4().hex[:8]}",
        organization_id=org_b,
    )
    session.add_all([project, other])
    session.flush()
    event_type = EventType(id=uuid.uuid4(), project_id=project.id, name="page", display_name="Page")
    session.add(event_type)
    session.flush()

    a_admin = _user(session, "a-admin@example.com", org_id=org_a, org_role=OrganizationRole.admin)
    a_owner = _user(session, "a-owner@example.com", org_id=org_a, org_role=OrganizationRole.owner)
    a_member = _user(
        session, "a-member@example.com", org_id=org_a, org_role=OrganizationRole.member
    )
    a_editor = _user(
        session, "a-editor@example.com", org_id=org_a, org_role=OrganizationRole.member
    )
    b_owner = _user(session, "b-owner@example.com", org_id=org_b, org_role=OrganizationRole.owner)
    platform = _user(session, "platform@example.com", platform_admin=True)
    session.add(ProjectMember(project_id=project.id, user_id=a_editor, role="editor"))
    # The org-B owner holds a row in org B's project: rows elsewhere count for nothing.
    session.add(ProjectMember(project_id=other.id, user_id=b_owner, role="editor"))
    world = World(
        project_id=project.id,
        event_type_id=event_type.id,
        a_admin=a_admin,
        a_owner=a_owner,
        a_member=a_member,
        a_editor=a_editor,
        b_owner=b_owner,
        platform=platform,
    )
    for user_id in world.everyone:
        session.add(EventTypeOwner(event_type_id=event_type.id, user_id=user_id))
    session.commit()
    return world


# -- alert owner routing ---------------------------------------------------


def test_alert_owners_are_limited_to_the_projects_org_admins_and_row_holders(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        world = _world(session)
        [owners] = alert_owner_routing.resolve_owners_sync(
            session,
            world.project_id,
            [alert_owner_routing.OwnedScope("event_type", str(world.event_type_id))],
        )

    resolved = {contact.user_id for contact in owners}
    assert resolved == world.eligible
    assert world.b_owner not in resolved
    assert world.platform not in resolved
    assert world.a_member not in resolved


# -- in-app notifications --------------------------------------------------


def test_notify_reaches_only_the_projects_org_admins_and_row_holders(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        world = _world(session)
        notified = notification_service.notify_sync(
            session,
            project_id=world.project_id,
            kind="comment",
            entity_type="event_type",
            entity_id=world.event_type_id,
            title="New comment",
            url="/p/shop",
            user_ids=world.everyone,
            honour_mute=False,
        )
        session.commit()

    assert notified == world.eligible


def test_members_among_sync_matches_the_shared_statement(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        world = _world(session)
        among = notification_service._members_among_sync(
            session, world.project_id, set(world.everyone)
        )
        assert among == world.eligible
        assert notification_service._members_among_sync(session, world.project_id, set()) == set()


# -- notification email sweep ----------------------------------------------


@dataclass
class Outbox:
    recipients: list[str]


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch, factory: sessionmaker[Session]) -> Outbox:
    box = Outbox(recipients=[])

    def _send(**kwargs: object) -> None:
        box.recipients.append(str(list(kwargs["recipients"])[0]))  # type: ignore[call-overload]

    monkeypatch.setattr(notification_email, "_get_sync_session", factory)
    monkeypatch.setattr(alerts, "_send_email_message", _send)
    monkeypatch.setattr(
        app_settings_service,
        "get_email_config_sync",
        lambda *_a, **_k: app_settings_service.EmailConfig(
            smtp_host="relay.example.com",
            smtp_port=587,
            smtp_username="",
            smtp_password="",
            smtp_security=SMTP_SECURITY_STARTTLS,
            smtp_from_address="tripl@example.com",
        ),
    )
    monkeypatch.setattr(
        app_settings_service,
        "get_runtime_config_sync",
        lambda *_a, **_k: app_settings_service.RuntimeConfig(
            app_base_url=BASE_URL, scan_row_limit_default=1000, metrics_row_limit_default=1000
        ),
    )
    return box


def test_notification_email_goes_only_to_the_projects_org_admins_and_row_holders(
    factory: sessionmaker[Session], outbox: Outbox
) -> None:
    with factory() as session:
        world = _world(session)
        emails = {
            user_id: session.get(User, user_id).email  # type: ignore[union-attr]
            for user_id in world.everyone
        }
        now = datetime.now(UTC)
        # Rows written for everyone (as a stale grant would have left them): the
        # sweep re-checks membership at send time.
        for user_id in world.everyone:
            session.add(
                Notification(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    project_id=world.project_id,
                    kind="comment",
                    entity_type="event",
                    entity_id=uuid.uuid4(),
                    title="New comment",
                    body="",
                    url="/p/shop",
                    created_at=now - timedelta(minutes=5),
                )
            )
        session.commit()

    notification_email.send_notification_emails.run(None)

    assert sorted(outbox.recipients) == sorted(emails[user_id] for user_id in world.eligible)
    assert emails[world.b_owner] not in outbox.recipients
    assert emails[world.platform] not in outbox.recipients
