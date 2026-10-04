from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tripl.models.domain_enums import (
    EventCommentStatus,
    EventPhotoKind,
    EventPhotoStorageBackend,
)


class EventPhotoResponse(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    project_id: uuid.UUID
    kind: EventPhotoKind
    original_filename: str
    content_type: str
    size_bytes: int
    storage_backend: EventPhotoStorageBackend | None
    sort_order: int
    # Resolved URL the client can render directly. For "local" this is an
    # authenticated API endpoint; for "gcs" it's a signed URL (or public URL
    # when the bucket is configured public), or that same API endpoint when
    # the backend cannot produce a direct URL (e.g. GCS credentials that
    # cannot sign; event_photo_service.url_for). For figma-kind attachments
    # this is the embed URL.
    url: str
    external_url: str | None = None
    uploaded_by_user_id: uuid.UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PhotoLimitsResponse(BaseModel):
    """What the upload endpoint will take, for the browser to say so up front.

    ``photo_max_size_mb`` is an owner setting; every signed-in user may read it,
    because an editor's upload is what it refuses. Both values are the
    organization's (F20 PR11): its own cap, never above the operator's, and its
    allow-list, never wider than the operator's.
    """

    photo_max_size_mb: int
    #: Content types the upload accepts, lower-case (``image/png``...).
    photo_allowed_mime: list[str]


class EventPhotoReorder(BaseModel):
    photo_ids: list[uuid.UUID]


class EventPhotoFigmaCreate(BaseModel):
    """Attach a Figma frame/file as a design spec on this event."""

    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=500)


class EventPhotoCommentResponse(BaseModel):
    id: uuid.UUID
    # Exactly one anchor is set, enforced by ck_event_photo_comment_one_anchor:
    # a comment pinned to one attachment, or the event's own discussion.
    photo_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None
    user_id: uuid.UUID | None
    body: str
    # Resolution state. Carried on every row because this is one table, but only
    # a top-level comment can be acted on — the thread is what gets answered.
    status: EventCommentStatus = EventCommentStatus.open
    resolution_note: str | None = None
    snoozed_until: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


EventCommentAction = Literal["resolve", "snooze", "reopen"]


class EventPhotoCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    parent_id: uuid.UUID | None = None


class EventCommentActionRequest(BaseModel):
    """One action on one thread, shaped like ``SchemaDriftActionRequest``.

    POST to an ``/actions`` sub-resource rather than PATCH on the comment, the
    way every other resolvable thing in this codebase does it: the body names an
    intent, and the five columns it moves are the service's business, not the
    client's.
    """

    action: EventCommentAction
    note: str | None = Field(None, max_length=2000)
    snoozed_until: datetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> EventCommentActionRequest:
        if self.action == "snooze" and self.snoozed_until is None:
            raise ValueError("snoozed_until is required when action is snooze")
        # A ``snoozed_until`` that has ALREADY PASSED is accepted here, and this
        # is the one "stay quiet until then" body in the codebase that takes one
        # (``schemas/time_guards.py`` refuses it for four, and ``mute_monitor``
        # has answered 422 to its own since it shipped). Not an oversight:
        # nothing is hidden by taking it. The reply carries ``snoozed_until``
        # beside ``status``, and both readers that decide whether the thread
        # still wants an answer work it out from that pair rather than from
        # ``status`` alone — ``event_comment_service.unanswered_clause`` on the
        # server, ``commentThreadState.isThreadUnanswered`` in the client, which
        # also drops the "snoozed" badge once the date is behind the clock. So
        # the thread reads as open immediately, which is exactly what a lapsed
        # snooze means here anyway, and refusing the instant would buy no
        # honesty while closing the only way to reach that state without waiting
        # out the clock — the way
        # ``test_event_comments.py::test_a_lapsed_snooze_counts_as_unanswered_again``
        # reaches it. The refusal below is a different question: a date sent
        # with an action that will never store it.
        if self.action != "snooze" and self.snoozed_until is not None:
            # A snooze date on a resolve or a reopen is a client that meant
            # something else; accepting and discarding it would hide the mistake.
            raise ValueError("snoozed_until is only meaningful when action is snooze")
        return self
