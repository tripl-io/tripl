"""The notification center (#259): one ``notify`` for every producer, and the bell's reads.

Every producer — a comment, a reply, a mention, an open question, a signal, a
branch review step, a lifecycle finding — goes through the same :func:`notify`
with a ``kind``. There is deliberately no per-event helper: what differs
between producers is who is addressed and the text, and both are arguments.

Recipients are the union of ``user_ids`` (addressed directly) and the unmuted
subscribers of ``watchers_of``, then filtered, in this order:

1. ``exclude_user_ids`` and the actor are dropped — nobody hears about their
   own action;
2. only CURRENT project members survive (instance owners always do), read at
   send time, so a grant that outlived a membership never leaks a project;
3. with ``honour_mute`` (the default) anyone who muted the primary entity is
   dropped — an @mention passes ``honour_mute=False``;
4. with ``throttle`` anyone who already got a notification of this ``kind``
   about this entity within the window is dropped (signals: 6h).

Rows are added to the caller's transaction; ``notify`` never commits. The sync
core :func:`notify_sync` is what Celery workers call with their ``Session``;
:func:`notify` runs the same core on the request's ``AsyncSession``.

Email is not sent here. Rows carry ``emailed_at = NULL`` and the email paths
(instant sweep, daily/weekly digest) pick them up, honouring
``user_notification_prefs`` and skipping silently without SMTP.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import NotRequired, TypedDict, Unpack

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.domain_enums import UserRole
from tripl.models.notification import Notification, NotificationKind
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.models.user_notification_prefs import (
    DEFAULT_EMAIL_MODE,
    DEFAULT_MENTIONS_EMAIL,
    UserNotificationPrefs,
)
from tripl.schemas.notification import (
    MAX_NOTIFICATIONS_PAGE,
    MarkReadRequest,
    MarkReadResponse,
    NotificationPage,
    NotificationPrefsResponse,
    NotificationPrefsUpdate,
    NotificationResponse,
)
from tripl.services import app_settings_service
from tripl.services.project_access import member_project_ids
from tripl.services.subscription_service import (
    EntityRef,
    muted_user_ids_sync,
    subscriber_ids_sync,
)

# The signal throttle: one signal notification per entity per subscriber per 6h.
SIGNAL_THROTTLE = timedelta(hours=6)

_TITLE_LIMIT = 300
_BODY_LIMIT = 2000


class NotifyCommon(TypedDict):
    """The part several ``notify`` passes over one change share: what it is about
    and who did it. Each pass adds its own ``kind``, ``title`` and audience."""

    project_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    url: str
    body: str
    actor_user_id: uuid.UUID | None


class NotifyArgs(TypedDict):
    """:func:`notify_sync`'s keyword arguments, for the async wrapper and callers
    that build the shared part of several ``notify`` calls once."""

    project_id: NotRequired[uuid.UUID]
    kind: NotRequired[str]
    entity_type: NotRequired[str]
    entity_id: NotRequired[uuid.UUID]
    title: NotRequired[str]
    url: NotRequired[str]
    body: NotRequired[str]
    actor_user_id: NotRequired[uuid.UUID | None]
    user_ids: NotRequired[Iterable[uuid.UUID]]
    watchers_of: NotRequired[Iterable[EntityRef]]
    watcher_reasons: NotRequired[Iterable[str] | None]
    exclude_user_ids: NotRequired[Iterable[uuid.UUID]]
    honour_mute: NotRequired[bool]
    throttle: NotRequired[timedelta | None]
    now: NotRequired[datetime | None]


def _members_among_sync(
    session: Session, project_id: uuid.UUID, user_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """``project_access.members_among`` on a sync session: owners plus member rows."""
    if not user_ids:
        return set()
    owners = session.scalars(
        select(User.id).where(User.id.in_(user_ids), User.role == UserRole.owner.value)
    )
    members = session.scalars(
        select(ProjectMember.user_id)
        .join(User, User.id == ProjectMember.user_id)
        .where(ProjectMember.project_id == project_id, ProjectMember.user_id.in_(user_ids))
    )
    return set(owners.all()) | set(members.all())


def _recently_notified_sync(
    session: Session,
    *,
    user_ids: set[uuid.UUID],
    kind: str,
    entity_type: str,
    entity_id: uuid.UUID,
    since: datetime,
) -> set[uuid.UUID]:
    if not user_ids:
        return set()
    rows = session.scalars(
        select(Notification.user_id)
        .where(
            Notification.user_id.in_(user_ids),
            Notification.kind == kind,
            Notification.entity_type == entity_type,
            Notification.entity_id == entity_id,
            Notification.created_at >= since,
        )
        .distinct()
    )
    return set(rows.all())


def notify_sync(
    session: Session,
    *,
    project_id: uuid.UUID,
    kind: str,
    entity_type: str,
    entity_id: uuid.UUID,
    title: str,
    url: str,
    body: str = "",
    actor_user_id: uuid.UUID | None = None,
    user_ids: Iterable[uuid.UUID] = (),
    watchers_of: Iterable[EntityRef] = (),
    watcher_reasons: Iterable[str] | None = None,
    exclude_user_ids: Iterable[uuid.UUID] = (),
    honour_mute: bool = True,
    throttle: timedelta | None = None,
    now: datetime | None = None,
) -> set[uuid.UUID]:
    """Write one notification per recipient; answer the set of users notified.

    See the module docstring for who the recipients are. The answer lets a
    caller that sends several kinds for one action (a mention, then the comment
    itself) exclude whoever already heard about it.
    """
    kind = NotificationKind(kind).value
    moment = now or datetime.now(UTC)
    recipients = set(user_ids)
    refs = list(watchers_of)
    if refs:
        recipients |= subscriber_ids_sync(session, refs, reasons=watcher_reasons)
    recipients -= set(exclude_user_ids)
    if actor_user_id is not None:
        recipients.discard(actor_user_id)
    recipients = _members_among_sync(session, project_id, recipients)
    if honour_mute and recipients:
        recipients -= muted_user_ids_sync(session, (entity_type, entity_id))
    if throttle is not None and recipients:
        recipients -= _recently_notified_sync(
            session,
            user_ids=recipients,
            kind=kind,
            entity_type=entity_type,
            entity_id=entity_id,
            since=moment - throttle,
        )
    clean_title = " ".join(title.split())[:_TITLE_LIMIT]
    clean_body = body.strip()[:_BODY_LIMIT]
    for user_id in sorted(recipients, key=str):
        session.add(
            Notification(
                user_id=user_id,
                project_id=project_id,
                kind=kind,
                entity_type=entity_type,
                entity_id=entity_id,
                title=clean_title,
                body=clean_body,
                url=url,
                actor_user_id=actor_user_id,
                created_at=moment,
            )
        )
    return recipients


async def notify(session: AsyncSession, **kwargs: Unpack[NotifyArgs]) -> set[uuid.UUID]:
    """:func:`notify_sync` on the request's ``AsyncSession`` (same transaction)."""
    missing = _REQUIRED_NOTIFY_ARGS - kwargs.keys()
    if missing:
        raise TypeError(f"notify() missing {sorted(missing)}")
    return await session.run_sync(lambda s: notify_sync(s, **kwargs))


