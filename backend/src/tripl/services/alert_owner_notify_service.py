"""The editor's one-off "Notify owners" on an incident or a signal (F07, #260).

Unlike the rule-driven follow-up (``worker.tasks.alert_owner_notify``) this runs
in the request: the editor clicked a button and wants to know who was told, so
each email is sent through the same SMTP path in a worker thread and the route
answers with the real per-owner outcome. Every attempt is recorded in
``alert_owner_notifications`` with ``source='manual'``, no delivery, and a
``target_key`` naming the incident or the signal.

Two guards keep the button from becoming a mail cannon. A COOLDOWN: an owner
already emailed about the same target (a ``sent`` manual row with the same
``target_key``) in the last :data:`MANUAL_NOTIFY_COOLDOWN` is skipped with the
reason "notified N minutes ago" — reported in the response, not recorded, so it
never extends its own cooldown. A CAP: at most :data:`MANUAL_NOTIFY_MAX_OWNERS`
owners per request, in resolution order; the rest are not contacted. The
cooldown is a read-then-send, so two clicks landing in the same instant can
both send — acceptable for a human-paced button.

Owners are resolved exactly as for a rule (``alert_owner_routing``): current
project members with an email. SMTP not configured, or a demo project, records
each owner as ``skipped`` with the reason instead of failing the request.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    get_default_items_template,
    percent_delta_of,
)
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem, trim_scope_name
from tripl.models.alert_owner_notification import (
    OWNER_NOTIFICATION_TARGET_KEY_MAX_LEN,
    AlertOwnerNotification,
    AlertOwnerNotificationSource,
    AlertOwnerNotificationStatus,
)
from tripl.models.domain_enums import MetricScopeType
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.project import Project
from tripl.models.user import User
from tripl.schemas.alert_owner import NotifyOwnersResponse
from tripl.schemas.event_metric import SignalTriageScope
from tripl.services import alert_owner_routing, app_settings_service
from tripl.services.anomaly_attribution_service import _anomaly_in_project
from tripl.services.project_lookup import resolve_project

# How many of an incident's newest items the email lists. An incident is one
# scope over time, so the newest few say everything the owner needs.
INCIDENT_EMAIL_MAX_ITEMS = 10
# One owner, one target: at most one manual email per this window.
MANUAL_NOTIFY_COOLDOWN = timedelta(minutes=10)
# Owners contacted per "Notify owners" request, at most.
MANUAL_NOTIFY_MAX_OWNERS = 20


@dataclass(frozen=True)
class ManualNotifyResult:
    project: Project
    response: NotifyOwnersResponse
    # What the audit row calls the target.
    target_name: str


def _items_text(items: list[AlertDeliveryItem]) -> str:
    from tripl.worker.tasks.alerts_messages import _build_items_text

    return _build_items_text(
        items,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template=get_default_items_template(ALERT_MESSAGE_FORMAT_PLAIN),
        session=None,
    )


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def incident_target_key(correlation_group_id: uuid.UUID) -> str:
    return f"incident:{correlation_group_id}"


def signal_target_key(anomaly: MetricAnomaly) -> str:
    """One signal, as the cooldown sees it: scope, scan and bucket."""
    scan = str(anomaly.scan_config_id) if anomaly.scan_config_id is not None else "none"
    key = (
        f"signal:{anomaly.scope_type}:{anomaly.scope_ref}:{scan}:"
        f"{_as_aware(anomaly.bucket).isoformat()}"
    )
    if len(key) > OWNER_NOTIFICATION_TARGET_KEY_MAX_LEN:
        key = "signal:sha256:" + hashlib.sha256(key.encode()).hexdigest()
    return key


def cooldown_reason(last_sent_at: datetime, now: datetime) -> str:
    minutes = max(1, int((now - _as_aware(last_sent_at)).total_seconds() // 60))
    return f"notified {minutes} minute{'' if minutes == 1 else 's'} ago"


async def _recently_notified(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target_key: str,
    user_ids: list[uuid.UUID],
    now: datetime,
) -> dict[uuid.UUID, datetime]:
    """Owner -> when they were last emailed about this target, inside the cooldown."""
    if not user_ids:
        return {}
    rows = await session.execute(
        select(AlertOwnerNotification.user_id, func.max(AlertOwnerNotification.sent_at))
        .where(
            AlertOwnerNotification.project_id == project_id,
            AlertOwnerNotification.target_key == target_key,
            AlertOwnerNotification.source == AlertOwnerNotificationSource.manual.value,
            AlertOwnerNotification.status == AlertOwnerNotificationStatus.sent.value,
            AlertOwnerNotification.sent_at >= now - MANUAL_NOTIFY_COOLDOWN,
            AlertOwnerNotification.user_id.in_(user_ids),
        )
        .group_by(AlertOwnerNotification.user_id)
    )
    return {
        user_id: _as_aware(sent_at)
        for user_id, sent_at in rows.all()
        if user_id is not None and sent_at is not None
    }


async def _send_all(
    session: AsyncSession,
    *,
    project: Project,
    owners: list[alert_owner_routing.OwnerContact],
    actor: User,
    correlation_group_id: uuid.UUID | None,
    title: str,
    headline: str,
    items_text: str,
    target_key: str,
) -> NotifyOwnersResponse:
    now = datetime.now(UTC)
    owners = owners[:MANUAL_NOTIFY_MAX_OWNERS]
    recent = await _recently_notified(
        session,
        project_id=project.id,
        target_key=target_key,
        user_ids=[contact.user_id for contact in owners],
        now=now,
    )
    skip_reason: str | None = None
    email_config: app_settings_service.EmailConfig | None = None
    from_address = ""
    if project.is_demo:
        skip_reason = alert_owner_routing.DEMO_PROJECT_SKIPPED
    elif any(contact.user_id not in recent for contact in owners):
        email_config = await app_settings_service.get_email_config(session)
        try:
            from_address = alert_owner_routing.owner_email_sender(email_config)
        except alert_owner_routing.OwnerEmailUnavailable as exc:
            skip_reason = str(exc)

    rows: list[tuple[AlertOwnerNotification, str]] = []
    for contact in owners:
        last_sent_at = recent.get(contact.user_id)
        if last_sent_at is not None:
            # Reported, not recorded: a skipped click must not extend the
            # cooldown or clutter the history.
            cooled = AlertOwnerNotification(
                project_id=project.id,
                user_id=contact.user_id,
                email=contact.email,
                source=AlertOwnerNotificationSource.manual.value,
                status=AlertOwnerNotificationStatus.skipped.value,
                error=cooldown_reason(last_sent_at, now),
            )
            rows.append((cooled, contact.name))
            continue
        row = AlertOwnerNotification(
            project_id=project.id,
            delivery_id=None,
            correlation_group_id=correlation_group_id,
            target_key=target_key,
            user_id=contact.user_id,
            email=contact.email,
            source=AlertOwnerNotificationSource.manual.value,
            status=AlertOwnerNotificationStatus.skipped.value,
            error=skip_reason,
            triggered_by=actor.id,
        )
        if skip_reason is None and email_config is not None:
            subject, body = alert_owner_routing.build_owner_email(
                project_name=project.name,
                owner_name=contact.name,
                title=title,
                headline=headline,
                items_text=items_text,
            )
            try:
                await asyncio.to_thread(
                    alert_owner_routing.send_owner_email,
                    email_config,
                    from_address=from_address,
                    recipient=contact.email,
                    subject=subject,
                    body=body,
                )
            except Exception as exc:  # noqa: BLE001 — recorded on the row
                row.status = AlertOwnerNotificationStatus.failed.value
                row.error = alert_owner_routing.trim_error(exc)
            else:
                row.status = AlertOwnerNotificationStatus.sent.value
                row.sent_at = datetime.now(UTC)
        session.add(row)
        rows.append((row, contact.name))
    await session.flush()
    return NotifyOwnersResponse(
        owners=[alert_owner_routing.notification_to_response(row, name) for row, name in rows]
    )


def _actor_label(actor: User) -> str:
    return alert_owner_routing.display_name(actor.name, actor.email)


async def notify_incident_owners(
    session: AsyncSession,
    slug: str,
    correlation_group_id: uuid.UUID,
    actor: User,
) -> ManualNotifyResult:
    """Email the owners of one inbox incident, now. Does not commit."""
    project = await resolve_project(session, slug)
    items = list(
        (
            await session.execute(
                select(AlertDeliveryItem)
                .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
                .where(
                    AlertDelivery.project_id == project.id,
                    AlertDeliveryItem.correlation_group_id == correlation_group_id,
                )
                .order_by(AlertDeliveryItem.bucket.desc(), AlertDeliveryItem.id.desc())
                .limit(INCIDENT_EMAIL_MAX_ITEMS)
            )
        ).scalars()
    )
    if not items:
        raise HTTPException(status_code=404, detail="Alert correlation group not found")
    latest = items[0]
    (owners,) = await alert_owner_routing.resolve_owners(
        session, project.id, [alert_owner_routing.owned_scope_of(latest)]
    )
    response = await _send_all(
        session,
        project=project,
        owners=owners,
        actor=actor,
        correlation_group_id=correlation_group_id,
        title=f"Incident on what you own — {latest.scope_name}",
        headline=(
            f"{_actor_label(actor)} asked you to look at an alerting incident on "
            f"{latest.scope_name}, which you own."
        ),
        items_text=_items_text(items),
        target_key=incident_target_key(correlation_group_id),
    )
    return ManualNotifyResult(project=project, response=response, target_name=latest.scope_name)


async def _scope_name(session: AsyncSession, anomaly: MetricAnomaly) -> str:
    scope_type = str(anomaly.scope_type)
    name: str | None = None
    if scope_type == MetricScopeType.event.value and anomaly.event_id is not None:
        name = await session.scalar(select(Event.name).where(Event.id == anomaly.event_id))
    elif scope_type == MetricScopeType.event_type.value and anomaly.event_type_id is not None:
        name = await session.scalar(
            select(EventType.display_name).where(EventType.id == anomaly.event_type_id)
        )
    elif scope_type == MetricScopeType.metric.value:
        try:
            metric_id = uuid.UUID(anomaly.scope_ref)
        except ValueError:
            metric_id = None
        if metric_id is not None:
            name = await session.scalar(
                select(MetricDefinition.display_name).where(MetricDefinition.id == metric_id)
            )
    return trim_scope_name(name or f"{scope_type} {anomaly.scope_ref}")


async def notify_signal_owners(
    session: AsyncSession,
    slug: str,
    data: SignalTriageScope,
    actor: User,
) -> ManualNotifyResult:
    """Email the owners of one signal no rule routed, now. Does not commit."""
    project = await resolve_project(session, slug)
    conditions = [
        MetricAnomaly.scope_type == data.scope_type.value,
        MetricAnomaly.scope_ref == data.scope_ref,
        MetricAnomaly.bucket == data.bucket,
    ]
    conditions.append(
        MetricAnomaly.scan_config_id == data.scan_config_id
        if data.scan_config_id is not None
        else MetricAnomaly.scan_config_id.is_(None)
    )
    candidate = await session.scalar(select(MetricAnomaly).where(*conditions).limit(1))
    anomaly = (
        await _anomaly_in_project(session, project.id, candidate.id)
        if candidate is not None
        else None
    )
    if anomaly is None:
        raise HTTPException(status_code=404, detail="Signal not found")

    scope_name = await _scope_name(session, anomaly)
    # A transient item, never added to the session: it exists only so the
    # email lists the signal in the same line format a delivery's items use.
    line_item = AlertDeliveryItem(
        id=uuid.uuid4(),
        scope_type=anomaly.scope_type,
        scope_ref=anomaly.scope_ref,
        scope_name=scope_name,
        event_type_id=anomaly.event_type_id,
        event_id=anomaly.event_id,
        bucket=anomaly.bucket,
        direction=anomaly.direction,
        actual_count=anomaly.actual_count,
        expected_count=anomaly.expected_count,
        absolute_delta=abs(anomaly.actual_count - anomaly.expected_count),
        percent_delta=percent_delta_of(anomaly.actual_count, anomaly.expected_count),
    )
    (owners,) = await alert_owner_routing.resolve_owners(
        session, project.id, [alert_owner_routing.owned_scope_of(anomaly)]
    )
    response = await _send_all(
        session,
        project=project,
        owners=owners,
        actor=actor,
        correlation_group_id=None,
        title=f"Signal on what you own — {scope_name}",
        headline=(
            f"{_actor_label(actor)} asked you to look at a signal on {scope_name}, "
            "which you own. No alert rule routed it."
        ),
        items_text=_items_text([line_item]),
        target_key=signal_target_key(anomaly),
    )
    return ManualNotifyResult(project=project, response=response, target_name=scope_name)
