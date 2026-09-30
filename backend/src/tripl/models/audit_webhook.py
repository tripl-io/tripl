"""An organization's audit webhook and its delivery outbox (F20, GH #273).

* :class:`OrgAuditWebhook` — one per organization: the https URL every audit
  row of the organization is POSTed to, the signing secret (Fernet under the
  operator's ``ENCRYPTION_KEY``, :mod:`tripl.crypto`; never returned after it
  is first shown), ``enabled``, and the last outcome for the settings page.
* :class:`AuditWebhookOutbox` — one row per audit row to deliver, written in
  the SAME transaction as the audit row (``audit_service.record``), so an
  audit row that commits is delivered and one that rolls back never is. The
  ``audit_webhook.deliver_audit_webhooks`` beat task claims due rows, posts
  them and retries with backoff until ``sent`` or ``dead``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.audit_log import AuditLog
from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin


def _now() -> datetime:
    return datetime.now(UTC)


class AuditWebhookStatus(enum.StrEnum):
    """Where one outbox row stands.

    ``pending`` — never attempted; ``failed`` — attempted, will be retried at
    ``next_attempt_at``; ``sent`` — a 2xx answer; ``dead`` — gave up after
    ``MAX_ATTEMPTS`` (``services.audit_webhook_delivery``).
    """

    pending = "pending"
    failed = "failed"
    sent = "sent"
    dead = "dead"


class OrgAuditWebhook(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "org_audit_webhooks"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    # Fernet ciphertext under the operator's ENCRYPTION_KEY; shown once, never returned.
    secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # A short fixed text (``HTTP 500``, ``timeout``), never the receiver's body.
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AuditWebhookOutbox(UUIDMixin, Base):
    __tablename__ = "audit_webhook_outbox"
    __table_args__ = (
        # The delivery task's claim: due rows of one status, oldest first.
        Index("ix_audit_webhook_outbox_status_next_attempt", "status", "next_attempt_at"),
        # The settings page's recent deliveries, and the purge of an organization.
        Index("ix_audit_webhook_outbox_org_created", "organization_id", "created_at"),
        # ``ON DELETE CASCADE`` from ``audit_log`` scans on this.
        Index("ix_audit_webhook_outbox_audit_log", "audit_log_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    audit_log_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("audit_log.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(
        String(16), default=AuditWebhookStatus.pending.value, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_now, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, default=_now, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # Many-to-one, so the unit of work INSERTs the audit row before this one
    # when both are added in one flush (``audit_service.record``).
    audit_log: Mapped[AuditLog] = relationship(lazy="raise_on_sql")
