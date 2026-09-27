"""One email to one owner about an alert (F07, #260).

A rule with ``notify_owners`` on emails the owners of every event type (and
catalog metric) its delivery touched, in addition to the rule's own
destination. Each such email is a row here, so the delivery detail can say who
was told and what happened to it. The editor's manual "Notify owners" action on
an incident or on an unrouted signal writes rows too, with no delivery behind
them.

Idempotency is the ``(delivery_id, user_id)`` unique key: the follow-up task
inserts (or re-claims) the row as ``pending`` BEFORE it sends, so a second run
for the same delivery (a redelivered Celery message, a retried main delivery)
finds the row and sends nothing. Only a ``sent`` row and a FRESH ``pending`` row
(younger than :data:`OWNER_NOTIFICATION_PENDING_LEASE`) count as claimed: a
later run re-claims a ``skipped`` row (SMTP configured since), a ``failed`` row
and a stale ``pending`` row (a worker killed mid-send) in place, with a
conditional UPDATE so two concurrent runs cannot both win it.

Manual rows carry ``delivery_id`` NULL, so they never collide with each other
under that key (NULLs are distinct in both Postgres and SQLite). They carry a
``target_key`` instead — the incident or the signal they were about — which the
manual path uses for its per-(target, owner) cooldown.

``status`` is a plain string with a CHECK rather than a native enum on purpose:
``pending`` is a short-lived claim marker, and adding a value to a Postgres enum
later needs an autocommit migration, which this small bookkeeping column does
not justify.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin


class AlertOwnerNotificationStatus(enum.StrEnum):
    # Claimed by a worker, not yet handed to SMTP. A worker killed mid-send
    # leaves the row here; once older than the lease a later run re-claims it
    # (see the module docstring).
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class AlertOwnerNotificationSource(enum.StrEnum):
    # The follow-up to a rule's delivery (``AlertRule.notify_owners``).
    rule = "rule"
    # An editor's "Notify owners" on an incident card or a signal card.
    manual = "manual"


OWNER_NOTIFICATION_EMAIL_MAX_LEN = 320
OWNER_NOTIFICATION_TARGET_KEY_MAX_LEN = 512

# How long a ``pending`` row holds its claim. Past it the worker that took the
# claim is presumed dead and the next run may re-claim the row and send.
OWNER_NOTIFICATION_PENDING_LEASE = timedelta(minutes=15)


class AlertOwnerNotification(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "alert_owner_notifications"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id", "user_id", name="uq_alert_owner_notification_delivery_user"
        ),
        Index("ix_alert_owner_notification_project_created", "project_id", "created_at"),
        Index("ix_alert_owner_notification_group", "correlation_group_id"),
        Index(
            "ix_alert_owner_notification_target_user",
            "project_id",
            "target_key",
            "user_id",
        ),
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_alert_owner_notification_status",
        ),
        CheckConstraint(
            "source IN ('rule', 'manual')",
            name="ck_alert_owner_notification_source",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    # NULL on a manual notification; see the module docstring.
    delivery_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("alert_deliveries.id", ondelete="CASCADE"),
        nullable=True,
    )
    # The incident a manual notify was about, when it was about one.
    correlation_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # What a manual notify was about (``incident:<group>`` or ``signal:...``),
    # the key of its cooldown; NULL on a rule-driven row.
    target_key: Mapped[str | None] = mapped_column(
        String(OWNER_NOTIFICATION_TARGET_KEY_MAX_LEN), nullable=True
    )
    # SET NULL: the row, and the address it went to, outlive the user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    email: Mapped[str] = mapped_column(String(OWNER_NOTIFICATION_EMAIL_MAX_LEN))
    source: Mapped[str] = mapped_column(
        String(16),
        default=AlertOwnerNotificationSource.rule.value,
        server_default=AlertOwnerNotificationSource.rule.value,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=AlertOwnerNotificationStatus.pending.value,
        server_default=AlertOwnerNotificationStatus.pending.value,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who clicked "Notify owners"; NULL for the rule-driven follow-up.
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
