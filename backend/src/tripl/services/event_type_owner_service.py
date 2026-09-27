"""Per-event-type stakeholder ownership.

Owners are attached to live (main-branch) event types. They gate plan-branch
merges: a branch that touches an owned event type requires an approval from
at least one of its owners. Owners are never deep-copied onto working branches
— they are runtime metadata about the live plan, not part of the design canvas.
"""

from __future__ import annotations

import uuid
from typing import cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.user import User
from tripl.schemas.event_type_owner import EventTypeOwnerResponse
from tripl.services import subscription_service
from tripl.services.project_access import member_role
from tripl.services.project_lookup import get_project_id_by_slug
from tripl.services.project_member_service import NOT_A_MEMBER_DETAIL


async def _resolve_main_event_type(
    session: AsyncSession, slug: str, event_type_id: uuid.UUID
) -> EventType:
    """Look up an event type by id and confirm it lives on the live main branch.

    Owners only attach to the live plan, so trying to manage owners on a
    working-branch deep-copy is rejected with 404 (the id resolves but does not
    point at a main row in this project).
    """
    row = await session.execute(
        select(EventType, PlanBranch, Project)
        .join(PlanBranch, EventType.branch_id == PlanBranch.id)
        .join(Project, EventType.project_id == Project.id)
        .where(EventType.id == event_type_id, Project.slug == slug)
    )
    hit = row.first()
    if hit is None:
        raise HTTPException(status_code=404, detail="Event type not found")
    et, branch, _project = hit
    if branch.kind != BranchKind.main.value:
        raise HTTPException(
            status_code=400,
            detail="Owners can be managed only on the live (main) event type",
        )
    return cast(EventType, et)


async def _serialize(owner: EventTypeOwner, user: User) -> EventTypeOwnerResponse:
    return EventTypeOwnerResponse(
        id=owner.id,
        event_type_id=owner.event_type_id,
        user_id=owner.user_id,
        user_email=user.email,
        user_name=user.name or user.email,
        granted_by=owner.granted_by,
        created_at=owner.created_at,
    )


async def list_owners(
    session: AsyncSession, slug: str, event_type_id: uuid.UUID
) -> list[EventTypeOwnerResponse]:
    await _resolve_main_event_type(session, slug, event_type_id)
    rows = await session.execute(
        select(EventTypeOwner, User)
        .join(User, EventTypeOwner.user_id == User.id)
        .where(EventTypeOwner.event_type_id == event_type_id)
        .order_by(EventTypeOwner.created_at.asc())
    )
    return [await _serialize(owner, user) for owner, user in rows.all()]


async def list_project_owners(session: AsyncSession, slug: str) -> list[EventTypeOwnerResponse]:
    """Every owner of every live event type in the project, in ONE query.

    The event-type list shows each type's owners and derives its merge-gate
    status from them; asking ``list_owners`` once per type made that page one
    request per event type (PLAN-42). Grouped by event type, then oldest grant
    first, the order ``list_owners`` returns one type's owners in.
    """
    project_id = await get_project_id_by_slug(session, slug)  # 404 for an unknown project
    rows = await session.execute(
        select(EventTypeOwner, User)
        .join(User, EventTypeOwner.user_id == User.id)
        .join(EventType, EventTypeOwner.event_type_id == EventType.id)
        .join(PlanBranch, EventType.branch_id == PlanBranch.id)
        .where(EventType.project_id == project_id, PlanBranch.kind == BranchKind.main.value)
        .order_by(EventTypeOwner.event_type_id, EventTypeOwner.created_at.asc())
    )
    return [await _serialize(owner, user) for owner, user in rows.all()]


async def add_owner(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    user_id: uuid.UUID,
    granted_by: uuid.UUID | None,
) -> EventTypeOwnerResponse:
    event_type = await _resolve_main_event_type(session, slug, event_type_id)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # An owner gates merges with their approval, so they must be able to see the
    # project; the instance owner counts, as they see every project.
    if await member_role(session, user, event_type.project_id) is None:
        raise HTTPException(status_code=422, detail=NOT_A_MEMBER_DETAIL)
    existing = await session.scalar(
        select(EventTypeOwner).where(
            EventTypeOwner.event_type_id == event_type_id,
            EventTypeOwner.user_id == user_id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="User is already an owner")
    owner = EventTypeOwner(
        event_type_id=event_type_id,
        user_id=user_id,
        granted_by=granted_by,
    )
    session.add(owner)
    # An owner hears about their type's events: open questions, signals (#259).
    await subscription_service.subscribe(
        session,
        user_id=user_id,
        project_id=event_type.project_id,
        entity_type=subscription_service.EVENT_TYPE,
        entity_id=event_type_id,
        reason="owner",
    )
    await session.commit()
    await session.refresh(owner)
    return await _serialize(owner, user)


async def remove_owner(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> None:
    await _resolve_main_event_type(session, slug, event_type_id)
    owner = await session.get(EventTypeOwner, owner_id)
    if owner is None or owner.event_type_id != event_type_id:
        raise HTTPException(status_code=404, detail="Owner not found")
    await subscription_service.drop_reason(
        session,
        user_id=owner.user_id,
        entity_type=subscription_service.EVENT_TYPE,
        entity_id=event_type_id,
        reason="owner",
    )
    await session.delete(owner)
    await session.commit()


async def load_owner_user_ids(
    session: AsyncSession, event_type_ids: list[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Bulk lookup: event_type_id → set of owner user_ids.

    Returns empty dict if no ids given. Used by merge gating to find owners of
    every event type touched by the branch (matched live rows on main).
    """
    if not event_type_ids:
        return {}
    rows = await session.execute(
        select(EventTypeOwner.event_type_id, EventTypeOwner.user_id).where(
            EventTypeOwner.event_type_id.in_(event_type_ids)
        )
    )
    out: dict[uuid.UUID, set[uuid.UUID]] = {}
    for et_id, user_id in rows.all():
        out.setdefault(et_id, set()).add(user_id)
    return out
