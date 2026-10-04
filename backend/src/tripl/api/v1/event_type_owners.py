import uuid

from fastapi import APIRouter

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.schemas.event_type_owner import EventTypeOwnerCreate, EventTypeOwnerResponse
from tripl.services import audit_service, event_type_owner_service

router = APIRouter(
    prefix="/projects/{slug}/event-types/{event_type_id}/owners",
    tags=["event-type-owners"],
)


# The whole project's owners in one read, for the event-type list:
# a sibling router, since ``router``'s prefix names one event type.
project_router = APIRouter(prefix="/projects/{slug}/event-type-owners", tags=["event-type-owners"])


@project_router.get("", response_model=list[EventTypeOwnerResponse])
async def list_project_owners(session: SessionDep, slug: str) -> list[EventTypeOwnerResponse]:
    """Owners of every live event type in the project; group by ``event_type_id``."""
    return await event_type_owner_service.list_project_owners(session, slug)


@router.get("", response_model=list[EventTypeOwnerResponse])
async def list_owners(
    session: SessionDep, slug: str, event_type_id: uuid.UUID
) -> list[EventTypeOwnerResponse]:
    return await event_type_owner_service.list_owners(session, slug, event_type_id)


@router.post("", response_model=EventTypeOwnerResponse, status_code=201)
async def add_owner(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    event_type_id: uuid.UUID,
    data: EventTypeOwnerCreate,
) -> EventTypeOwnerResponse:
    owner = await event_type_owner_service.add_owner(
        session, slug, event_type_id, data.user_id, granted_by=current_user.id
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_type.add_owner",
        target_type="event_type",
        target_id=event_type_id,
        target_name="",
        project_slug=slug,
        payload={"user_id": str(data.user_id)},
    )
    return owner


@router.delete("/{owner_id}", status_code=204)
async def remove_owner(
    session: SessionDep,
    current_user: EditorUserDep,
    slug: str,
    event_type_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> None:
    await event_type_owner_service.remove_owner(session, slug, event_type_id, owner_id)
    await audit_service.record(
        session,
        user=current_user,
        action="event_type.remove_owner",
        target_type="event_type",
        target_id=event_type_id,
        target_name="",
        project_slug=slug,
        payload={"owner_id": str(owner_id)},
    )
