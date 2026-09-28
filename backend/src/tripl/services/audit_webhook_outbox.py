"""Queue an audit row for the organization's audit webhook (F20, GH #273).

:func:`enqueue` is called by ``audit_service.record`` for every audit row that
belongs to an organization: when that organization has an ENABLED webhook, an
``audit_webhook_outbox`` row is added to the same session, so it commits or
rolls back with the audit row itself. The delivery is the beat task's
(``worker.tasks.audit_webhook``).

The check is one indexed lookup (``org_audit_webhooks.organization_id`` is
unique), cached on the session per organization, so a batch of rows (the inbox
bulk route) looks once. Saving or deleting a webhook drops the cache
(:func:`forget`), so the save's own audit row sees the new state.

Models only: importable from ``audit_service`` without a cycle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.audit_log import AuditLog
from tripl.models.audit_webhook import AuditWebhookOutbox, AuditWebhookStatus, OrgAuditWebhook

_CACHE_KEY = "audit_webhook_enabled_orgs"


def _cache(session: AsyncSession) -> dict[uuid.UUID, bool]:
    cache = session.info.get(_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
        session.info[_CACHE_KEY] = cache
    return cache


def forget(session: AsyncSession) -> None:
    """Drop the per-session answer; call after a webhook is saved or deleted."""
    session.info.pop(_CACHE_KEY, None)


async def webhook_enabled(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Whether ``org_id`` has an enabled webhook, without flushing the session.

    No autoflush: ``record`` calls this with its audit row already added, and
    a ``commit=False`` batch must not start writing before its caller commits.
    A webhook saved in this transaction is flushed by its service first.
    """
    cache = _cache(session)
    if org_id not in cache:
        with session.no_autoflush:
            found = await session.scalar(
                select(OrgAuditWebhook.id).where(
                    OrgAuditWebhook.organization_id == org_id,
                    OrgAuditWebhook.enabled.is_(True),
                )
            )
        cache[org_id] = found is not None
    return cache[org_id]


async def enqueue(session: AsyncSession, entry: AuditLog, org_id: uuid.UUID) -> bool:
    """Add ``entry``'s outbox row when ``org_id`` has an enabled webhook. No flush."""
    if not await webhook_enabled(session, org_id):
        return False
    now = datetime.now(UTC)
    session.add(
        AuditWebhookOutbox(
            organization_id=org_id,
            audit_log=entry,
            status=AuditWebhookStatus.pending.value,
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
    )
    return True
