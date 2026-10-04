import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.schemas.relation import RelationCreate, RelationUpdate
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.search_service import reindex_project_branch


async def list_relations(
    session: AsyncSession, slug: str, branch_id: uuid.UUID | None = None
) -> list[EventTypeRelation]:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(EventTypeRelation).where(
            EventTypeRelation.project_id == project_id,
            EventTypeRelation.branch_id == branch_id,
        )
    )
    return list(result.scalars().all())


async def _check_end(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    event_type_id: uuid.UUID,
    field_id: uuid.UUID,
    side: str,
) -> None:
    """Refuse one end of a relation that does not live in this project branch.

    The four ids come straight from the client and only the foreign keys read
    them, which are satisfied by a row in ANY project or branch. Nothing else
    checks the field belongs to the type it is named with. A stray id is not a
    row that merely reads oddly: ``deep_copy_plan_to_branch`` maps a relation's
    ids through the source branch's own event types and fields, so one such row
    makes every later branch creation for the project raise KeyError and 500
    until it is deleted, and the merge's relation key has the same hole.
    422 here, where the row is still refusable.
    """
    type_exists = (
        await session.execute(
            select(EventType.id).where(
                EventType.id == event_type_id,
                EventType.project_id == project_id,
                EventType.branch_id == branch_id,
            )
        )
    ).scalar_one_or_none()
    if type_exists is None:
        raise HTTPException(
            status_code=422,
            detail=f"{side}_event_type_id is not an event type in this project branch",
        )
    # The field's owner is the only tie it has to a project and a branch —
    # ``field_definitions`` carries neither column — so matching it against the
    # type just checked is what keeps the field in the branch too.
    owner_id = (
        await session.execute(
            select(FieldDefinition.event_type_id).where(FieldDefinition.id == field_id)
        )
    ).scalar_one_or_none()
    if owner_id != event_type_id:
        raise HTTPException(
            status_code=422,
            detail=f"{side}_field_id is not a field of {side}_event_type_id",
        )


async def create_relation(
    session: AsyncSession,
    slug: str,
    data: RelationCreate,
    branch_id: uuid.UUID | None = None,
) -> EventTypeRelation:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    await _check_end(
        session,
        project_id=project_id,
        branch_id=branch_id,
        event_type_id=data.source_event_type_id,
        field_id=data.source_field_id,
        side="source",
    )
    await _check_end(
        session,
        project_id=project_id,
        branch_id=branch_id,
        event_type_id=data.target_event_type_id,
        field_id=data.target_field_id,
        side="target",
    )
    relation = EventTypeRelation(**data.model_dump(), project_id=project_id, branch_id=branch_id)
    session.add(relation)
    await session.commit()
    await session.refresh(relation)
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    return relation


async def update_relation(
    session: AsyncSession,
    slug: str,
    relation_id: uuid.UUID,
    data: RelationUpdate,
    branch_id: uuid.UUID | None = None,
) -> EventTypeRelation:
    """Edit a relation in place (AU-13), re-checking any end that moves.

    The same ``_check_end`` guard as create, on the merged values: a changed
    field must still belong to its (possibly unchanged) event type, in this
    project branch.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    relation = (
        await session.execute(
            select(EventTypeRelation).where(
                EventTypeRelation.id == relation_id,
                EventTypeRelation.project_id == project_id,
                EventTypeRelation.branch_id == branch_id,
            )
        )
    ).scalar_one_or_none()
    if relation is None:
        raise HTTPException(status_code=404, detail="Relation not found")
    changes = data.model_dump(exclude_unset=True)
    for side in ("source", "target"):
        type_key, field_key = f"{side}_event_type_id", f"{side}_field_id"
        if type_key not in changes and field_key not in changes:
            continue
        await _check_end(
            session,
            project_id=project_id,
            branch_id=branch_id,
            event_type_id=changes.get(type_key, getattr(relation, type_key)),
            field_id=changes.get(field_key, getattr(relation, field_key)),
            side=side,
        )
    for key, value in changes.items():
        setattr(relation, key, value)
    await session.commit()
    await session.refresh(relation)
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
    return relation


async def delete_relation(
    session: AsyncSession,
    slug: str,
    relation_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> None:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    result = await session.execute(
        select(EventTypeRelation).where(
            EventTypeRelation.id == relation_id,
            EventTypeRelation.project_id == project_id,
            EventTypeRelation.branch_id == branch_id,
        )
    )
    relation = result.scalar_one_or_none()
    if not relation:
        raise HTTPException(status_code=404, detail="Relation not found")
    await session.delete(relation)
    await session.commit()
    await reindex_project_branch(session, project_id=project_id, branch_id=branch_id, slug=slug)
