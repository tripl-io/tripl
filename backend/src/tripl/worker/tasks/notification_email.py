"""Email delivery of in-app notifications (#259). Email is the only external channel in v1.

Three entry points, ONE delivery routine (:func:`_deliver`):

* ``send_notification_emails`` — the INSTANT path. Enqueued with the ids a
  ``notify`` call just committed (:func:`enqueue_notification_emails`), and run
  every minute by beat with no ids as a sweep, so a lost enqueue only delays
  an email instead of dropping it. One email per notification.
* ``send_notification_digest("daily" | "weekly")`` — the digest beat tasks.
  One email per user, grouping what is still unread and not yet emailed.

Who gets what (``UserNotificationPrefs``; no row means the defaults ``daily`` +
mention emails on):

* an @mention is emailed instantly when ``mentions_email`` is on, whatever
  ``email_mode`` says, and never when it is off (not in a digest either);
* everything else follows ``email_mode``: ``instant`` -> the instant path,
  ``daily`` / ``weekly`` -> that digest, ``off`` -> never.

Guarantees, all checked at SEND time rather than when the row was written:

* members only — a user who has left the project since gets nothing about it
  (the instance owner counts, as they see every project); demo projects send
  no email at all;
* at most once — a row is CLAIMED by stamping ``emailed_at`` with a
  conditional UPDATE (``emailed_at IS NULL``) committed before the send, so two
  overlapping runs (an enqueue and the sweep, two digests) cannot both send it.
  A failed send clears the stamp again so a later run retries;
* SMTP not configured (or no valid Default From) -> nothing is sent, nothing is
  stamped, nothing is reported as an error: the in-app notification is the
  product, email is the extra. A row left unsent that way is picked up once
  SMTP is configured, bounded by the lookbacks below.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.orm import Session

from tripl.models.domain_enums import UserRole
from tripl.models.notification import Notification, NotificationKind
from tripl.models.project import Project
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.models.user_notification_prefs import (
    DEFAULT_EMAIL_MODE,
    DEFAULT_MENTIONS_EMAIL,
    EmailMode,
    UserNotificationPrefs,
)
from tripl.services import alert_owner_routing, app_settings_service
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session

logger = logging.getLogger(__name__)

# How far back each path looks. The instant sweep only retries recent rows, so
# an address that keeps failing (or SMTP configured weeks later) does not turn
# into a burst of stale one-by-one emails; a digest covers its own period plus
# slack for a missed beat.
INSTANT_LOOKBACK = timedelta(hours=24)
DIGEST_LOOKBACK: dict[str, timedelta] = {
    EmailMode.daily.value: timedelta(days=3),
    EmailMode.weekly.value: timedelta(days=14),
}
# Rows one digest email lists before it says "and N more".
DIGEST_MAX_ITEMS = 50
# Bound on one sweep, so a backlog is worked off over several minutes.
INSTANT_BATCH = 500

EMAIL_FOOTER = (
    "You get this email because of your notification settings in tripl (Profile > Notifications)."
)

_email_mode: ColumnElement[str] = func.coalesce(
    UserNotificationPrefs.email_mode, DEFAULT_EMAIL_MODE
)
_mentions_email = func.coalesce(UserNotificationPrefs.mentions_email, DEFAULT_MENTIONS_EMAIL)
_IS_MENTION = Notification.kind == NotificationKind.mention.value


@dataclass(frozen=True)
class _Row:
    id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    project_name: str
    title: str
    body: str
    url: str
    email: str
    user_name: str


# --- entry points ---------------------------------------------------------------


@celery_app.task(name="tripl.worker.tasks.notification_email.send_notification_emails")  # type: ignore[untyped-decorator]
def send_notification_emails(notification_ids: list[str] | None = None) -> dict[str, object]:
    """Instant emails: for ``notification_ids``, or (beat, no ids) a sweep of recent rows."""
    ids = _parse_ids(notification_ids) if notification_ids is not None else None
    session = _get_sync_session()
    try:
        return _deliver(session, digest=None, ids=ids, now=datetime.now(UTC))
    finally:
        session.close()


@celery_app.task(name="tripl.worker.tasks.notification_email.send_notification_digest")  # type: ignore[untyped-decorator]
def send_notification_digest(mode: str) -> dict[str, object]:
    """The daily / weekly digest (beat ``send-notification-digest-daily`` / ``-weekly``)."""
    if mode not in DIGEST_LOOKBACK:
        return {"status": "invalid_mode", "mode": mode}
    session = _get_sync_session()
    try:
        return _deliver(session, digest=mode, ids=None, now=datetime.now(UTC))
    finally:
        session.close()


def enqueue_notification_emails(notification_ids: Iterable[uuid.UUID]) -> None:
    """Queue the instant path for rows a ``notify`` call just COMMITTED; never raises.

    Safe to call for every notification: the task itself decides who wants an
    instant email. The per-minute sweep is the safety net, so a broker hiccup
    here only delays an email.
    """
    ids = [str(notification_id) for notification_id in notification_ids]
    if not ids:
        return
    try:
        send_notification_emails.delay(ids)
    except Exception:  # noqa: BLE001 — never fail the write that produced the rows
        logger.exception("Could not enqueue notification emails for %d row(s)", len(ids))


# --- the one delivery routine ----------------------------------------------------


def _parse_ids(values: Sequence[str]) -> list[uuid.UUID]:
    parsed: list[uuid.UUID] = []
    for value in values:
        try:
            parsed.append(uuid.UUID(str(value)))
        except ValueError, TypeError, AttributeError:
            continue
    return parsed


def _deliver(
    session: Session,
    *,
    digest: str | None,
    ids: list[uuid.UUID] | None,
    now: datetime,
) -> dict[str, object]:
    path = digest or "instant"
    email_config = app_settings_service.get_email_config_sync(session)
    try:
        from_address = alert_owner_routing.owner_email_sender(email_config)
    except alert_owner_routing.OwnerEmailUnavailable:
        # SMTP missing: in-app still works, nothing to report, nothing stamped.
        return {"status": "smtp_unavailable", "path": path}

    rows = _candidates(session, digest=digest, ids=ids, now=now)
    if not rows:
        return {"status": "done", "path": path, "sent": 0, "failed": 0}
    base_url = app_settings_service.get_runtime_config_sync(session).app_base_url

    batches: list[list[_Row]]
    if digest is None:
        batches = [[row] for row in rows]
    else:
        by_user: dict[uuid.UUID, list[_Row]] = {}
        for row in rows:
            by_user.setdefault(row.user_id, []).append(row)
        batches = list(by_user.values())

    counts = {"sent": 0, "failed": 0, "already": 0}
    for batch in batches:
        claimed = _claim(session, [row.id for row in batch], now=now, digest=digest is not None)
        rows_to_send = [row for row in batch if row.id in claimed]
        if not rows_to_send:
            counts["already"] += len(batch)
            continue
        subject, body = _render(rows_to_send, base_url=base_url, digest=digest)
        try:
            alert_owner_routing.send_owner_email(
                email_config,
                from_address=from_address,
                recipient=rows_to_send[0].email,
                subject=subject,
                body=body,
            )
            counts["sent"] += 1
        except Exception as exc:  # noqa: BLE001 — one user's failure never stops the others
            logger.warning(
                "Notification email (%s) to user %s failed: %s",
                path,
                rows_to_send[0].user_id,
                exc,
            )
            session.rollback()
            _release(session, [row.id for row in rows_to_send], stamp=now)
            counts["failed"] += 1
    return {"status": "done", "path": path, **counts}


def _candidates(
    session: Session,
    *,
    digest: str | None,
    ids: list[uuid.UUID] | None,
    now: datetime,
) -> list[_Row]:
    """Unemailed rows this path should send, members-only, oldest first."""
    membership = (
        select(ProjectMember.id)
        .where(
            ProjectMember.project_id == Notification.project_id,
            ProjectMember.user_id == Notification.user_id,
        )
        .exists()
    )
    conditions = [
        Notification.emailed_at.is_(None),
        Project.is_demo.is_(False),
        or_(User.role == UserRole.owner.value, membership),
    ]
    if digest is None:
        conditions.append(
            or_(
                and_(_IS_MENTION, _mentions_email.is_(True)),
                and_(~_IS_MENTION, _email_mode == EmailMode.instant.value),
            )
        )
        if ids is not None:
            if not ids:
                return []
            conditions.append(Notification.id.in_(ids))
        conditions.append(Notification.created_at >= now - INSTANT_LOOKBACK)
    else:
        conditions.extend(
            [
                _email_mode == digest,
                Notification.read_at.is_(None),
                Notification.created_at >= now - DIGEST_LOOKBACK[digest],
                # Mention emails switched off means off, digest included.
                or_(~_IS_MENTION, _mentions_email.is_(True)),
            ]
        )
    stmt = (
        select(
            Notification.id,
            Notification.user_id,
            Notification.project_id,
            Project.name,
            Notification.title,
            Notification.body,
            Notification.url,
            User.email,
            User.name,
        )
        .join(User, User.id == Notification.user_id)
        .join(Project, Project.id == Notification.project_id)
        .outerjoin(UserNotificationPrefs, UserNotificationPrefs.user_id == Notification.user_id)
        .where(*conditions)
        .order_by(Notification.user_id, Notification.created_at, Notification.id)
    )
    if digest is None:
        stmt = stmt.limit(INSTANT_BATCH)
    return [
        _Row(
            id=row_id,
            user_id=user_id,
            project_id=project_id,
            project_name=project_name or "tripl",
            title=title or "",
            body=body or "",
            url=url or "",
            email=email,
            user_name=alert_owner_routing.display_name(name, email or ""),
        )
        for (
            row_id,
            user_id,
            project_id,
            project_name,
            title,
            body,
            url,
            email,
            name,
        ) in session.execute(stmt).all()
        if email and "@" in email
    ]


def _claim(
    session: Session, row_ids: list[uuid.UUID], *, now: datetime, digest: bool
) -> set[uuid.UUID]:
    """Stamp ``emailed_at`` on the rows still unstamped; the ids this run now owns.

    One conditional UPDATE per row (``rowcount`` says who won it), committed
    before anything is sent, so a concurrent run sees the stamp. A digest also
    re-checks ``read_at``: a row read in the app since it was selected is left
    out of the email.
    """
    claimed: set[uuid.UUID] = set()
    for row_id in row_ids:
        conditions = [Notification.id == row_id, Notification.emailed_at.is_(None)]
        if digest:
            conditions.append(Notification.read_at.is_(None))
        result = session.execute(
            update(Notification)
            .where(*conditions)
            .values(emailed_at=now)
            .execution_options(synchronize_session=False)
        )
        if int(getattr(result, "rowcount", 0) or 0) == 1:
            claimed.add(row_id)
    session.commit()
    return claimed


def _release(session: Session, row_ids: list[uuid.UUID], *, stamp: datetime) -> None:
    """Undo this run's claim after a failed send, so a later run retries."""
    try:
        session.execute(
            update(Notification)
            .where(Notification.id.in_(row_ids), Notification.emailed_at == stamp)
            .values(emailed_at=None)
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except Exception:  # noqa: BLE001 — the rows stay stamped; worst case one email is lost
        logger.exception("Could not release %d notification email claim(s)", len(row_ids))
        session.rollback()


# --- rendering -------------------------------------------------------------------


def _single_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _link(base_url: str, path: str) -> str | None:
    if not base_url or not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _item_lines(row: _Row, base_url: str) -> list[str]:
    lines = [f"- {_single_line(row.title)}"]
    if row.body.strip():
        lines.append(f"  {_single_line(row.body)}")
    link = _link(base_url, row.url)
    if link:
        lines.append(f"  {link}")
    return lines


def _render(rows: list[_Row], *, base_url: str, digest: str | None) -> tuple[str, str]:
    """(subject, plain-text body) of one email."""
    first = rows[0]
    if digest is None:
        subject = _single_line(f"[{first.project_name}] {first.title}")
        parts = [f"Hi {first.user_name},", first.title.strip()]
        if first.body.strip():
            parts.append(first.body.strip())
        link = _link(base_url, first.url)
        if link:
            parts.append(link)
        parts.append(f"Project: {first.project_name}\n{EMAIL_FOOTER}")
        return subject, "\n\n".join(parts) + "\n"

    period = "today" if digest == EmailMode.daily.value else "this week"
    count = len(rows)
    subject = _single_line(f"[tripl] {count} unread notification{'s' if count != 1 else ''}")
    parts = [f"Hi {first.user_name},", f"Here is what happened {period} in tripl:"]
    shown = rows[:DIGEST_MAX_ITEMS]
    by_project: dict[uuid.UUID, list[_Row]] = {}
    for row in shown:
        by_project.setdefault(row.project_id, []).append(row)
    for project_rows in by_project.values():
        lines = [project_rows[0].project_name]
        for row in project_rows:
            lines.extend(_item_lines(row, base_url))
        parts.append("\n".join(lines))
    if count > len(shown):
        parts.append(f"...and {count - len(shown)} more in the app.")
    parts.append(EMAIL_FOOTER)
    return subject, "\n\n".join(parts) + "\n"


__all__ = [
    "enqueue_notification_emails",
    "send_notification_digest",
    "send_notification_emails",
]
