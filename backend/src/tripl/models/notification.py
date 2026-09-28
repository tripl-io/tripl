"""One in-app notification for one user (#259).

Written by ``services.notification_service.notify`` only. ``read_at`` is the
bell's unread state; ``emailed_at`` is stamped by whichever email path sent it
(instant or a daily/weekly digest), so each row is emailed at most once.

``kind`` and ``entity_type`` are checked strings rather than native enums, the
same call ``lifecycle_findings.kind`` made: a new kind is a CHECK swap, not an
enum-type migration in an autocommit block.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UtcDateTime, UUIDMixin


class NotificationKind(enum.StrEnum):
    comment = "comment"
    reply = "reply"
    mention = "mention"
    open_question = "open_question"
    signal = "signal"
    branch_review_requested = "branch_review_requested"
    branch_approved = "branch_approved"
    branch_merged = "branch_merged"
    lifecycle = "lifecycle"


NOTIFICATION_KIND_CHECK = "kind IN ({})".format(
    ", ".join(f"'{kind.value}'" for kind in NotificationKind)
)


class Notification(UUIDMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(NOTIFICATION_KIND_CHECK, name="ck_notification_kind"),
        CheckConstraint(
            "entity_type IN ('event', 'event_type', 'metric', 'branch', 'doc')",
            name="ck_notification_entity_type",
        ),
        Index("ix_notification_user_read_created", "user_id", "read_at", "created_at"),
        # The signal throttle asks "did this user hear about this entity lately".
        Index(
            "ix_notification_throttle",
            "user_id",
            "entity_type",
            "entity_id",
            "kind",
            "created_at",
        ),
        # "Was this exact signal already announced" asks across users.
        Index("ix_notification_entity_kind", "entity_type", "entity_id", "kind"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[uuid.UUID] = mapped_column()
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    url: Mapped[str] = mapped_column(String(1000))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    read_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    # Stamped in Python as well: SQLite's CURRENT_TIMESTAMP text would not sort
    # against Python-bound values, and the bell pages by (created_at, id).
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), server_default=func.now()
    )
