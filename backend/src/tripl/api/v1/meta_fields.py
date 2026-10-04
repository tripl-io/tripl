import uuid

from fastapi import APIRouter

from tripl.api.deps import BranchIdDep, EditorUserDep, SessionDep
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.schemas.meta_field import (
    MetaFieldCreate,
    MetaFieldResponse,
    MetaFieldUpdate,
    MetaFieldUsageResponse,
)
from tripl.services import audit_service, meta_field_service

router = APIRouter(prefix="/projects/{slug}/meta-fields", tags=["meta-fields"])


@router.get("", response_model=list[MetaFieldResponse])
async def list_meta_fields(
    session: SessionDep, slug: str, branch_id: BranchIdDep
) -> list[MetaFieldResponse]:
    return await meta_field_service.list_meta_fields(session, slug, branch_id)


@router.post("", response_model=MetaFieldResponse, status_code=201)
async def create_meta_field(
    session: SessionDep,
    slug: str,
    data: MetaFieldCreate,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> MetaFieldDefinition:
    mf = await meta_field_service.create_meta_field(session, slug, data, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="meta_field.create",
        target_type="meta_field",
        target_id=mf.id,
        target_name=mf.name,
        project_slug=slug,
        payload=data.model_dump(),
    )
    return mf


@router.get("/{meta_field_id}/usage", response_model=MetaFieldUsageResponse)
async def get_meta_field_usage(
    session: SessionDep,
    slug: str,
    meta_field_id: uuid.UUID,
    branch_id: BranchIdDep,
) -> MetaFieldUsageResponse:
    """Values and events a delete of this field would clear."""
    return await meta_field_service.get_meta_field_usage(session, slug, meta_field_id, branch_id)


@router.patch("/{meta_field_id}", response_model=MetaFieldResponse)
async def update_meta_field(
    session: SessionDep,
    slug: str,
    meta_field_id: uuid.UUID,
    data: MetaFieldUpdate,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> MetaFieldDefinition:
    mf = await meta_field_service.update_meta_field(session, slug, meta_field_id, data, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="meta_field.update",
        target_type="meta_field",
        target_id=mf.id,
        target_name=mf.name,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return mf


@router.delete("/{meta_field_id}", status_code=204)
async def delete_meta_field(
    session: SessionDep,
    slug: str,
    meta_field_id: uuid.UUID,
    current_user: EditorUserDep,
    branch_id: BranchIdDep,
) -> None:
    existing = next(
        (
            mf
            for mf in await meta_field_service.list_meta_fields(session, slug, branch_id)
            if mf.id == meta_field_id
        ),
        None,
    )
    name = existing.name if existing else ""
    await meta_field_service.delete_meta_field(session, slug, meta_field_id, branch_id)
    await audit_service.record(
        session,
        user=current_user,
        action="meta_field.delete",
        target_type="meta_field",
        target_id=meta_field_id,
        target_name=name,
        project_slug=slug,
    )
