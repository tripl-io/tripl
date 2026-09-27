import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.schemas.event_type import EventTypeCreate, EventTypeResponse, EventTypeUpdate
from tripl.services._branch_event_threads import rescue_branch_event_threads
from tripl.services._event_reference_cleanup import drop_dangling_event_references
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.scan_config_lookup import (
    event_type_binding_conflict_detail,
    governing_name_format,
    load_governing_scan_configs_by_type,
    scan_configs_binding_event_types,
)
from tripl.services.search_service import reindex_project_branch


async def list_event_types(
    session: AsyncSession, slug: str, branch_id: uuid.UUID | None = None
) -> list[EventTypeResponse]:
    use_cache = branch_id is None
    project_id = await resolve_project_id(session, slug)
    if use_cache:
        cached = await cache.get_json(cache.key_event_types_list(project_id))
        if cached is not None:
            return [EventTypeResponse.model_validate(item) for item in cached]

    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(EventType)
        .where(EventType.project_id == project_id, EventType.branch_id == branch_id)
        .order_by(EventType.order, EventType.created_at)
        .limit(1000)  # defensive cap; realistic projects have <100 event types
    )
    rows = list(result.scalars().all())
    await attach_event_name_formats(session, project_id=project_id, event_types=rows)
    responses = [EventTypeResponse.model_validate(et) for et in rows]
    if use_cache:
        await cache.set_json(
            cache.key_event_types_list(project_id),
            [r.model_dump(mode="json") for r in responses],
            ttl_seconds=300,
        )
    return responses


async def get_event_type(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> EventType:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(EventType).where(
            EventType.id == event_type_id,
            EventType.project_id == project_id,
            EventType.branch_id == branch_id,
        )
    )
    et = result.scalar_one_or_none()
    if not et:
        raise HTTPException(status_code=404, detail="Event type not found")
    return et


async def read_event_type(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> EventType:
    """``get_event_type`` plus the resolved naming rule, for the GET route.

    Kept apart from ``get_event_type`` because every mutation path loads its
    type through that one and none of them read the rule; three extra queries
    per field edit would buy nothing.
    """
    et = await get_event_type(session, slug, event_type_id, branch_id)
    await attach_event_name_formats(session, project_id=et.project_id, event_types=[et])
    return et


async def attach_event_name_formats(
    session: AsyncSession, *, project_id: uuid.UUID, event_types: list[EventType]
) -> None:
    """Set ``event_name_format`` on each ORM row the way ``event.warnings`` is set.

    The rule is resolved server-side — through the type's main counterpart for a
    branch copy — so every client reads ONE answer instead of re-deriving it
    from the scan config list, which is how the authoring form silently lost
    the rule on branches (tripl-kjhi.1).
    """
    if not event_types:
        return
    configs_by_type = await load_governing_scan_configs_by_type(
        session, project_id=project_id, event_type_ids=[et.id for et in event_types]
    )
    for et in event_types:
        et.event_name_format = governing_name_format(  # type: ignore[attr-defined]
            configs_by_type.get(et.id, [])
        )


async def create_event_type(
    session: AsyncSession,
    slug: str,
    data: EventTypeCreate,
    branch_id: uuid.UUID | None = None,
) -> EventType:
    is_main = branch_id is None
    requested_branch_id = branch_id
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    existing = await session.execute(
        select(EventType).where(
            EventType.project_id == project_id,
            EventType.branch_id == branch_id,
            EventType.name == data.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="Event type with this name already exists in project"
        )
    payload = data.model_dump(exclude={"field_definitions"})
    et = EventType(**payload, project_id=project_id, branch_id=branch_id)
    session.add(et)
    await session.flush()
    seen_names: set[str] = set()
    for idx, fd in enumerate(data.field_definitions):
        if fd.name in seen_names:
            raise HTTPException(
                status_code=409, detail=f"Duplicate field name {fd.name!r} in event type"
            )
        seen_names.add(fd.name)
        fd_payload = fd.model_dump()
        fd_payload["order"] = fd.order or idx
        session.add(FieldDefinition(**fd_payload, event_type_id=et.id))
    await session.commit()
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    if is_main:
        await cache.delete_prefix(cache.prefix_event_types(project_id))
        # Event types feed ProjectsPage summary counts; bust those too.
        await cache.delete_prefix(cache.prefix_projects())
    # Re-fetch so the selectin field_definitions relationship is populated for the response.
    return await get_event_type(session, slug, et.id, requested_branch_id)


async def update_event_type(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    data: EventTypeUpdate,
    branch_id: uuid.UUID | None = None,
) -> EventType:
    is_main = branch_id is None
    et = await get_event_type(session, slug, event_type_id, branch_id)
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(et, key, value)
    await session.commit()
    await session.refresh(et)
    await reindex_project_branch(
        session,
        project_id=et.project_id,
        branch_id=et.branch_id,
        slug=slug,
    )
    if is_main:
        await cache.delete_prefix(cache.prefix_event_types(et.project_id))
    return et


async def delete_event_type(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> None:
    is_main = branch_id is None
    et = await get_event_type(session, slug, event_type_id, branch_id)
    project_id = et.project_id
    resolved_branch_id = et.branch_id
    # Refused rather than repaired: the FK is ON DELETE SET NULL, so this delete
    # would succeed and leave every scan that named the type bound to nothing —
    # a scan that still lists and still runs and collects no events at all
    # (tripl-0zpq.254). ScanConfig has no disabled flag to set the way
    # ``delete_scan_config`` disables its rules, and only the operator knows
    # whether the scan should be rebound, made grouped, or deleted.
    #
    # In practice this fires on a MAIN type, because the scan settings form only
    # offers main's types — but that is a fact about the form, not an invariant:
    # ``scan_configs.event_type_id`` is a plain FK with no branch or project
    # term, so a config pointed at a branch copy's id refuses that copy's delete
    # too. Correctly, for the same reason: the binding would be emptied either
    # way. Scoped to THIS project (see the helper's docstring) so the 409 can
    # never name a scan the reader cannot see, let alone edit.
    binding = await scan_configs_binding_event_types(
        session, project_id=project_id, event_type_ids=[et.id]
    )
    bound = binding.get(et.id, [])
    if bound:
        raise HTTPException(
            status_code=409,
            detail=event_type_binding_conflict_detail(
                configs=bound,
                lead="This event type cannot be deleted.",
                then="delete the event type",
            ),
        )
    # The most common door, and the least visible one: EventType maps no
    # ``events`` relationship, so its events go purely through the database FK
    # cascade and no service ever sees them being deleted. Their dangling
    # references have to be cleared here or nowhere (tripl-xjuv).
    doomed_event_ids = list(
        (await session.execute(select(Event.id).where(Event.event_type_id == et.id)))
        .scalars()
        .all()
    )
    await drop_dangling_event_references(session, project_id=project_id, event_ids=doomed_event_ids)
    # Same door for the events' discussions: on a branch, a row whose main twin
    # shows its thread hands it over before the cascade (tripl-0zpq.289).
    await rescue_branch_event_threads(session, project_id=project_id, event_ids=doomed_event_ids)
    await session.delete(et)
    await session.commit()
    await reindex_project_branch(
        session,
        project_id=project_id,
        branch_id=resolved_branch_id,
        slug=slug,
    )
    if is_main:
        await cache.delete_prefix(cache.prefix_event_types(project_id))
        await cache.delete_prefix(cache.prefix_projects())
