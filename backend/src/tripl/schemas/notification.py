"""Notification center, subscriptions and delivery preferences (#259)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

SubscriptionEntityTypeLiteral = Literal["event", "event_type", "metric", "branch"]
SubscriptionReasonLiteral = Literal["author", "owner", "commenter", "reviewer", "manual"]
NotificationKindLiteral = Literal[
    "comment",
    "reply",
    "mention",
    "open_question",
    "signal",
    "branch_review_requested",
    "branch_approved",
    "branch_merged",
    "lifecycle",
]
EmailModeLiteral = Literal["off", "instant", "daily", "weekly"]

# One page of the bell; the API clamps ``limit`` to this.
MAX_NOTIFICATIONS_PAGE = 100


class NotificationActor(BaseModel):
    id: uuid.UUID
    name: str
    email: str


class NotificationResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    project_slug: str
    project_name: str
    kind: NotificationKindLiteral
    entity_type: SubscriptionEntityTypeLiteral
    entity_id: uuid.UUID
    title: str
    body: str
    url: str
    actor: NotificationActor | None
    read_at: datetime | None
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationResponse]
    # Opaque; pass back as ``cursor`` for the next (older) page. ``None`` = end.
    next_cursor: str | None


class UnreadCountResponse(BaseModel):
    unread: int


class MarkReadRequest(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    all: bool = False

    @model_validator(mode="after")
    def _one_target(self) -> MarkReadRequest:
        if not self.all and not self.ids:
            raise ValueError("Pass ids or all=true")
        return self


class MarkReadResponse(BaseModel):
    updated: int
    unread: int


class NotificationPrefsResponse(BaseModel):
    email_mode: EmailModeLiteral
    mentions_email: bool
    # False when the instance has no SMTP configured: email settings are kept
    # but nothing is sent until it is.
    email_available: bool


class NotificationPrefsUpdate(BaseModel):
    email_mode: EmailModeLiteral | None = None
    mentions_email: bool | None = None


class SubscriptionState(BaseModel):
    entity_type: SubscriptionEntityTypeLiteral
    # The canonical id the subscription is kept under (an event's discussion
    # home, i.e. the main twin of a branch copy).
    entity_id: uuid.UUID
    watching: bool
    muted: bool
    reasons: list[SubscriptionReasonLiteral]


class SubscriptionMuteRequest(BaseModel):
    muted: bool