_REQUIRED_NOTIFY_ARGS = frozenset(
    {"project_id", "kind", "entity_type", "entity_id", "title", "url"}
)


# ── the bell ────────────────────────────────────────────────────────────────


def _visible(visible: set[uuid.UUID] | None) -> ColumnElement[bool] | None:
    if visible is None:
        return None
    return Notification.project_id.in_(visible)


def _encode_cursor(created_at: datetime, notification_id: uuid.UUID) -> str:
    raw = f"{created_at.astimezone(UTC).isoformat()}|{notification_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, ident = raw.partition("|")
        return datetime.fromisoformat(stamp), uuid.UUID(ident)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc


async def list_notifications(
    session: AsyncSession,
    user: User,
    *,
    unread: bool = False,
    limit: int = 30,
    cursor: str | None = None,
) -> NotificationPage:
    """The caller's notifications, newest first, only from projects they can see now."""
    visible = await member_project_ids(session, user)
    if visible is not None and not visible:
        return NotificationPage(items=[], next_cursor=None)
    limit = max(1, min(limit, MAX_NOTIFICATIONS_PAGE))
    stmt = (
        select(Notification, Project.slug, Project.name, User)
        .join(Project, Project.id == Notification.project_id)
        .outerjoin(User, User.id == Notification.actor_user_id)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit + 1)
    )
    scope = _visible(visible)
    if scope is not None:
        stmt = stmt.where(scope)
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    if cursor:
        at, ident = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Notification.created_at < at,
                and_(Notification.created_at == at, Notification.id < ident),
            )
        )
    rows = (await session.execute(stmt)).all()
    page = rows[:limit]
    items = [
        NotificationResponse.model_validate(
            {
                "id": note.id,
                "project_id": note.project_id,
                "project_slug": slug,
                "project_name": name,
                "kind": note.kind,
                "entity_type": note.entity_type,
                "entity_id": note.entity_id,
                "title": note.title,
                "body": note.body,
                "url": note.url,
                "actor": (
                    {"id": actor.id, "name": actor.name or actor.email, "email": actor.email}
                    if actor is not None
                    else None
                ),
                "read_at": note.read_at,
                "created_at": note.created_at,
            }
        )
        for note, slug, name, actor in page
    ]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1][0]
        next_cursor = _encode_cursor(last.created_at, last.id)
    return NotificationPage(items=items, next_cursor=next_cursor)


