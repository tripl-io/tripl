"""An organization's audit webhook: settings, test send, recent deliveries (F20, GH #273).

The routes are ``api/v1/audit_webhook.py`` (organization owners, browser
session). Every function here flushes and leaves the commit to the caller,
which audits first. Saving or deleting drops the enqueue cache of the session
(``audit_webhook_outbox.forget``), so the route's own audit row is queued (or
not) by the state it just wrote.

The URL rules at save: https with a host, no credentials, no fragment; on a
hosted instance the host must be public (``reject_private_host``). The same
rule is applied again at every send (``audit_webhook_delivery``).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import HTTPException, status
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.alerting_validation import reject_private_host
from tripl.config import DEPLOYMENT_HOSTED, settings
from tripl.crypto import InvalidToken, decrypt_value, encrypt_value
from tripl.models.audit_log import AuditLog
from tripl.models.audit_webhook import AuditWebhookOutbox, OrgAuditWebhook
from tripl.schemas.audit_webhook import (
    AuditWebhookDeliveryResponse,
    AuditWebhookDeliveryStatus,
    AuditWebhookResponse,
    AuditWebhookSaved,
    AuditWebhookTestResult,
    AuditWebhookUpdate,
)
from tripl.services import audit_webhook_delivery, audit_webhook_outbox
from tripl.services.audit_rows import iso_utc

WEBHOOK_NOT_FOUND = "Audit webhook not configured"
SECRET_UNREADABLE = "secret unreadable"


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


async def get_webhook(session: AsyncSession, org_id: uuid.UUID) -> OrgAuditWebhook | None:
    webhook: OrgAuditWebhook | None = await session.scalar(
        select(OrgAuditWebhook).where(OrgAuditWebhook.organization_id == org_id)
    )
    return webhook


async def require_webhook(session: AsyncSession, org_id: uuid.UUID) -> OrgAuditWebhook:
    hook = await get_webhook(session, org_id)
    if hook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WEBHOOK_NOT_FOUND)
    return hook


def webhook_response(hook: OrgAuditWebhook | None) -> AuditWebhookResponse:
    if hook is None:
        return AuditWebhookResponse(configured=False)
    return AuditWebhookResponse(
        configured=True,
        url=hook.url,
        enabled=hook.enabled,
        secret_configured=bool(hook.secret_encrypted),
        created_at=hook.created_at,
        updated_at=hook.updated_at,
        last_success_at=hook.last_success_at,
        last_error=hook.last_error,
        last_error_at=hook.last_error_at,
    )


def saved_response(hook: OrgAuditWebhook, secret: str | None) -> AuditWebhookSaved:
    return AuditWebhookSaved(**webhook_response(hook).model_dump(), secret=secret)


async def check_url(url: str) -> None:
    """422 unless ``url`` is https with a host (a public one, hosted)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise _unprocessable("The webhook URL must be an https URL")
    if parsed.username is not None or parsed.password is not None:
        raise _unprocessable("The webhook URL must not carry credentials")
    if parsed.fragment:
        raise _unprocessable("The webhook URL must not carry a fragment")
    if settings.deployment_mode == DEPLOYMENT_HOSTED:
        try:
            await asyncio.to_thread(
                reject_private_host, parsed.hostname, field=audit_webhook_delivery.FIELD
            )
        except ValueError as exc:
            raise _unprocessable(str(exc)) from None


@dataclass(frozen=True)
class SavedWebhook:
    hook: OrgAuditWebhook
    created: bool
    #: The new secret on create, else ``None``.
    secret: str | None
    changed: list[str]


async def save_webhook(
    session: AsyncSession, org_id: uuid.UUID, data: AuditWebhookUpdate
) -> SavedWebhook:
    """Create (with a fresh secret) or update URL and switch. Flushes."""
    await check_url(data.url)
    hook = await get_webhook(session, org_id)
    secret: str | None = None
    if hook is None:
        secret = audit_webhook_delivery.new_secret()
        hook = OrgAuditWebhook(
            organization_id=org_id,
            url=data.url,
            enabled=data.enabled,
            secret_encrypted=encrypt_value(secret),
        )
        session.add(hook)
        changed = ["url", "enabled", "secret"]
        created = True
    else:
        changed = [
            name for name in ("url", "enabled") if getattr(hook, name) != getattr(data, name)
        ]
        hook.url = data.url
        hook.enabled = data.enabled
        created = False
    await session.flush()
    # Server-side timestamps: read them now, not lazily from a response.
    await session.refresh(hook)
    audit_webhook_outbox.forget(session)
    return SavedWebhook(hook=hook, created=created, secret=secret, changed=changed)


