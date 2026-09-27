import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.models.domain_enums import MetaFieldType
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.schemas.meta_field import (
    MULTI_VALUE_FIELD_TYPES,
    MetaFieldCreate,
    MetaFieldResponse,
    MetaFieldUpdate,
    MetaFieldUsageResponse,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.search_service import reindex_project_branch


async def list_meta_fields(
    session: AsyncSession, slug: str, branch_id: uuid.UUID | None = None
) -> list[MetaFieldResponse]:
    use_cache = branch_id is None
    project_id = await resolve_project_id(session, slug)
    if use_cache:
        cached = await cache.get_json(cache.key_meta_fields_list(project_id))
        if cached is not None:
            return [MetaFieldResponse.model_validate(item) for item in cached]

    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(MetaFieldDefinition)
        .where(
            MetaFieldDefinition.project_id == project_id,
            MetaFieldDefinition.branch_id == branch_id,
        )
        .order_by(MetaFieldDefinition.order)
        .limit(1000)  # defensive cap; realistic projects have <50 meta fields
    )
    rows = list(result.scalars().all())
    responses = [MetaFieldResponse.model_validate(mf) for mf in rows]
    if use_cache:
        await cache.set_json(
            cache.key_meta_fields_list(project_id),
            [r.model_dump(mode="json") for r in responses],
            ttl_seconds=300,
        )
    return responses


async def create_meta_field(
    session: AsyncSession,
    slug: str,
    data: MetaFieldCreate,
    branch_id: uuid.UUID | None = None,
) -> MetaFieldDefinition:
    is_main = branch_id is None
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    existing = await session.execute(
        select(MetaFieldDefinition).where(
            MetaFieldDefinition.project_id == project_id,
            MetaFieldDefinition.branch_id == branch_id,
            MetaFieldDefinition.name == data.name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Meta field with this name already exists")
    mf = MetaFieldDefinition(**data.model_dump(), project_id=project_id, branch_id=branch_id)
    session.add(mf)
    await session.commit()
    await session.refresh(mf)
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    if is_main:
        await cache.delete_prefix(cache.prefix_meta_fields(project_id))
    return mf


async def update_meta_field(
    session: AsyncSession,
    slug: str,
    meta_field_id: uuid.UUID,
    data: MetaFieldUpdate,
    branch_id: uuid.UUID | None = None,
) -> MetaFieldDefinition:
    is_main = branch_id is None
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(MetaFieldDefinition).where(
            MetaFieldDefinition.id == meta_field_id,
            MetaFieldDefinition.project_id == project_id,
            MetaFieldDefinition.branch_id == branch_id,
        )
    )
    mf = result.scalar_one_or_none()
    if not mf:
        raise HTTPException(status_code=404, detail="Meta field not found")
    update_data = data.model_dump(exclude_unset=True)
    # The schema can only check the pair when both halves are in the payload.
    # Here the other half is on the row: switching a multi-valued field to
    # `boolean`, or turning the flag on for one that already is, has to be
    # refused against what is actually stored.
    resulting_type = update_data.get("field_type", mf.field_type)
    resulting_multi = update_data.get("allow_multiple", mf.allow_multiple)
    if resulting_multi and MetaFieldType(resulting_type) not in MULTI_VALUE_FIELD_TYPES:
        allowed = ", ".join(sorted(item.value for item in MULTI_VALUE_FIELD_TYPES))
        raise HTTPException(
            status_code=422,
            detail=f"allow_multiple is only supported for {allowed} fields",
        )
    for key, value in update_data.items():
        setattr(mf, key, value)
    await session.commit()
    await session.refresh(mf)
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    if is_main:
        await cache.delete_prefix(cache.prefix_meta_fields(project_id))
    return mf


async def delete_meta_field(
    session: AsyncSession,
    slug: str,
    meta_field_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> None:
    is_main = branch_id is None
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(MetaFieldDefinition).where(
            MetaFieldDefinition.id == meta_field_id,
            MetaFieldDefinition.project_id == project_id,
            MetaFieldDefinition.branch_id == branch_id,
        )
    )
    mf = result.scalar_one_or_none()
    if not mf:
        raise HTTPException(status_code=404, detail="Meta field not found")
    await session.delete(mf)
    await session.commit()
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    if is_main:
        await cache.delete_prefix(cache.prefix_meta_fields(project_id))


async def get_meta_field_usage(
    session: AsyncSession,
    slug: str,
    meta_field_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> MetaFieldUsageResponse:
    """How many values, on how many events, the field holds on this branch.

    The delete confirm names both before the cascade removes them (AU-37). An
    empty string is not a value anyone would miss, so it is not counted.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    exists = await session.scalar(
        select(MetaFieldDefinition.id).where(
            MetaFieldDefinition.id == meta_field_id,
            MetaFieldDefinition.project_id == project_id,
            MetaFieldDefinition.branch_id == branch_id,
        )
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="Meta field not found")
    row = (
        await session.execute(
            select(
                func.count(EventMetaValue.id),
                func.count(func.distinct(EventMetaValue.event_id)),
            ).where(
                EventMetaValue.meta_field_definition_id == meta_field_id,
                EventMetaValue.value != "",
            )
        )
    ).one()
    return MetaFieldUsageResponse(value_count=int(row[0] or 0), event_count=int(row[1] or 0))