async def _unread_count(session: AsyncSession, user: User, visible: set[uuid.UUID] | None) -> int:
    if visible is not None and not visible:
        return 0
    stmt = select(func.count(Notification.id)).where(
        Notification.user_id == user.id, Notification.read_at.is_(None)
    )
    scope = _visible(visible)
    if scope is not None:
        stmt = stmt.where(scope)
    return int(await session.scalar(stmt) or 0)


async def unread_count(session: AsyncSession, user: User) -> int:
    return await _unread_count(session, user, await member_project_ids(session, user))


async def mark_read(session: AsyncSession, user: User, data: MarkReadRequest) -> MarkReadResponse:
    """Stamp ``read_at`` on the caller's own unread rows (``ids`` or ``all``).

    Ids that are not the caller's, or already read, are ignored rather than
    refused: the bell may race another tab.
    """
    visible = await member_project_ids(session, user)
    updated = 0
    if visible is None or visible:
        stmt = (
            update(Notification)
            .where(Notification.user_id == user.id, Notification.read_at.is_(None))
            .values(read_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        scope = _visible(visible)
        if scope is not None:
            stmt = stmt.where(scope)
        if not data.all:
            stmt = stmt.where(Notification.id.in_(data.ids))
        result = await session.execute(stmt)
        updated = int(getattr(result, "rowcount", 0) or 0)
        await session.commit()
    return MarkReadResponse(updated=updated, unread=await _unread_count(session, user, visible))


# ── delivery preferences ────────────────────────────────────────────────────


async def _email_available(session: AsyncSession) -> bool:
    return app_settings_service.email_can_send(await app_settings_service.get_email_config(session))


async def get_prefs(session: AsyncSession, user: User) -> NotificationPrefsResponse:
    row = await session.get(UserNotificationPrefs, user.id)
    return NotificationPrefsResponse(
        email_mode=row.email_mode if row is not None else DEFAULT_EMAIL_MODE,
        mentions_email=row.mentions_email if row is not None else DEFAULT_MENTIONS_EMAIL,
        email_available=await _email_available(session),
    )


async def update_prefs(
    session: AsyncSession, user: User, data: NotificationPrefsUpdate
) -> NotificationPrefsResponse:
    row = await session.get(UserNotificationPrefs, user.id)
    if row is None:
        row = UserNotificationPrefs(
            user_id=user.id,
            email_mode=DEFAULT_EMAIL_MODE,
            mentions_email=DEFAULT_MENTIONS_EMAIL,
        )
        session.add(row)
    if data.email_mode is not None:
        row.email_mode = data.email_mode
    if data.mentions_email is not None:
        row.mentions_email = data.mentions_email
    await session.commit()
    return await get_prefs(session, user)


def prefs_for_users_sync(
    session: Session, user_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, bool]]:
    """user id → (email_mode, mentions_email), defaults filled in. For the email paths."""
    rows = session.execute(
        select(
            UserNotificationPrefs.user_id,
            UserNotificationPrefs.email_mode,
            UserNotificationPrefs.mentions_email,
        ).where(UserNotificationPrefs.user_id.in_(list(user_ids)))
    ).all()
    found = {user_id: (mode, mentions) for user_id, mode, mentions in rows}
    return {
        user_id: found.get(user_id, (DEFAULT_EMAIL_MODE, DEFAULT_MENTIONS_EMAIL))
        for user_id in user_ids
    }
