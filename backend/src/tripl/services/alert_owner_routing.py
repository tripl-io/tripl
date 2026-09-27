"""Who owns what an alert is about, and how they are told (F07, #260).

An alert scope is OWNED when it resolves to an event type with
``EventTypeOwner`` rows, or to a catalog metric with ``MetricDefinition.owner_id``:

* ``event_type`` — the type itself (the item's ``event_type_id``, else its
  ``scope_ref``);
* ``event`` — the event's type (the item's ``event_type_id``, else the event row);
* ``metric`` — the metric's ``owner_id`` (``scope_ref`` is the definition id);
* any other scope that carries an ``event_type_id`` / ``event_id`` (the drift,
  release-regression and lifecycle families) resolves the same way, since it is
  about that event type too;
* ``project_total`` and ``source_freshness`` have no owner.

Owners are then filtered to CURRENT project members (the instance owner counts,
as they see every project) with a usable email address: an owner who has since
lost access is not emailed about a project they can no longer open.

The resolution is written ONCE, as a generator that yields statements and
receives their rows, and driven by a sync runner (the Celery worker) and an
async one (the API), so the two sides cannot disagree about who an owner is.

Owner emails go out through the same SMTP path an email destination uses
(``worker.tasks.alerts._send_email_message``), from the instance's Default From.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.alerting_validation import validate_sender_address
from tripl.models.alert_owner_notification import AlertOwnerNotification
from tripl.models.domain_enums import MetricScopeType, UserRole
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project_member import ProjectMember
from tripl.models.user import User
from tripl.schemas.alert_owner import AlertOwnerNotificationResponse, AlertOwnerRef
from tripl.services import app_settings_service

# Scopes that are about the whole project or a scan, never about something a
# person owns.
_UNOWNED_SCOPES = frozenset(
    {MetricScopeType.project_total.value, MetricScopeType.source_freshness.value}
)

SMTP_NOT_CONFIGURED = "SMTP is not configured; owner email skipped."
NO_FROM_ADDRESS = "SMTP_FROM_ADDRESS is unset; owner email skipped."
INVALID_FROM_ADDRESS = "The Default From address is invalid; owner email skipped."
DEMO_PROJECT_SKIPPED = "Demo projects send no email; owner email skipped."

OWNER_EMAIL_FOOTER = (
    "You get this email because you own the affected event type or metric in tripl."
)


@dataclass(frozen=True)
class OwnedScope:
    """What an alert item (or a signal) is about, as far as ownership goes."""

    scope_type: str
    scope_ref: str
    event_type_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None


@dataclass(frozen=True)
class OwnerContact:
    user_id: uuid.UUID
    name: str
    email: str

    def as_ref(self) -> AlertOwnerRef:
        return AlertOwnerRef(user_id=self.user_id, name=self.name)


def owned_scope_of(row: Any) -> OwnedScope:
    """The ownership key of anything shaped like an alert item or a signal."""
    return OwnedScope(
        scope_type=str(row.scope_type),
        scope_ref=str(row.scope_ref),
        event_type_id=getattr(row, "event_type_id", None),
        event_id=getattr(row, "event_id", None),
    )


def display_name(name: str | None, email: str) -> str:
    """A person's name for an owners line: their name, else the address's local part."""
    if name and name.strip():
        return name.strip()
    return email.split("@", 1)[0]


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError, TypeError, AttributeError:
        return None


def _has_email(email: str | None) -> bool:
    return bool(email) and "@" in str(email)


@dataclass(frozen=True)
class _Target:
    event_type_id: uuid.UUID | None
    event_id: uuid.UUID | None
    metric_id: uuid.UUID | None


def _target_of(scope: OwnedScope) -> _Target:
    if scope.scope_type in _UNOWNED_SCOPES:
        return _Target(None, None, None)
    if scope.scope_type == MetricScopeType.metric.value:
        return _Target(None, None, _as_uuid(scope.scope_ref))
    event_type_id = scope.event_type_id
    if event_type_id is None and scope.scope_type == MetricScopeType.event_type.value:
        event_type_id = _as_uuid(scope.scope_ref)
    event_id = scope.event_id
    if event_id is None and scope.scope_type == MetricScopeType.event.value:
        event_id = _as_uuid(scope.scope_ref)
    return _Target(event_type_id, None if event_type_id is not None else event_id, None)


_Resolution = Generator[Select[Any], list[Any], list[list[OwnerContact]]]


def _resolution(project_id: uuid.UUID, scopes: Sequence[OwnedScope]) -> _Resolution:
    """Owners per scope, in ``scopes`` order; see the module docstring.

    Yields at most four bulk statements whatever the number of scopes.
    """
    targets = [_target_of(scope) for scope in scopes]

    event_ids = {target.event_id for target in targets if target.event_id is not None}
    type_of_event: dict[uuid.UUID, uuid.UUID] = {}
    if event_ids:
        rows = yield select(Event.id, Event.event_type_id).where(
            Event.project_id == project_id, Event.id.in_(event_ids)
        )
        type_of_event = {event_id: type_id for event_id, type_id in rows}

    type_per_scope = [
        target.event_type_id
        if target.event_type_id is not None
        else (type_of_event.get(target.event_id) if target.event_id is not None else None)
        for target in targets
    ]
    type_ids = {type_id for type_id in type_per_scope if type_id is not None}
    metric_ids = {target.metric_id for target in targets if target.metric_id is not None}

    by_type: dict[uuid.UUID, list[OwnerContact]] = {}
    if type_ids:
        rows = yield (
            select(EventTypeOwner.event_type_id, User.id, User.name, User.email)
            .join(User, User.id == EventTypeOwner.user_id)
            .join(EventType, EventType.id == EventTypeOwner.event_type_id)
            .where(EventType.project_id == project_id, EventTypeOwner.event_type_id.in_(type_ids))
            .order_by(EventTypeOwner.created_at, User.id)
        )
        for type_id, user_id, name, email in rows:
            by_type.setdefault(type_id, []).append(
                OwnerContact(user_id=user_id, name=display_name(name, email or ""), email=email)
            )

    by_metric: dict[uuid.UUID, list[OwnerContact]] = {}
    if metric_ids:
        rows = yield (
            select(MetricDefinition.id, User.id, User.name, User.email)
            .join(User, User.id == MetricDefinition.owner_id)
            .where(MetricDefinition.project_id == project_id, MetricDefinition.id.in_(metric_ids))
        )
        for metric_id, user_id, name, email in rows:
            by_metric.setdefault(metric_id, []).append(
                OwnerContact(user_id=user_id, name=display_name(name, email or ""), email=email)
            )

    candidates = {
        contact.user_id
        for contacts in (*by_type.values(), *by_metric.values())
        for contact in contacts
    }
    eligible: set[uuid.UUID] = set()
    if candidates:
        rows = yield (
            select(User.id)
            .outerjoin(
                ProjectMember,
                and_(ProjectMember.user_id == User.id, ProjectMember.project_id == project_id),
            )
            .where(
                User.id.in_(candidates),
                or_(User.role == UserRole.owner.value, ProjectMember.id.is_not(None)),
            )
        )
        eligible = {user_id for (user_id,) in rows}

    result: list[list[OwnerContact]] = []
    for type_id, target in zip(type_per_scope, targets, strict=True):
        seen: set[uuid.UUID] = set()
        owners: list[OwnerContact] = []
        pool = [
            *(by_type.get(type_id, []) if type_id is not None else []),
            *(by_metric.get(target.metric_id, []) if target.metric_id is not None else []),
        ]
        for contact in pool:
            if contact.user_id in seen or contact.user_id not in eligible:
                continue
            if not _has_email(contact.email):
                continue
            seen.add(contact.user_id)
            owners.append(contact)
        result.append(owners)
    return result


def resolve_owners_sync(
    session: Session, project_id: uuid.UUID, scopes: Sequence[OwnedScope]
) -> list[list[OwnerContact]]:
    """Owners per scope, for the Celery worker."""
    steps = _resolution(project_id, scopes)
    try:
        statement = next(steps)
        while True:
            statement = steps.send(list(session.execute(statement).all()))
    except StopIteration as done:
        return list(done.value)


async def resolve_owners(
    session: AsyncSession, project_id: uuid.UUID, scopes: Sequence[OwnedScope]
) -> list[list[OwnerContact]]:
    """Owners per scope, for the API."""
    steps = _resolution(project_id, scopes)
    try:
        statement = next(steps)
        while True:
            statement = steps.send(list((await session.execute(statement)).all()))
    except StopIteration as done:
        return list(done.value)


# --- sending -----------------------------------------------------------------


class OwnerEmailUnavailable(ValueError):
    """Owner email cannot be sent on this instance; the rows are ``skipped``."""


def owner_email_sender(email_config: app_settings_service.EmailConfig) -> str:
    """The From: address owner emails go out under, or raise ``OwnerEmailUnavailable``.

    The instance's Default From, validated the way the email destinations
    validate it (``validate_sender_address`` accepts a display name).
    """
    if not email_config.smtp_host:
        raise OwnerEmailUnavailable(SMTP_NOT_CONFIGURED)
    if not email_config.smtp_from_address:
        raise OwnerEmailUnavailable(NO_FROM_ADDRESS)
    try:
        return validate_sender_address(email_config.smtp_from_address)
    except ValueError as exc:
        raise OwnerEmailUnavailable(INVALID_FROM_ADDRESS) from exc


def send_owner_email(
    email_config: app_settings_service.EmailConfig,
    *,
    from_address: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    """One plain-text email to one owner, through the email destinations' SMTP path.

    Resolved through the ``alerts`` module at call time so the suite's patch of
    ``alerts._send_email_message`` covers this path too, and blocking: the API
    calls it through ``asyncio.to_thread``.
    """
    from tripl.worker.tasks import alerts

    alerts._send_email_message(
        smtp_host=email_config.smtp_host,
        smtp_port=email_config.smtp_port,
        smtp_username=email_config.smtp_username,
        smtp_password=email_config.smtp_password,
        smtp_security=email_config.smtp_security,
        from_address=from_address,
        recipients=[recipient],
        subject=subject,
        body=body,
    )


def _single_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def build_owner_email(
    *,
    project_name: str,
    owner_name: str,
    title: str,
    headline: str,
    items_text: str,
    link: str | None = None,
) -> tuple[str, str]:
    """(subject, plain-text body) of one owner email."""
    subject = _single_line(f"[{project_name or 'tripl'}] {title}")
    parts = [f"Hi {owner_name},", headline]
    if items_text.strip():
        parts.append(items_text.rstrip())
    if link:
        parts.append(link)
    parts.append(f"Project: {project_name}\n{OWNER_EMAIL_FOOTER}")
    return subject, "\n\n".join(parts) + "\n"


def trim_error(value: object, limit: int = 1000) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- read side ----------------------------------------------------------------


async def load_delivery_owner_notifications(
    session: AsyncSession, delivery_id: uuid.UUID
) -> list[AlertOwnerNotificationResponse]:
    """The owner emails a delivery produced, oldest first, for its detail view."""
    rows = (
        await session.execute(
            select(AlertOwnerNotification, User.name)
            .outerjoin(User, User.id == AlertOwnerNotification.user_id)
            .where(AlertOwnerNotification.delivery_id == delivery_id)
            .order_by(AlertOwnerNotification.created_at, AlertOwnerNotification.id)
        )
    ).all()
    return [notification_to_response(row, name) for row, name in rows]


def notification_to_response(
    row: AlertOwnerNotification, user_name: str | None
) -> AlertOwnerNotificationResponse:
    return AlertOwnerNotificationResponse(
        user_id=row.user_id,
        name=display_name(user_name, row.email) if row.user_id is not None else None,
        email=row.email,
        status=row.status,
        error=row.error,
        sent_at=row.sent_at,
    )


async def owner_refs_for(
    session: AsyncSession, project_id: uuid.UUID, rows: Iterable[Any]
) -> list[list[AlertOwnerRef]]:
    """Display owners for each item/signal-shaped row, in order."""
    scopes = [owned_scope_of(row) for row in rows]
    if not scopes:
        return []
    resolved = await resolve_owners(session, project_id, scopes)
    return [[contact.as_ref() for contact in owners] for owners in resolved]
