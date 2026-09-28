"""Deliver queued audit rows to organizations' audit webhooks (F20, GH #273).

``deliver_audit_webhooks`` runs on the beat every 30 seconds:

1. **Retention.** ``sent`` rows older than 7 days and ``dead`` rows older than
   30 days are deleted. A finished row's ``next_attempt_at`` is when it
   finished, so both deletes ride the ``(status, next_attempt_at)`` index.
2. **Claim.** Up to :data:`BATCH_SIZE` due rows (``pending`` / ``failed``,
   ``next_attempt_at`` passed) of ACTIVE organizations
   (``active_org_scope``) with an ENABLED webhook, ``FOR UPDATE SKIP LOCKED``,
   leased by pushing ``next_attempt_at`` :data:`CLAIM_LEASE` ahead, committed.
   The lease outlives the task's hard time limit, so no run can still be
   working on a row when another may claim it again; a worker that dies
   mid-batch leaves its rows to be retried when the lease runs out. The lease
   end is also the run's token: before each send and in the outcome's own
   ``UPDATE`` the row must still carry it (and a due status), so a row whose
   lease was ever taken over is neither sent again nor overwritten by this
   run. A suspended organization's rows wait, untouched, until it is
   unsuspended.
3. **Send.** Each row, oldest audit row first, through
   ``audit_webhook_delivery.post_event`` (signed; https; the hosted
   private-host rule re-checked per send; redirects refused; a 10 s timeout
   per socket operation and a 15 s deadline per request),
   committed one by one. A 2xx is ``sent``. Anything else counts an attempt:
   retried after 1m, 5m, 30m, 2h, then every 6h, and ``dead`` after 8. After
   one organization's first failure in a run, its remaining rows are put back
   a minute without counting an attempt, so a down receiver costs one timeout
   per run, not a hundred.

Delivery is at least once and not strictly ordered: a receiver deduplicates
on ``X-Tripl-Event-Id`` and orders by ``created_at``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from tripl.crypto import InvalidToken, decrypt_value
from tripl.models.audit_log import AuditLog
from tripl.models.audit_webhook import AuditWebhookOutbox, AuditWebhookStatus, OrgAuditWebhook
from tripl.models.organization import Organization
from tripl.services import audit_webhook_delivery
from tripl.services.active_org_scope import active_org_ids
from tripl.services.audit_rows import row_record
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session

logger = logging.getLogger(__name__)

BATCH_SIZE: Final = 100
#: Longer than the task's hard time limit (``task_time_limit``, 60 min): a run
#: is killed before its lease can run out, so the next tick never claims a row
#: a live run still holds. A batch needs far less (100 x ~25 s at worst).
CLAIM_LEASE: Final = timedelta(minutes=65)
SENT_RETENTION: Final = timedelta(days=7)
DEAD_RETENTION: Final = timedelta(days=30)
#: How far an organization's other rows are put back after its first failure.
SIBLING_DEFER: Final = timedelta(minutes=1)

_DUE_STATUSES: Final = (AuditWebhookStatus.pending.value, AuditWebhookStatus.failed.value)


def purge_finished(session: Session, now: datetime) -> int:
    """Delete ``sent`` rows past 7 days and ``dead`` rows past 30. Commits."""
    result = session.execute(
        delete(AuditWebhookOutbox)
        .where(
            or_(
                and_(
                    AuditWebhookOutbox.status == AuditWebhookStatus.sent.value,
                    AuditWebhookOutbox.next_attempt_at < now - SENT_RETENTION,
                ),
                and_(
                    AuditWebhookOutbox.status == AuditWebhookStatus.dead.value,
                    AuditWebhookOutbox.next_attempt_at < now - DEAD_RETENTION,
                ),
            )
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


def claim_due(session: Session, now: datetime, *, batch_size: int = BATCH_SIZE) -> list[uuid.UUID]:
    """Lease up to ``batch_size`` due rows of active organizations. Commits."""
    enabled_orgs = select(OrgAuditWebhook.organization_id).where(OrgAuditWebhook.enabled.is_(True))
    ids = list(
        session.scalars(
            select(AuditWebhookOutbox.id)
            .where(
                AuditWebhookOutbox.status.in_(_DUE_STATUSES),
                AuditWebhookOutbox.next_attempt_at <= now,
                AuditWebhookOutbox.organization_id.in_(active_org_ids()),
                AuditWebhookOutbox.organization_id.in_(enabled_orgs),
            )
            .order_by(AuditWebhookOutbox.next_attempt_at, AuditWebhookOutbox.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
    )
    if ids:
        session.execute(
            update(AuditWebhookOutbox)
            .where(AuditWebhookOutbox.id.in_(ids))
            .values(next_attempt_at=now + CLAIM_LEASE)
            .execution_options(synchronize_session=False)
        )
    session.commit()
    return ids


def _outcome_values(
    attempts: int, result: audit_webhook_delivery.DeliveryResult, now: datetime
) -> dict[str, object]:
    """The outbox columns an attempt's result sets (``attempts`` counts it)."""
    attempts += 1
    if result.ok:
        return {
            "attempts": attempts,
            "status": AuditWebhookStatus.sent.value,
            "sent_at": now,
            "next_attempt_at": now,
            "last_error": None,
        }
    error = (result.error or "failed")[:255]
    if attempts >= audit_webhook_delivery.MAX_ATTEMPTS:
        return {
            "attempts": attempts,
            "status": AuditWebhookStatus.dead.value,
            "next_attempt_at": now,
            "last_error": error,
        }
    return {
        "attempts": attempts,
        "status": AuditWebhookStatus.failed.value,
        "next_attempt_at": now + audit_webhook_delivery.retry_delay(attempts),
        "last_error": error,
    }


