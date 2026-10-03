"""Retire organizations nobody uses any more (tripl-sav5.5).

A public demo gives every visitor an organization, and most visitors look once
and leave. :func:`retire_idle_organizations` marks the ones left idle for
``IDLE_ORG_RETENTION_DAYS`` as ``deleting``; the caller then queues the same
purge an owner's delete runs, and the hourly stranded-deletion chaser covers a
purge that is lost.

"Idle" is judged from what a visitor leaves behind when they use the product:
a sign-in (a session row touched) and a demo project opened (its
``demo_last_accessed_at``), both against the cutoff. A new organization is
never idle, and the default organization is never touched.

:func:`delete_orphan_accounts` then removes the accounts those purges leave in
no organization (tripl-sav5.8).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.models.audit_log import AuditLog
from tripl.models.domain_enums import OrganizationStatus
from tripl.models.organization import DEFAULT_ORG_ID, Organization, OrganizationMember
from tripl.models.project import Project
from tripl.models.user import User
from tripl.models.user_session import UserSession


def retention_cutoff(now: datetime | None = None) -> datetime | None:
    """The instant before which activity no longer counts; None when the sweep is off."""
    days = settings.idle_org_retention_days
    if days <= 0 or settings.deployment_mode != DEPLOYMENT_HOSTED:
        return None
    return (now if now is not None else datetime.now(UTC)) - timedelta(days=days)


async def retire_idle_organizations(
    session: AsyncSession, *, now: datetime | None = None
) -> list[uuid.UUID]:
    """Mark every idle organization ``deleting`` and return their ids. Commits."""
    cutoff = retention_cutoff(now)
    if cutoff is None:
        return []
    signed_in = exists().where(
        OrganizationMember.organization_id == Organization.id,
        UserSession.user_id == OrganizationMember.user_id,
        UserSession.updated_at >= cutoff,
    )
    opened = exists().where(
        Project.organization_id == Organization.id,
        Project.demo_last_accessed_at >= cutoff,
    )
    ids = list(
        (
            await session.scalars(
                select(Organization.id).where(
                    Organization.status == OrganizationStatus.active.value,
                    Organization.id != DEFAULT_ORG_ID,
                    Organization.created_at < cutoff,
                    ~signed_in,
                    ~opened,
                )
            )
        ).all()
    )
    if ids:
        await session.execute(
            update(Organization)
            .where(
                Organization.id.in_(ids),
                # Only what is still active: a suspension in between wins.
                Organization.status == OrganizationStatus.active.value,
            )
            .values(status=OrganizationStatus.deleting.value, updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return ids


async def delete_orphan_accounts(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete accounts left in no organization and unused since the cutoff (tripl-sav5.8).

    An idle organization's purge removes its memberships but not its members'
    accounts, so a public demo would keep every visitor's email and name for
    good. Deleted here: an account that belongs to no organization, is not a
    platform admin, was created before the cutoff and has no session touched
    since it. Every foreign key onto ``users`` cascades or sets NULL, so the
    rows that point at it go with it or keep pointing at nobody; the audit log's
    copy of the address is blanked first, since a NULL ``user_id`` would leave
    the email behind. Returns how many were deleted. Commits.
    """
    cutoff = retention_cutoff(now)
    if cutoff is None:
        return 0
    member = exists().where(OrganizationMember.user_id == User.id)
    recent_session = exists().where(
        UserSession.user_id == User.id,
        UserSession.updated_at >= cutoff,
    )
    ids = list(
        (
            await session.scalars(
                select(User.id).where(
                    ~member,
                    ~recent_session,
                    User.is_platform_admin.is_(False),
                    User.created_at < cutoff,
                )
            )
        ).all()
    )
    if not ids:
        return 0
    await session.execute(
        update(AuditLog)
        .where(AuditLog.user_id.in_(ids))
        .values(user_email="")
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        delete(User).where(User.id.in_(ids), ~member).execution_options(synchronize_session=False)
    )
    await session.commit()
    return len(ids)
