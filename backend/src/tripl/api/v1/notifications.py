"""The notification center, delivery preferences and Watch/Unwatch/Mute (#259).

One router for both halves so ``api.v1.router`` mounts it with one line:

* ``/me/notifications...`` and ``/me/notification-prefs`` are the caller's own
  and span every project they are a member of; they carry no ``slug``, so the
  membership gate passes them and the service filters by membership itself;
* ``/projects/{slug}/subscriptions/{entity_type}/{entity_id}`` is slug-scoped
  and behind the membership gate like every other project route.

Watching is personal, not plan editing: a viewer may watch, mute and read.
Only a ``read``-scope API key is refused the writes.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tripl.api.deps import CurrentUserDep, SessionDep, WriteUserDep
from tripl.schemas.notification import (
    MAX_NOTIFICATIONS_PAGE,
    MarkReadRequest,
    MarkReadResponse,
    NotificationPage,
    NotificationPrefsResponse,
    NotificationPrefsUpdate,
    SubscriptionEntityTypeLiteral,
    SubscriptionMuteRequest,
    SubscriptionState,
    UnreadCountResponse,
)
from tripl.services import notification_service, subscription_service

router = APIRouter(tags=["notifications"])

_SUBSCRIPTION_PATH = "/projects/{slug}/subscriptions/{entity_type}/{entity_id}"


@router.get("/me/notifications", response_model=NotificationPage)
async def list_my_notifications(
    session: SessionDep,
    user: CurrentUserDep,
    unread: bool = False,
    limit: Annotated[int, Query(ge=1, le=MAX_NOTIFICATIONS_PAGE)] = 30,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> NotificationPage:
    return await notification_service.list_notifications(
        session, user, unread=unread, limit=limit, cursor=cursor
    )


@router.get("/me/notifications/unread-count", response_model=UnreadCountResponse)
async def my_unread_count(session: SessionDep, user: CurrentUserDep) -> UnreadCountResponse:
    return UnreadCountResponse(unread=await notification_service.unread_count(session, user))


@router.post("/me/notifications/read", response_model=MarkReadResponse)
async def mark_my_notifications_read(
    session: SessionDep, user: WriteUserDep, data: MarkReadRequest
) -> MarkReadResponse:
    return await notification_service.mark_read(session, user, data)


@router.get("/me/notification-prefs", response_model=NotificationPrefsResponse)
async def get_my_notification_prefs(
    session: SessionDep, user: CurrentUserDep
) -> NotificationPrefsResponse:
    return await notification_service.get_prefs(session, user)


@router.patch("/me/notification-prefs", response_model=NotificationPrefsResponse)
async def update_my_notification_prefs(
    session: SessionDep, user: WriteUserDep, data: NotificationPrefsUpdate
) -> NotificationPrefsResponse:
    return await notification_service.update_prefs(session, user, data)


@router.get(_SUBSCRIPTION_PATH, response_model=SubscriptionState)
async def get_my_subscription(
    session: SessionDep,
    user: CurrentUserDep,
    slug: str,
    entity_type: SubscriptionEntityTypeLiteral,
    entity_id: uuid.UUID,
) -> SubscriptionState:
    return await subscription_service.get_state(
        session, slug, entity_type, entity_id, user_id=user.id
    )


@router.put(_SUBSCRIPTION_PATH, response_model=SubscriptionState)
async def watch_entity(
    session: SessionDep,
    user: WriteUserDep,
    slug: str,
    entity_type: SubscriptionEntityTypeLiteral,
    entity_id: uuid.UUID,
) -> SubscriptionState:
    return await subscription_service.watch(session, slug, entity_type, entity_id, user_id=user.id)


@router.delete(_SUBSCRIPTION_PATH, response_model=SubscriptionState)
async def unwatch_entity(
    session: SessionDep,
    user: WriteUserDep,
    slug: str,
    entity_type: SubscriptionEntityTypeLiteral,
    entity_id: uuid.UUID,
) -> SubscriptionState:
    return await subscription_service.unwatch(
        session, slug, entity_type, entity_id, user_id=user.id
    )


@router.patch(_SUBSCRIPTION_PATH, response_model=SubscriptionState)
async def mute_entity(
    session: SessionDep,
    user: WriteUserDep,
    slug: str,
    entity_type: SubscriptionEntityTypeLiteral,
    entity_id: uuid.UUID,
    data: SubscriptionMuteRequest,
) -> SubscriptionState:
    return await subscription_service.set_muted(
        session, slug, entity_type, entity_id, user_id=user.id, muted=data.muted
    )
