from __future__ import annotations

import uuid

from fastapi import APIRouter

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.schemas.event_photo import (
    EventCommentActionRequest,
    EventPhotoCommentCreate,
    EventPhotoCommentResponse,
)
from tripl.services import audit_service, event_comment_service

# A sibling of the photo threads under the same event prefix, not a nested
# resource of one: this discussion is about the event, and having to attach a
# photo before you could raise anything is what put it out of reach.
router = APIRouter(
    prefix="/projects/{slug}/events/{event_id}/comments",
    tags=["event-comments"],
)


@router.get("", response_model=list[EventPhotoCommentResponse])
async def list_event_comments(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
) -> list[EventPhotoCommentResponse]:
    comments = await event_comment_service.list_comments(session, slug, event_id)
    return [EventPhotoCommentResponse.model_validate(comment) for comment in comments]


@router.post("", response_model=EventPhotoCommentResponse, status_code=201)
async def create_event_comment(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    data: EventPhotoCommentCreate,
    current_user: EditorUserDep,
) -> EventPhotoCommentResponse:
    comment = await event_comment_service.create_comment(
        session,
        slug,
        event_id,
        body=data.body,
        parent_id=data.parent_id,
        user_id=current_user.id,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_comment.create",
        target_type="event_comment",
        target_id=comment.id,
        project_slug=slug,
        payload={"event_id": str(event_id)},
    )
    return EventPhotoCommentResponse.model_validate(comment)


@router.post("/{comment_id}/actions", response_model=EventPhotoCommentResponse)
async def apply_event_comment_action(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    comment_id: uuid.UUID,
    data: EventCommentActionRequest,
    current_user: EditorUserDep,
) -> EventPhotoCommentResponse:
    """Resolve, snooze or reopen one thread.

    An ``/actions`` sub-resource, not a PATCH on the comment: the body names an
    intent and the service decides which of the five resolution columns move —
    the shape every other resolvable thing here already uses.
    """
    comment = await event_comment_service.apply_comment_action(
        session,
        slug,
        event_id,
        comment_id,
        data,
        user_id=current_user.id,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_comment.action",
        target_type="event_comment",
        target_id=comment_id,
        project_slug=slug,
        payload={"event_id": str(event_id), "action": data.action},
    )
    return EventPhotoCommentResponse.model_validate(comment)


@router.delete("/{comment_id}", status_code=204)
async def delete_event_comment(
    session: SessionDep,
    slug: str,
    event_id: uuid.UUID,
    comment_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    await event_comment_service.delete_comment(
        session, slug, event_id, comment_id, user=current_user
    )
    await audit_service.record(
        session,
        user=current_user,
        action="event_comment.delete",
        target_type="event_comment",
        target_id=comment_id,
        project_slug=slug,
        payload={"event_id": str(event_id)},
    )
