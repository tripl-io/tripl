from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin
from tripl.models.domain_enums import ShadowEventStatus
from tripl.models.enum_types import db_enum

SHADOW_STATUS_NEW = ShadowEventStatus.new.value
SHADOW_STATUS_ACCEPTED = ShadowEventStatus.accepted.value
SHADOW_STATUS_DISMISSED = ShadowEventStatus.dismissed.value
# Sample property dicts kept per candidate.
SHADOW_SAMPLE_LIMIT = 5


class ShadowEventCandidate(UUIDMixin, Base):
    """An event identity observed in warehouse data with no matching plan event.

    Rows are upserted by the metrics collector whenever a built event name
    misses ``events_by_name``. ``event_name`` is the stable scan identity (the
    same string ``Event.source_name`` would carry), so accepting a candidate
    creates an event that matches on the next collection.
    """

    __tablename__ = "shadow_event_candidates"
    __table_args__ = (
        UniqueConstraint(
            "scan_config_id",
            "event_name",
            name="uq_shadow_candidate_config_name",
        ),
        Index("ix_shadow_candidate_project_status", "project_id", "status"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    scan_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scan_configs.id", ondelete="CASCADE"),
    )
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("event_types.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_name: Mapped[str] = mapped_column(String(500))
    # Count observed in the most recent collection window (not cumulative —
    # collection windows are re-collected, so summing would double-count).
    observed_count: Mapped[int] = mapped_column(BigInteger, default=0)
    # Up to ``SHADOW_SAMPLE_LIMIT`` property dicts (column -> value) from the rows
    # that produced this identity in the most recent collection window, so the
    # shadow inbox can show what the event looks like before it is accepted.
    # Replaced, not merged, like ``observed_count``.
    sample_properties: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, default=list, server_default="[]"
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        db_enum(ShadowEventStatus, "shadow_event_status"),
        default=SHADOW_STATUS_NEW,
    )
    accepted_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
