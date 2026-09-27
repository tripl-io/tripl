from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin


class ProjectHealthSnapshot(UUIDMixin, Base):
    """One day of a project's plan health (F15, #268).

    Written once a day by ``worker.tasks.health.snapshot_project_health`` (an
    upsert on ``(project_id, day)``, UTC date) and read by the Overview trend
    and the weekly digest. Rows older than ``SNAPSHOT_RETENTION_DAYS`` are
    deleted by the same task.

    ``component_averages`` is ``{key: {"value": float | None, "applies_count": int}}``;
    ``worst_events`` is ``[{"event_id", "name", "score", "top_issue"}]``.
    """

    __tablename__ = "project_health_snapshots"
    __table_args__ = (
        UniqueConstraint("project_id", "day", name="uq_project_health_snapshot_day"),
        Index("ix_project_health_snapshot_project_day", "project_id", "day"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scored_events: Mapped[int] = mapped_column(Integer, default=0)
    healthy_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    unhealthy_count: Mapped[int] = mapped_column(Integer, default=0)
    component_averages: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    worst_events: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