async def rotate_secret(session: AsyncSession, hook: OrgAuditWebhook) -> str:
    """A new secret, effective for the next send (queued rows included). Flushes."""
    secret = audit_webhook_delivery.new_secret()
    hook.secret_encrypted = encrypt_value(secret)
    await session.flush()
    await session.refresh(hook)
    return secret


async def delete_webhook(session: AsyncSession, hook: OrgAuditWebhook) -> int:
    """Remove the webhook and its queue; how many undelivered rows were dropped."""
    org_id = hook.organization_id
    dropped = await session.execute(
        delete(AuditWebhookOutbox)
        .where(
            AuditWebhookOutbox.organization_id == org_id,
            AuditWebhookOutbox.status.in_(("pending", "failed")),
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        delete(AuditWebhookOutbox)
        .where(AuditWebhookOutbox.organization_id == org_id)
        .execution_options(synchronize_session=False)
    )
    await session.delete(hook)
    await session.flush()
    audit_webhook_outbox.forget(session)
    return int(getattr(dropped, "rowcount", 0) or 0)


async def delete_org_webhook(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Every webhook row of a purged organization (they would cascade; spelled out)."""
    for model in (AuditWebhookOutbox, OrgAuditWebhook):
        await session.execute(
            delete(model)
            .where(model.organization_id == org_id)
            .execution_options(synchronize_session=False)
        )


async def send_test(
    hook: OrgAuditWebhook, *, org_slug: str, user_email: str
) -> AuditWebhookTestResult:
    """POST a synthetic ``audit.webhook_test`` event now, signed like a real one."""
    now = datetime.now(UTC)
    event_id = uuid.uuid4()
    record = {
        "id": str(event_id),
        "created_at": iso_utc(now),
        "org_slug": org_slug,
        "project_slug": "",
        "branch_name": "",
        "user_email": user_email,
        "action": audit_webhook_delivery.TEST_ACTION,
        "target_type": "organization",
        "target_id": None,
        "target_name": org_slug,
        "payload": {"test": True},
    }
    try:
        secret = decrypt_value(hook.secret_encrypted)
    except InvalidToken:
        return AuditWebhookTestResult(ok=False, error=SECRET_UNREADABLE, event_id=event_id)
    result = await asyncio.to_thread(
        audit_webhook_delivery.post_event, hook.url, secret, record, now=now
    )
    return AuditWebhookTestResult(
        ok=result.ok, status_code=result.status_code, error=result.error, event_id=event_id
    )


async def list_deliveries(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    status_filter: AuditWebhookDeliveryStatus | None,
    limit: int,
) -> list[AuditWebhookDeliveryResponse]:
    """The organization's most recent outbox rows, newest first."""
    statement = (
        select(AuditWebhookOutbox, AuditLog.action)
        .join(AuditLog, AuditLog.id == AuditWebhookOutbox.audit_log_id)
        .where(AuditWebhookOutbox.organization_id == org_id)
    )
    if status_filter is not None:
        statement = statement.where(AuditWebhookOutbox.status == status_filter)
    rows = (
        await session.execute(
            statement.order_by(
                desc(AuditWebhookOutbox.created_at), desc(AuditWebhookOutbox.id)
            ).limit(limit)
        )
    ).all()
    return [
        AuditWebhookDeliveryResponse(
            id=outbox.id,
            audit_log_id=outbox.audit_log_id,
            action=action,
            status=outbox.status,
            attempts=outbox.attempts,
            next_attempt_at=outbox.next_attempt_at,
            last_error=outbox.last_error,
            created_at=outbox.created_at,
            sent_at=outbox.sent_at,
        )
        for outbox, action in rows
    ]
