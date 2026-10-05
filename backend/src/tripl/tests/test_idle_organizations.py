"""Organizations nobody uses are retired on a hosted instance."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from tripl.config import settings
from tripl.models.audit_log import AuditLog
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.models.user_session import UserSession
from tripl.services import org_idle_service
from tripl.tests._tenancy import use_multi_tenant
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import org_delete

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=60)
RECENTLY = NOW - timedelta(days=2)


@pytest.fixture(autouse=True)
def hosted_with_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    use_multi_tenant(monkeypatch)
    monkeypatch.setattr(settings, "idle_org_retention_days", 30)


async def _org(
    *,
    created: datetime = LONG_AGO,
    signed_in: datetime | None = None,
    opened: datetime | None = None,
) -> uuid.UUID:
    """An organization with one member; a session and a demo project when asked."""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as session:
        org = Organization(slug=f"sandbox-{suffix}", name="Sandbox", created_at=created)
        user = User(email=f"{suffix}@example.com", name="Visitor", password_hash="x")
        session.add_all([org, user])
        await session.flush()
        session.add(OrganizationMember(organization_id=org.id, user_id=user.id, role="owner"))
        if signed_in is not None:
            session.add(
                UserSession(
                    user_id=user.id,
                    session_token_hash=uuid.uuid4().hex,
                    expires_at=signed_in + timedelta(days=30),
                    created_at=signed_in,
                    updated_at=signed_in,
                )
            )
        if opened is not None:
            session.add(
                Project(
                    organization_id=org.id,
                    name="Demo",
                    slug=f"demo-{suffix}",
                    is_demo=True,
                    demo_last_accessed_at=opened,
                )
            )
        await session.commit()
        return org.id


async def _status(org_id: uuid.UUID) -> str | None:
    async with TestSessionLocal() as session:
        status: str | None = await session.scalar(
            select(Organization.status).where(Organization.id == org_id)
        )
        return status


async def test_only_organizations_left_idle_are_retired() -> None:
    abandoned = await _org(signed_in=LONG_AGO, opened=LONG_AGO)
    never_used = await _org()
    signed_in = await _org(signed_in=RECENTLY)
    opened = await _org(signed_in=LONG_AGO, opened=RECENTLY)
    brand_new = await _org(created=RECENTLY)

    async with TestSessionLocal() as session:
        retired = await org_idle_service.retire_idle_organizations(session, now=NOW)

    assert set(retired) == {abandoned, never_used}
    assert DEFAULT_ORG_ID not in retired
    assert await _status(abandoned) == "deleting"
    for kept in (signed_in, opened, brand_new):
        assert await _status(kept) == "active"


async def test_a_colleague_keeps_an_idle_owners_organization_alive() -> None:
    org_id = await _org(signed_in=LONG_AGO, opened=LONG_AGO)
    colleague_id = await _account(org_id=org_id, signed_in=RECENTLY)

    async with TestSessionLocal() as session:
        assert org_id not in await org_idle_service.retire_idle_organizations(session, now=NOW)
    assert await _status(org_id) == "active"

    # The same organization becomes eligible only after every member is idle.
    async with TestSessionLocal() as session:
        await session.execute(
            update(UserSession)
            .where(UserSession.user_id == colleague_id)
            .values(updated_at=LONG_AGO)
        )
        await session.commit()
        assert org_id in await org_idle_service.retire_idle_organizations(session, now=NOW)
    assert await _status(org_id) == "deleting"


@pytest.mark.parametrize(
    ("mode", "days"), [("hosted", 0), ("self_hosted", 30)], ids=["off", "self-hosted"]
)
async def test_nothing_is_retired_unless_asked_for_on_a_hosted_instance(
    monkeypatch: pytest.MonkeyPatch, mode: str, days: int
) -> None:
    monkeypatch.setattr(settings, "deployment_mode", mode)
    monkeypatch.setattr(settings, "idle_org_retention_days", days)
    org_id = await _org()

    async with TestSessionLocal() as session:
        assert await org_idle_service.retire_idle_organizations(session, now=NOW) == []
    assert await _status(org_id) == "active"


def test_the_task_queues_the_owner_delete_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    idle = [uuid.uuid4(), uuid.uuid4()]

    async def _retire(_session: Any) -> list[uuid.UUID]:
        return idle

    async def _no_db(run: Any) -> None:
        await run(None)

    async def _orphans(_session: Any) -> int:
        return 3

    queued: list[str] = []
    monkeypatch.setattr(org_idle_service, "retire_idle_organizations", _retire)
    monkeypatch.setattr(org_idle_service, "delete_orphan_accounts", _orphans)
    monkeypatch.setitem(
        org_delete.retire_idle_organizations.run.__globals__,
        "run_with_async_worker_session",
        _no_db,
    )
    monkeypatch.setattr(org_delete.purge_organization, "delay", queued.append)

    result = org_delete.retire_idle_organizations.run()

    assert queued == [str(org_id) for org_id in idle]
    assert result == {"retired": queued, "accounts_deleted": 3}
    from tripl.worker.celery_app import celery_app

    assert any(
        entry["task"] == "tripl.worker.tasks.org_delete.retire_idle_organizations"
        for entry in celery_app.conf.beat_schedule.values()
    )


async def _account(
    *,
    created: datetime = LONG_AGO,
    signed_in: datetime | None = None,
    org_id: uuid.UUID | None = None,
    platform_admin: bool = False,
) -> uuid.UUID:
    """An account, optionally in an organization, with a session when asked."""
    suffix = uuid.uuid4().hex[:8]
    async with TestSessionLocal() as session:
        user = User(
            email=f"acct-{suffix}@example.com",
            name="Visitor",
            password_hash="x",
            created_at=created,
            is_platform_admin=platform_admin,
        )
        session.add(user)
        await session.flush()
        if org_id is not None:
            session.add(OrganizationMember(organization_id=org_id, user_id=user.id, role="member"))
        if signed_in is not None:
            session.add(
                UserSession(
                    user_id=user.id,
                    session_token_hash=uuid.uuid4().hex,
                    expires_at=signed_in + timedelta(days=30),
                    created_at=signed_in,
                    updated_at=signed_in,
                )
            )
        session.add(
            AuditLog(
                user_id=user.id,
                user_email=user.email,
                action="user.login",
                target_type="user",
                target_id=user.id,
            )
        )
        await session.commit()
        return user.id


async def test_accounts_left_in_no_organization_are_deleted() -> None:
    orphan = await _account(signed_in=LONG_AGO)
    never_signed_in = await _account()
    member = await _account(org_id=DEFAULT_ORG_ID)
    recent = await _account(signed_in=RECENTLY)
    new = await _account(created=RECENTLY)
    admin = await _account(platform_admin=True)

    async with TestSessionLocal() as session:
        assert await org_idle_service.delete_orphan_accounts(session, now=NOW) == 2

    async with TestSessionLocal() as session:
        left = set(await session.scalars(select(User.id)))
        assert orphan not in left and never_signed_in not in left
        assert {member, recent, new, admin} <= left
        # The audit trail stays, without the deleted accounts' addresses.
        rows = list(
            await session.execute(
                select(AuditLog.user_id, AuditLog.user_email).where(
                    AuditLog.target_id.in_([orphan, never_signed_in])
                )
            )
        )
        assert rows and all(user_id is None and email == "" for user_id, email in rows)
        sessions = await session.scalar(select(UserSession.id).where(UserSession.user_id == orphan))
        assert sessions is None


async def test_no_account_is_deleted_unless_the_sweep_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "idle_org_retention_days", 0)
    orphan = await _account()
    async with TestSessionLocal() as session:
        assert await org_idle_service.delete_orphan_accounts(session, now=NOW) == 0
        assert await session.get(User, orphan) is not None
