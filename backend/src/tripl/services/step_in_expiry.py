"""Recording a read-only step-in's natural expiry (F20 PR14).

A step-in ends in one of three ways: its admin ends it (``end_step_in``), a
second step-in to the same organization supersedes it (``start_step_in``), or
it runs out. The first two write ``platform.step_in_end`` as they happen; the
third has no moment of its own, so it is recorded lazily — the first time org
resolution or a step-in listing sees an expired step-in that was never ended,
it sets ``ended_at`` (to ``expires_at``, when it actually stopped counting) and
files ``platform.step_in_end`` with ``{"expired": true}`` in the target
organization.

Idempotent: each row is closed by a compare-and-set on ``ended_at IS NULL``,
and only the request whose update matched files the audit row, so two
concurrent requests cannot record one expiry twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.organization import Organization
from tripl.models.platform_step_in import PlatformStepIn
from tripl.models.user import User
from tripl.services import audit_service


async def close_expired_step_ins(
    session: AsyncSession, user: User, *, org_id: uuid.UUID | None = None
) -> int:
    """Close and audit ``user``'s expired, un-ended step-ins; how many. Commits if any.

    ``org_id`` narrows it to one organization (org resolution). Call it before
    loading any :class:`PlatformStepIn` into the session: the update does not
    synchronize loaded instances. Commits only when it closed something, so a
    request that sees nothing expired keeps its transaction untouched.
    """
    now = datetime.now(UTC)
    statement = (
        select(
            PlatformStepIn.id,
            PlatformStepIn.organization_id,
            PlatformStepIn.expires_at,
            Organization.slug,
        )
        .join(Organization, Organization.id == PlatformStepIn.organization_id)
        .where(
            PlatformStepIn.user_id == user.id,
            PlatformStepIn.ended_at.is_(None),
            PlatformStepIn.expires_at <= now,
        )
        .order_by(PlatformStepIn.expires_at, PlatformStepIn.id)
    )
    if org_id is not None:
        statement = statement.where(PlatformStepIn.organization_id == org_id)
    rows = (await session.execute(statement)).all()
    closed = 0
    for step_in_id, organization_id, expires_at, slug in rows:
        result = await session.execute(
            update(PlatformStepIn)
            .where(PlatformStepIn.id == step_in_id, PlatformStepIn.ended_at.is_(None))
            .values(ended_at=expires_at)
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            # Another request recorded it first.
            continue
        await audit_service.record(
            session,
            user=user,
            action="platform.step_in_end",
            target_type="organization",
            target_id=organization_id,
            target_name=slug,
            payload={"step_in_id": str(step_in_id), "expired": True},
            organization_id=organization_id,
            commit=False,
        )
        closed += 1
    if closed:
        await session.commit()
    return closed
