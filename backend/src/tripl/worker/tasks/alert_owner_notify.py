"""Email the owners of what a sent delivery was about (F07, #260).

Enqueued by the two send paths — ``alerts.send_alert_delivery`` and
``alert_digest_send.send_alert_digest`` — AFTER the delivery is committed as
``sent`` and only when its rule has ``notify_owners`` on — and again from their
``already_sent`` early returns, so a worker killed between the delivery's commit
and this enqueue still gets its owners told on the redelivered message (the
claim below makes the repeat harmless). It never touches the delivery row:
whatever happens here, the rule's own delivery stays sent.

One email per owner per delivery, carrying the delivery's items that owner owns
(plain text, the same item lines the plain email template renders). A digest
member is its own delivery, so an owner whose items sit in several rules' parts
of one digest message gets one email per rule delivery — accepted, and
documented, rather than merging follow-ups across rules.

The claim: a row in ``alert_owner_notifications`` is inserted as ``pending``
BEFORE the send and committed, and the ``(delivery_id, user_id)`` unique key
makes that insert the claim. Only a ``sent`` row and a ``pending`` row younger
than ``OWNER_NOTIFICATION_PENDING_LEASE`` (15 minutes) count as claimed; a later
run — a redelivered message, the send task's already-sent re-run — re-claims a
``skipped`` (SMTP configured since), ``failed`` or stale ``pending`` row in
place with a conditional UPDATE, and sends.

When owner email cannot go out at all (SMTP unset, no Default From, a demo
project) each owner still gets a row, as ``skipped`` with the reason, so the
delivery detail says why nobody was emailed. An exception for one owner — a
render, the SMTP call, a database write — marks that owner ``failed`` and the
run moves on to the next owner.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from tripl.alert_templates import (
    ALERT_MESSAGE_FORMAT_PLAIN,
    get_default_items_template,
    get_digest_items_template,
)
from tripl.models.alert_delivery import AlertDelivery, AlertDeliveryStatus
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_owner_notification import (
    OWNER_NOTIFICATION_PENDING_LEASE,
    AlertOwnerNotification,
    AlertOwnerNotificationSource,
    AlertOwnerNotificationStatus,
)
from tripl.models.alert_rule import AlertRule
from tripl.models.project import Project
from tripl.services import alert_owner_routing, app_settings_service
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.alert_owner_notify.notify_owners",
    bind=True,
)
def notify_owners(self: object, delivery_id: str) -> dict[str, object]:
    session = _get_sync_session()
    try:
        return _notify_owners(session, uuid.UUID(delivery_id))
    finally:
        session.close()


def _rule_is_muted(rule: AlertRule, now: datetime) -> bool:
    muted_until = rule.muted_until
    if muted_until is None:
        return False
    if muted_until.tzinfo is None:
        muted_until = muted_until.replace(tzinfo=UTC)
    return muted_until > now


def _owned_items(
    session: Session, project_id: uuid.UUID, items: list[AlertDeliveryItem]
) -> list[tuple[alert_owner_routing.OwnerContact, list[AlertDeliveryItem]]]:
    """Each owner with the items of this delivery they own, first-seen order."""
    resolved = alert_owner_routing.resolve_owners_sync(
        session, project_id, [alert_owner_routing.owned_scope_of(item) for item in items]
    )
    grouped: dict[uuid.UUID, tuple[alert_owner_routing.OwnerContact, list[AlertDeliveryItem]]]
    grouped = {}
    for item, owners in zip(items, resolved, strict=True):
        for contact in owners:
            entry = grouped.setdefault(contact.user_id, (contact, []))
            entry[1].append(item)
    return list(grouped.values())


def _items_text(
    session: Session,
    scan_config_id: uuid.UUID,
    items: list[AlertDeliveryItem],
    *,
    digest: bool,
) -> str:
    from tripl.worker.tasks.alerts_messages import _build_items_text

    template = (
        get_digest_items_template(ALERT_MESSAGE_FORMAT_PLAIN)
        if digest
        else get_default_items_template(ALERT_MESSAGE_FORMAT_PLAIN)
    )
    return _build_items_text(
        items,
        message_format=ALERT_MESSAGE_FORMAT_PLAIN,
        items_template=template,
        session=session,
        scan_config_id=scan_config_id,
        digest=digest,
    )


def _as_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _existing_rows(
    session: Session, delivery_id: uuid.UUID
) -> dict[uuid.UUID, AlertOwnerNotification]:
    """The rows earlier runs left for this delivery, by owner.

    Only a pre-check: the unique key and the conditional UPDATE in
    :func:`_claim` are what actually keep two runs off one owner.
    """
    rows = session.execute(
        select(AlertOwnerNotification).where(AlertOwnerNotification.delivery_id == delivery_id)
    ).scalars()
    return {row.user_id: row for row in rows if row.user_id is not None}


def _holds_claim(row: AlertOwnerNotification, now: datetime) -> bool:
    """True when ``row`` still answers for its owner: sent, or pending inside the lease."""
    if row.status == AlertOwnerNotificationStatus.sent.value:
        return True
    if row.status == AlertOwnerNotificationStatus.pending.value:
        stamp = row.updated_at or row.created_at
        return stamp is None or _as_aware(stamp) > now - OWNER_NOTIFICATION_PENDING_LEASE
    return False


def _claim(
    session: Session,
    *,
    existing: AlertOwnerNotification | None,
    project_id: uuid.UUID,
    delivery_id: uuid.UUID,
    contact: alert_owner_routing.OwnerContact,
    skip_reason: str | None,
    now: datetime,
) -> uuid.UUID | None:
    """Take (or re-take) this owner's row; the row id, or None when another run holds it.

    A new owner is an INSERT whose unique key is the claim. A ``skipped``,
    ``failed`` or stale ``pending`` row is re-claimed in place by a conditional
    UPDATE that also requires the status it was read with, so of two runs
    re-claiming the same row only one sees ``rowcount == 1``. Either way the
    claim is committed before anything is sent: an uncommitted claim is
    invisible to the other run.
    """
    status = (
        AlertOwnerNotificationStatus.skipped.value
        if skip_reason
        else AlertOwnerNotificationStatus.pending.value
    )
    if existing is None:
        row = AlertOwnerNotification(
            project_id=project_id,
            delivery_id=delivery_id,
            user_id=contact.user_id,
            email=contact.email,
            source=AlertOwnerNotificationSource.rule.value,
            status=status,
            error=skip_reason,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # Another run inserted this (delivery, owner) first.
            session.rollback()
            return None
        return row.id

    if _holds_claim(existing, now):
        return None
    lease_cutoff = now - OWNER_NOTIFICATION_PENDING_LEASE
    reclaimed = int(
        getattr(
            session.execute(
                update(AlertOwnerNotification)
                .where(
                    AlertOwnerNotification.id == existing.id,
                    AlertOwnerNotification.status == existing.status,
                    or_(
                        AlertOwnerNotification.status.in_(
                            [
                                AlertOwnerNotificationStatus.skipped.value,
                                AlertOwnerNotificationStatus.failed.value,
                            ]
                        ),
                        and_(
                            AlertOwnerNotification.status
                            == AlertOwnerNotificationStatus.pending.value,
                            AlertOwnerNotification.updated_at < lease_cutoff,
                        ),
                    ),
                )
                .values(
                    status=status,
                    error=skip_reason,
                    email=contact.email,
                    sent_at=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ),
            "rowcount",
            0,
        )
        or 0
    )
    session.commit()
    return existing.id if reclaimed == 1 else None


def _finish(
    session: Session,
    row_id: uuid.UUID,
    *,
    status: AlertOwnerNotificationStatus,
    error: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    session.execute(
        update(AlertOwnerNotification)
        .where(AlertOwnerNotification.id == row_id)
        .values(status=status.value, error=error, sent_at=sent_at, updated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    session.commit()


def _notify_owners(session: Session, delivery_id: uuid.UUID) -> dict[str, object]:
    delivery = session.execute(
        select(AlertDelivery)
        .options(selectinload(AlertDelivery.items))
        .where(AlertDelivery.id == delivery_id)
    ).scalar_one_or_none()
    if delivery is None:
        return {"status": "not_found", "delivery_id": str(delivery_id)}
    if delivery.status != AlertDeliveryStatus.sent.value:
        return {"status": "not_sent", "delivery_id": str(delivery_id)}
    rule = session.get(AlertRule, delivery.rule_id)
    if rule is None or not rule.notify_owners:
        return {"status": "disabled", "delivery_id": str(delivery_id)}
    now = datetime.now(UTC)
    # A rule muted or disabled since its delivery went out sends nothing more.
    if not rule.enabled or _rule_is_muted(rule, now):
        return {"status": "muted", "delivery_id": str(delivery_id)}
    project = session.get(Project, delivery.project_id)
    if project is None:
        return {"status": "not_found", "delivery_id": str(delivery_id)}
    # Plain values up front: a per-owner rollback below expires every loaded
    # object, and nothing after it should need to reload one.
    project_id = project.id
    project_name = project.name
    rule_name = rule.name
    scan_config_id = delivery.scan_config_id

    owned = _owned_items(session, project_id, list(delivery.items))
    if not owned:
        return {"status": "no_owners", "delivery_id": str(delivery_id)}

    existing = _existing_rows(session, delivery_id)

    skip_reason: str | None = None
    email_config: app_settings_service.EmailConfig | None = None
    from_address = ""
    if project.is_demo:
        skip_reason = alert_owner_routing.DEMO_PROJECT_SKIPPED
    else:
        email_config = app_settings_service.get_email_config_sync(session)
        try:
            from_address = alert_owner_routing.owner_email_sender(email_config)
        except alert_owner_routing.OwnerEmailUnavailable as exc:
            skip_reason = str(exc)

    digest = bool(
        isinstance(delivery.payload_snapshot, dict) and delivery.payload_snapshot.get("digest")
    )
    counts = {"sent": 0, "failed": 0, "skipped": 0, "already": 0}
    for contact, items in owned:
        row_id: uuid.UUID | None = None
        try:
            row_id = _claim(
                session,
                existing=existing.get(contact.user_id),
                project_id=project_id,
                delivery_id=delivery_id,
                contact=contact,
                skip_reason=skip_reason,
                now=now,
            )
            if row_id is None:
                counts["already"] += 1
                continue
            if skip_reason or email_config is None:
                counts["skipped"] += 1
                continue

            subject, body = alert_owner_routing.build_owner_email(
                project_name=project_name,
                owner_name=contact.name,
                title=f"{len(items)} alert(s) on what you own — {rule_name}",
                headline=(
                    f'The alert rule "{rule_name}" fired on {len(items)} signal(s) '
                    "you own. Its own destination has the full message."
                ),
                items_text=_items_text(session, scan_config_id, items, digest=digest),
            )
            alert_owner_routing.send_owner_email(
                email_config,
                from_address=from_address,
                recipient=contact.email,
                subject=subject,
                body=body,
            )
            _finish(
                session,
                row_id,
                status=AlertOwnerNotificationStatus.sent,
                sent_at=datetime.now(UTC),
            )
            counts["sent"] += 1
        except Exception as exc:  # noqa: BLE001 — one owner's failure never stops the others
            logger.warning(
                "Owner email for delivery %s to user %s failed: %s",
                delivery_id,
                contact.user_id,
                exc,
            )
            session.rollback()
            counts["failed"] += 1
            if row_id is not None:
                try:
                    _finish(
                        session,
                        row_id,
                        status=AlertOwnerNotificationStatus.failed,
                        error=alert_owner_routing.trim_error(exc),
                    )
                except Exception:  # noqa: BLE001 — the row stays pending; its lease expires
                    logger.exception(
                        "Could not record the failed owner email %s for delivery %s",
                        row_id,
                        delivery_id,
                    )
                    session.rollback()

    return {"status": "done", "delivery_id": str(delivery_id), **counts}


def enqueue_owner_notifications(delivery_id: str) -> None:
    """Queue the owner follow-up for a sent delivery; never raises.

    Called after the delivery's ``sent`` status is committed, inside the send
    task's own ``try``: an exception escaping here would land in that task's
    failure handler and mark an already-sent delivery failed.
    """
    try:
        notify_owners.delay(delivery_id)
    except Exception:  # noqa: BLE001 — a broker hiccup must not fail the delivery
        logger.exception("Could not enqueue owner notifications for delivery %s", delivery_id)
