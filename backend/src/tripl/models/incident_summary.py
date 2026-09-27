from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin


class IncidentSummary(UUIDMixin, TimestampMixin, Base):
    """The cached AI summary of one alerting-inbox incident (F14, #267).

    One row per ``(project, correlation group)``. ``correlation_group_id`` is not
    a foreign key: an incident is a virtual group of delivery items, not a row.
    ``facts`` is the numbered fact list the body was generated from, so a stale
    body still resolves its own citations; ``facts_hash`` is compared with the
    current facts to tell a fresh summary from a stale one.
    """

    __tablename__ = "incident_summaries"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "correlation_group_id", name="uq_incident_summary_project_group"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    correlation_group_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    facts_hash: Mapped[str] = mapped_column(String(64))
    facts: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    sentences: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    cause_known: Mapped[bool] = mapped_column(Boolean, default=False)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
