import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.schemas.field_definition import (
    FieldDefinitionBulkCreate,
    FieldDefinitionCreate,
    FieldDefinitionUpdate,
    FieldReorder,
)
from tripl.services.event_type_service import get_event_type
from tripl.services.scan_config_lookup import (
    name_format_conflict_detail,
    scan_configs_blocking_field_removal,
)
from tripl.services.search_service import reindex_project_branch


async def _on_main(session: AsyncSession, event_type: EventType) -> bool:
    """Whether ``event_type`` is a row of the live plan.

    Read off the row's own branch, not off the caller's ``branch_id``. That
    argument admits main's own id as well as ``None``, because
    ``resolve_branch_id`` accepts both. Keying the name-format guard on
    ``branch_id is None`` let ``?branch=<main id>`` delete a field a scan names
    events by from the live plan (tripl-0zpq.121). ``get_branch_id_override``
    now yields ``None`` for main, which closes the HTTP door. This keeps the
    guard, which prevents an outage rather than a stale cache, correct however
    a caller spells main.
    """
    branch = await session.get(PlanBranch, event_type.branch_id)
    return branch is not None and branch.kind == BranchKind.main.value


async def list_fields(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> list[FieldDefinition]:
    et = await get_event_type(session, slug, event_type_id, branch_id)
    result = await session.execute(
        select(FieldDefinition)
        .where(FieldDefinition.event_type_id == et.id)
        .order_by(FieldDefinition.order)
    )
    return list(result.scalars().all())


async def create_field(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    data: FieldDefinitionCreate,
    branch_id: uuid.UUID | None = None,
) -> FieldDefinition:
    et = await get_event_type(session, slug, event_type_id, branch_id)
    is_main = await _on_main(session, et)
    existing = await session.execute(
        select(FieldDefinition).where(
            FieldDefinition.event_type_id == et.id, FieldDefinition.name == data.name
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="Field with this name already exists for event type"
        )
    field = FieldDefinition(**data.model_dump(), event_type_id=et.id)
    session.add(field)
    await session.commit()
    await session.refresh(field)
    await reindex_project_branch(
        session,
        project_id=et.project_id,
        branch_id=et.branch_id,
        slug=slug,
    )
    if is_main:
        await cache.delete_prefix(cache.prefix_event_types(et.project_id))
    return field


async def bulk_create_fields(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    data: FieldDefinitionBulkCreate,
    branch_id: uuid.UUID | None = None,
) -> list[FieldDefinition]:
    """Create the supplied fields on an event type, skipping names that already
    exist. Idempotent so it can back a "create missing fields" action."""
    et = await get_event_type(session, slug, event_type_id, branch_id)
    is_main = await _on_main(session, et)
    existing = await session.execute(
        select(FieldDefinition).where(FieldDefinition.event_type_id == et.id)
    )
    existing_fields = list(existing.scalars().all())
    existing_names = {f.name for f in existing_fields}
    next_order = max((f.order for f in existing_fields), default=-1) + 1
    created = False
    seen: set[str] = set()
    for field_in in data.fields:
        if field_in.name in existing_names or field_in.name in seen:
            continue
        seen.add(field_in.name)
        payload = field_in.model_dump()
        payload["order"] = next_order
        next_order += 1
        session.add(FieldDefinition(**payload, event_type_id=et.id))
        created = True
    if created:
        await session.commit()
        await reindex_project_branch(
            session,
            project_id=et.project_id,
            branch_id=et.branch_id,
            slug=slug,
        )
        if is_main:
            await cache.delete_prefix(cache.prefix_event_types(et.project_id))
    return await list_fields(session, slug, event_type_id, branch_id)


async def update_field(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    field_id: uuid.UUID,
    data: FieldDefinitionUpdate,
    branch_id: uuid.UUID | None = None,
) -> FieldDefinition:
    et = await get_event_type(session, slug, event_type_id, branch_id)
    is_main = await _on_main(session, et)
    result = await session.execute(
        select(FieldDefinition).where(
            FieldDefinition.id == field_id, FieldDefinition.event_type_id == event_type_id
        )
    )
    field = result.scalar_one_or_none()
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    update_data = data.model_dump(exclude_unset=True)
    if "contract_max_bad_rate" in update_data and update_data["contract_max_bad_rate"] is None:
        update_data["contract_max_bad_rate"] = 0.0
    contract_min_value = update_data.get("contract_min_value", field.contract_min_value)
    contract_max_value = update_data.get("contract_max_value", field.contract_max_value)
    if (
        contract_min_value is not None
        and contract_max_value is not None
        and contract_min_value > contract_max_value
    ):
        raise HTTPException(
            status_code=422,
            detail="contract_min_value must be <= contract_max_value",
        )
    for key, value in update_data.items():
        setattr(field, key, value)
    await session.commit()
    await session.refresh(field)
    await reindex_project_branch(
        session,
        project_id=et.project_id,
        branch_id=et.branch_id,
        slug=slug,
    )
    if is_main:
        await cache.delete_prefix(cache.prefix_event_types(et.project_id))
    return field


async def _reject_if_a_scan_names_events_by(
    session: AsyncSession,
    *,
    event_type: EventType,
    field_name: str,
) -> None:
    """409 when deleting this field would leave a scan unable to name its events.

    The SECOND door to the tripl-lpin outage. Accepting a ``missing_field`` drift
    is guarded in ``schema_drift_service``; this is the same deletion reached from
    the plan UI in one click, with the same consequence — ``generate_events``
    assembles its format arguments only from columns that still have a
    FieldDefinition, so removing the one ``{action}`` names kills every collection
    with "the event name format references unknown keys" (tripl-3mmh).

    **409, deliberately**, the status the drift door already returns: this is not a
    malformed request (422) and not a permission problem (403) — the field is in a
    state that conflicts with the deletion, exactly like the 409 this module
    already returns for a duplicate field name. Two doors, one status, one message
    body from ``name_format_conflict_detail``, so an operator meets one rule.

    **No ``force`` escape hatch**, unlike the drift door, for two reasons. The
    drift override is non-reflexive only because it requires a ``note`` that lands
    in the drift's audit record; a deleted FieldDefinition leaves no record for a
    note to land in, so ``force`` here would be the bare click-through the drift
    door refused to build. And nobody is cornered: an open drift row nags until
    it is resolved one way or another, whereas a field an operator merely wanted
    to tidy can stay. The genuine over-fire (a project-wide grouped config that
    names the column but never scans this event type) still has its release valve
    on the drift door, which is where the note can be recorded.
    """
    blocking = await scan_configs_blocking_field_removal(
        session,
        project_id=event_type.project_id,
        event_type_id=event_type.id,
        field_name=field_name,
    )
    if not blocking:
        return
    raise HTTPException(
        status_code=409,
        detail=name_format_conflict_detail(
            field_name=field_name,
            configs=blocking,
            lead="Cannot delete this field.",
            then="delete the field",
        ),
    )


async def delete_field(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    field_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> None:
    et = await get_event_type(session, slug, event_type_id, branch_id)
    is_main = await _on_main(session, et)
    result = await session.execute(
        select(FieldDefinition).where(
            FieldDefinition.id == field_id, FieldDefinition.event_type_id == event_type_id
        )
    )
    field = result.scalar_one_or_none()
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    # Main only. A branch is where you PLAN a removal, and deleting a field there
    # changes no live collection — no scan reads a branch's plan. Guarding it
    # would make the branch useless for staging "drop the column and rewrite the
    # scan's name format", which is the repair this guard tells operators to do.
    # The branch's deletion becomes real when it merges, and the merge has its
    # own guard (``plan_branch_merge_service``) — the third door.
    if is_main:
        await _reject_if_a_scan_names_events_by(session, event_type=et, field_name=field.name)
    await session.delete(field)
    await session.commit()
    await reindex_project_branch(
        session,
        project_id=et.project_id,
        branch_id=et.branch_id,
        slug=slug,
    )
    if is_main:
        await cache.delete_prefix(cache.prefix_event_types(et.project_id))


async def reorder_fields(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    data: FieldReorder,
    branch_id: uuid.UUID | None = None,
) -> list[FieldDefinition]:
    et = await get_event_type(session, slug, event_type_id, branch_id)
    is_main = await _on_main(session, et)
    for idx, field_id in enumerate(data.field_ids):
        result = await session.execute(
            select(FieldDefinition).where(
                FieldDefinition.id == field_id, FieldDefinition.event_type_id == event_type_id
            )
        )
        field = result.scalar_one_or_none()
        if field:
            field.order = idx
    await session.commit()
    if is_main:
        await cache.delete_prefix(cache.prefix_event_types(et.project_id))
    return await list_fields(session, slug, event_type_id, branch_id)
