"""The enterprise feature still in this repository, wired through the extension hooks.

The audit webhook (F20) is registered as a bundled
:class:`~tripl.extensions.Extension`, so the core reaches it only through
:mod:`tripl.extensions`. Moving it to the ``tripl-enterprise`` package then
moves this module with it and changes nothing in the core.

The API router is imported when first asked for, so a Celery worker loading
the extension for its tasks does not import the HTTP layer.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from tripl.extensions import Extension, ExtensionRouter
from tripl.services import audit_webhook_outbox, audit_webhook_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tripl.models.audit_log import AuditLog


class _Bundled(Extension):
    name = "bundled-enterprise"

    def api_routers(self) -> Sequence[ExtensionRouter]:
        from tripl.api.v1.audit_webhook import router as audit_webhook_router

        return (ExtensionRouter(audit_webhook_router, outbound="send audit events to a webhook"),)

    async def on_org_deleting(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        await audit_webhook_service.delete_org_webhook(session, org_id)

    # -- audit -----------------------------------------------------------
    async def on_audit_recorded(
        self, session: AsyncSession, entry: AuditLog, org_id: uuid.UUID
    ) -> None:
        # The organization's audit webhook, in this very transaction: the row is
        # delivered if and only if it commits (audit_webhook_outbox).
        await audit_webhook_outbox.enqueue(session, entry, org_id)

    # -- worker ----------------------------------------------------------
    def celery_task_modules(self) -> Sequence[str]:
        return ("tripl.worker.tasks.audit_webhook",)

    def beat_schedule(self) -> Mapping[str, Mapping[str, Any]]:
        return {
            "deliver-audit-webhooks": {
                "task": "tripl.worker.tasks.audit_webhook.deliver_audit_webhooks",
                # Every 30 seconds: the one entry off the crontab grid. An audit
                # webhook feeds a SIEM, where a minute of lag is visible, and the
                # tick is one indexed read of due outbox rows when nothing is
                # queued. A tick not started within its interval is dropped; the
                # next one covers it.
                "schedule": timedelta(seconds=30),
                "options": {"expires": 30},
            },
        }


extension = _Bundled()
