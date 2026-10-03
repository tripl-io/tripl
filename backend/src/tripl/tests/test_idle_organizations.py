"""Organizations nobody uses are retired on a hosted instance (tripl-sav5.5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from tripl.config import settings
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.models.user_session import UserSession
from tripl.services import org_idle_service
from tripl.tests.conftest import TestSessionLocal
from tripl.worker.tasks import org_delete

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=60)
RECENTLY = NOW - timedelta(days=2)


@pytest.fixture(autouse=True)
def hosted_with_retention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
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

    queued: list[str] = []
    monkeypatch.setattr(org_idle_service, "retire_idle_organizations", _retire)
    monkeypatch.setitem(
        org_delete.retire_idle_organizations.run.__globals__,
        "run_with_async_worker_session",
        _no_db,
    )
    monkeypatch.setattr(org_delete.purge_organization, "delay", queued.append)

    result = org_delete.retire_idle_organizations.run()

    assert queued == [str(org_id) for org_id in idle]
    assert result == {"retired": queued}
    from tripl.worker.celery_app import celery_app

    assert any(
        entry["task"] == "tripl.worker.tasks.org_delete.retire_idle_organizations"
        for entry in celery_app.conf.beat_schedule.values()
    )
