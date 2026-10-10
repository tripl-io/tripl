from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.middleware.org_context import current_org
from tripl.schemas.event_photo import (
    EventPhotoCommentCreate,
    EventPhotoCommentResponse,
    EventPhotoFigmaCreate,
    EventPhotoReorder,
    EventPhotoResponse,
)
from tripl.services import audit_service, event_photo_service

router = APIRouter(
    prefix="/projects/{slug}/events/{event_id}/photos",
    tags=["event-photos"],
)


async def _to_response(session: SessionDep, photo, slug: str) -> EventPhotoResponse:  # type: ignore[no-untyped-def]
    # The organization this request resolved ``slug`` in: the file
    # URL names it, so a later fetch cannot land in another organization.
    org = current_org()
    url = await event_photo_service.url_for(
        photo, slug, org.slug if org is not None else None, session=session
    )
    return EventPhotoResponse(
        id=photo.id,
        event_id=photo.event_id,
        project_id=photo.project_id,
        kind=photo.kind,
        original_filename=photo.original_filename,
        content_type=photo.content_type,
        size_bytes=photo.size_bytes,
        storage_backend=photo.storage_backend,
        sort_order=photo.sort_order,
        url=url,
        external_url=photo.external_url,
        uploaded_by_user_id=photo.uploaded_by_user_id,
        created_at=photo.created_at,
    )


@router.get("", response_model=list[EventPhotoResponse])
async def list_event_photos(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
) -> list[EventPhotoResponse]:
    photos = await event_photo_service.list_photos(session, slug, event_id)
    return [await _to_response(session, photo, slug) for photo in photos]


@router.post("", response_model=EventPhotoResponse, status_code=201)
async def upload_event_photo(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    current_user: EditorUserDep,
    file: Annotated[UploadFile, File()],
) -> EventPhotoResponse:
    # The project's organization's limits and storage (F20 PR11): checked while
    # the file is read, so a refused upload is never buffered whole.
    policy = await event_photo_service.policy_for_slug(session, slug)
    data = await event_photo_service.read_upload(file, policy)
    photo = await event_photo_service.upload_photo(
        session,
        slug,
        event_id,
        data=data,
        content_type=file.content_type or "",
        original_filename=file.filename or "",
        uploaded_by_user_id=current_user.id,
        policy=policy,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_photo.upload",
        target_type="event_photo",
        target_id=photo.id,
        project_slug=slug,
        payload={"event_id": str(event_id)},
    )
    return await _to_response(session, photo, slug)


@router.patch("/reorder", response_model=list[EventPhotoResponse])
async def reorder_event_photos(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    data: EventPhotoReorder,
    current_user: EditorUserDep,
) -> list[EventPhotoResponse]:
    photos = await event_photo_service.reorder_photos(session, slug, event_id, data.photo_ids)
    await audit_service.record(
        session,
        user=current_user,
        action="event_photo.reorder",
        target_type="event",
        target_id=event_id,
        project_slug=slug,
        payload={"photo_ids": [str(photo_id) for photo_id in data.photo_ids]},
    )
    return [await _to_response(session, photo, slug) for photo in photos]


@router.delete("/{photo_id}", status_code=204)
async def delete_event_photo(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    await event_photo_service.delete_photo(session, slug, event_id, photo_id)
    await audit_service.record(
        session,
        user=current_user,
        action="event_photo.delete",
        target_type="event_photo",
        target_id=photo_id,
        project_slug=slug,
        payload={"event_id": str(event_id)},
    )


_BYTES = {"schema": {"type": "string", "format": "binary"}}


@router.get(
    "/{photo_id}/file",
    response_class=Response,
    responses={
        200: {
            "description": "The photo's bytes, with the content type it was uploaded with.",
            "content": {"image/*": _BYTES, "application/octet-stream": _BYTES},
        },
        204: {"description": "A Figma attachment: there is no file to stream."},
    },
)
async def download_event_photo(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
) -> Response:
    """Stream the photo bytes through the API.

    The serving path for every photo on the local backend, the default, whose
    files are not reachable from the browser. On GCS a photo's `url` field is
    a signed or public URL the browser fetches directly; when that URL cannot
    be made, for instance with credentials that cannot sign, the `url` field
    points here instead.
    """
    photo = await event_photo_service.get_photo(session, slug, event_id, photo_id)
    if photo.kind != event_photo_service.PHOTO_KIND_PHOTO or not photo.storage_key:
        # Figma-kind attachments have no blob to stream.
        return Response(status_code=204)
    data = await event_photo_service.read_blob(session, photo)
    return Response(
        content=data,
        media_type=photo.content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/figma", response_model=EventPhotoResponse, status_code=201)
async def attach_figma_spec(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    data: EventPhotoFigmaCreate,
    current_user: EditorUserDep,
) -> EventPhotoResponse:
    photo = await event_photo_service.attach_figma(
        session,
        slug,
        event_id,
        external_url=data.url,
        title=data.title,
        uploaded_by_user_id=current_user.id,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_photo.figma_attach",
        target_type="event_photo",
        target_id=photo.id,
        project_slug=slug,
        payload={"event_id": str(event_id)},
    )
    return await _to_response(session, photo, slug)


@router.get("/{photo_id}/comments", response_model=list[EventPhotoCommentResponse])
async def list_photo_comments(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
) -> list[EventPhotoCommentResponse]:
    comments = await event_photo_service.list_comments(session, slug, event_id, photo_id)
    return [EventPhotoCommentResponse.model_validate(comment) for comment in comments]


@router.post("/{photo_id}/comments", response_model=EventPhotoCommentResponse, status_code=201)
async def create_photo_comment(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
    data: EventPhotoCommentCreate,
    current_user: EditorUserDep,
) -> EventPhotoCommentResponse:
    comment = await event_photo_service.create_comment(
        session,
        slug,
        event_id,
        photo_id,
        body=data.body,
        parent_id=data.parent_id,
        user_id=current_user.id,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_photo.comment_create",
        target_type="event_photo_comment",
        target_id=comment.id,
        project_slug=slug,
        payload={"event_id": str(event_id), "photo_id": str(photo_id)},
    )
    return EventPhotoCommentResponse.model_validate(comment)


@router.delete("/{photo_id}/comments/{comment_id}", status_code=204)
async def delete_photo_comment(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    photo_id: uuid.UUID,
    comment_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    await event_photo_service.delete_comment(
        session, slug, event_id, photo_id, comment_id, user=current_user
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_photo.comment_delete",
        target_type="event_photo_comment",
        target_id=comment_id,
        project_slug=slug,
        payload={"event_id": str(event_id), "photo_id": str(photo_id)},
    )
