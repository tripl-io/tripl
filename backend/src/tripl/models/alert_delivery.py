from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.alert_destination import AlertDestinationType
from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from tripl.models.alert_delivery_item import AlertDeliveryItem
    from tripl.models.alert_destination import AlertDestination


class AlertDeliveryStatus(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"


class AlertDelivery(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "alert_deliveries"
    __table_args__ = (
        Index("ix_alert_delivery_project_created", "project_id", "created_at"),
        Index("ix_alert_delivery_destination_created", "destination_id", "created_at"),
        Index("ix_alert_delivery_rule_created", "rule_id", "created_at"),
        Index("ix_alert_delivery_scan_created", "scan_config_id", "created_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    scan_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
    )
    scan_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scan_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    destination_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_destinations.id", ondelete="CASCADE"),
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"),
    )
    status: Mapped[str] = mapped_column(
        db_enum(AlertDeliveryStatus, "alert_delivery_status"),
        default=AlertDeliveryStatus.pending.value,
        server_default=AlertDeliveryStatus.pending.value,
    )
    channel: Mapped[str] = mapped_column(db_enum(AlertDestinationType, "alert_destination_type"))
    matched_count: Mapped[int] = mapped_column(default=0, server_default="0")
    # Number of times the reaper has re-enqueued this delivery after it was
    # left stranded in `pending`. Bounds retries so a permanently broken
    # delivery is eventually marked failed instead of re-enqueued forever.
    dispatch_attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    # The single-flight lease held by the worker currently sending this
    # delivery. Non-NULL means "an attempt is in flight", so a
    # second worker handed the same id — the reaper re-enqueues a `pending` row
    # on age alone, and prefork runs several tasks per host — loses the
    # compare-and-set in ``alerts._claim_delivery`` and returns without
    # sending. Every send path clears it when its attempt ends, sent or failed.
    #
    # Deliberately a timestamp and not a fourth ``AlertDeliveryStatus``: status
    # is a native PG enum read by the Inbox, both of the reaper's WHERE
    # clauses, the Retry 409 and the frontend badges, and `pending -> sent |
    # failed` is the lifecycle the docs promise. A stale value is survivable
    # because this is a LEASE: a worker killed mid-send leaves it set with
    # nothing to roll it back, and the row becomes claimable again after
    # ``maintenance.STRANDED_DELIVERY_MINUTES`` — the same horizon that decides
    # the row was stranded in the first place.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_snapshot: Mapped[dict[str, object] | None] = mapped_column(sa.JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    destination: Mapped[AlertDestination] = relationship(back_populates="deliveries")
    items: Mapped[list[AlertDeliveryItem]] = relationship(
        back_populates="delivery",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
