from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin

PLANNED_EVENT_SOURCE_MANUAL = "manual"
PLANNED_EVENT_SOURCE_HOLIDAY = "holiday"


class PlannedEvent(UUIDMixin, TimestampMixin, Base):
    """A window in which the project expects its numbers to move (F18, #271).

    A campaign, a sale, a holiday. An anomaly whose bucket falls inside
    ``[starts_at, ends_at)`` and whose direction matches is still detected,
    stored and drawn, but carries this event in ``MetricAnomaly.planned_event_id``
    and raises no alert, notification or open signal. ``direction`` NULL expects
    either way; ``spike`` or ``drop`` expects only that one, so a sale that
    brings a drop is still news.

    The scope works like a chart annotation's: both NULL covers every series in
    the project, a (scope_type, scope_ref) pair only the series it names.
    """

    __tablename__ = "planned_events"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_planned_event_window"),
        CheckConstraint(
            "(scope_type IS NULL) = (scope_ref IS NULL)", name="ck_planned_event_scope_pair"
        ),
        # Plain strings checked here rather than the native enums: the baseline
        # schema is frozen, and a native enum column is pinned to it.
        CheckConstraint(
            "direction IS NULL OR direction IN ('spike', 'drop')",
            name="ck_planned_event_direction",
        ),
        CheckConstraint(
            "scope_type IS NULL OR scope_type IN "
            "('project_total', 'event_type', 'event', 'metric')",
            name="ck_planned_event_scope_type",
        ),
        CheckConstraint("source IN ('manual', 'holiday')", name="ck_planned_event_source"),
        Index("ix_planned_event_project_window", "project_id", "starts_at", "ends_at"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(UtcDateTime())
    ends_at: Mapped[datetime] = mapped_column(UtcDateTime())
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    scope_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scope_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # ``holiday`` rows are written by the project's holiday calendar
    # (``holiday_calendar``) and follow it: they cannot be edited or deleted by
    # hand, only by changing the calendar.
    source: Mapped[str] = mapped_column(
        String(8), default=PLANNED_EVENT_SOURCE_MANUAL, server_default=PLANNED_EVENT_SOURCE_MANUAL
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