def _still_ours(outbox_id: uuid.UUID, lease_until: datetime) -> Any:
    """The row still carries this run's lease and a due status."""
    return and_(
        AuditWebhookOutbox.id == outbox_id,
        AuditWebhookOutbox.next_attempt_at == lease_until,
        AuditWebhookOutbox.status.in_(_DUE_STATUSES),
    )


def _write_if_ours(
    session: Session, outbox_id: uuid.UUID, lease_until: datetime, values: dict[str, object]
) -> bool:
    """Apply ``values`` only while the row is still leased to this run.

    ``True``: written, left for the caller to commit. ``False``: the row is
    not ours any more; nothing changed and the transaction is closed.
    """
    result = session.execute(
        update(AuditWebhookOutbox)
        .where(_still_ours(outbox_id, lease_until))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    written = int(getattr(result, "rowcount", 0) or 0) == 1
    if not written:
        session.commit()
    return written


def _utc_now() -> datetime:
    return datetime.now(UTC)


def deliver_claimed(
    session: Session,
    ids: list[uuid.UUID],
    now: datetime,
    *,
    lease_until: datetime | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, int]:
    """Send the claimed rows, committing each outcome. Counts by outcome.

    ``lease_until`` is the ``next_attempt_at`` :func:`claim_due` stamped
    (``now + CLAIM_LEASE`` by default). A row that no longer carries it —
    finished or re-claimed by another run — is skipped (``lost``) and its
    outcome is never written over the other run's.

    Outcomes are stamped with the tick's ``now``; each request is signed with
    ``clock()`` at the moment it is sent, so the last row of a slow batch does
    not carry a timestamp a receiver's replay window already rejects.
    """
    stats = {"sent": 0, "failed": 0, "dead": 0, "deferred": 0, "lost": 0}
    if not ids:
        return stats
    lease = lease_until if lease_until is not None else now + CLAIM_LEASE
    rows = session.execute(
        select(
            AuditWebhookOutbox.id,
            AuditWebhookOutbox.organization_id,
            AuditWebhookOutbox.attempts,
            AuditLog,
        )
        .join(AuditLog, AuditLog.id == AuditWebhookOutbox.audit_log_id)
        .where(AuditWebhookOutbox.id.in_(ids))
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    org_ids = {row.organization_id for row in rows}
    hooks = {
        hook.organization_id: hook
        for hook in session.scalars(
            select(OrgAuditWebhook).where(OrgAuditWebhook.organization_id.in_(org_ids))
        )
    }
    slugs = dict(
        session.execute(
            select(Organization.id, Organization.slug).where(Organization.id.in_(org_ids))
        )
        .tuples()
        .all()
    )
    session.commit()
    secrets: dict[uuid.UUID, str | None] = {}
    failed_orgs: set[uuid.UUID] = set()
    for outbox_id, org_id, attempts, log in rows:
        hook = hooks.get(org_id)
        if hook is None or not hook.enabled:
            # Disabled or removed since it was queued: left for the lease to run
            # out; a removed webhook's rows are deleted with it.
            continue
        if org_id in failed_orgs:
            if _write_if_ours(session, outbox_id, lease, {"next_attempt_at": now + SIBLING_DEFER}):
                session.commit()
                stats["deferred"] += 1
            else:
                stats["lost"] += 1
            continue
        # Ownership, re-checked right before the send: a row another run has
        # taken since (its lease is a different instant) is not sent twice.
        owned = session.scalar(select(AuditWebhookOutbox.id).where(_still_ours(outbox_id, lease)))
        session.commit()
        if owned is None:
            stats["lost"] += 1
            continue
        if org_id not in secrets:
            try:
                secrets[org_id] = decrypt_value(hook.secret_encrypted)
            except InvalidToken:
                logger.warning("audit webhook: organization %s has an unreadable secret", org_id)
                secrets[org_id] = None
        secret = secrets[org_id]
        if secret is None:
            result = audit_webhook_delivery.DeliveryResult(ok=False, error="secret unreadable")
        else:
            result = audit_webhook_delivery.post_event(
                hook.url, secret, row_record(log, slugs.get(org_id, "")), now=clock()
            )
        values = _outcome_values(attempts, result, now)
        if not _write_if_ours(session, outbox_id, lease, values):
            stats["lost"] += 1
            continue
        hook_values: dict[str, object] = (
            {"last_success_at": now}
            if result.ok
            else {"last_error": values["last_error"], "last_error_at": now}
        )
        session.execute(
            update(OrgAuditWebhook)
            .where(OrgAuditWebhook.id == hook.id)
            .values(**hook_values)
            .execution_options(synchronize_session=False)
        )
        session.commit()
        if result.ok:
            stats["sent"] += 1
        else:
            failed_orgs.add(org_id)
            stats["dead" if values["status"] == AuditWebhookStatus.dead.value else "failed"] += 1
    return stats


def deliver_due(session: Session, now: datetime) -> dict[str, int]:
    """One tick: retention, claim, send."""
    purged = purge_finished(session, now)
    ids = claim_due(session, now)
    stats = deliver_claimed(session, ids, now, lease_until=now + CLAIM_LEASE)
    stats["claimed"] = len(ids)
    stats["purged"] = purged
    return stats


@celery_app.task(  # type: ignore[untyped-decorator]
    name="tripl.worker.tasks.audit_webhook.deliver_audit_webhooks",
)
def deliver_audit_webhooks() -> dict[str, int]:
    session = _get_sync_session()
    try:
        return deliver_due(session, datetime.now(UTC))
    finally:
        session.close()
