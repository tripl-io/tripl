from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.domain_enums import AlertInboxStatus
from tripl.models.enum_types import db_enum

ALERT_GROUP_STATUS_OPEN = AlertInboxStatus.open.value


class AlertCorrelationState(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "alert_correlation_states"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "correlation_group_id",
            name="uq_alert_correlation_state_project_group",
        ),
        Index("ix_alert_correlation_state_project_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    correlation_group_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(
        db_enum(AlertInboxStatus, "alert_inbox_status"),
        default=ALERT_GROUP_STATUS_OPEN,
        server_default=ALERT_GROUP_STATUS_OPEN,
    )
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    false_positive_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
