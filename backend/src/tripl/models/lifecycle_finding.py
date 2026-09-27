from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UtcDateTime, UUIDMixin


class LifecycleFindingKind(enum.StrEnum):
    #: A deprecated event past its ``sunset_at`` that still received volume in
    #: the last 24 hours.
    sunset_overdue = "sunset_overdue"
    #: A deprecated event whose successor (``superseded_by_event_id``) received
    #: no volume in the last 7 days.
    successor_silent = "successor_silent"


class LifecycleFinding(UUIDMixin, Base):
    """One lifecycle problem the daily sunset watch found on a main-branch event (#258).

    Written only by ``worker.tasks.lifecycle.check_lifecycle_findings``. One row
    per (event, kind): each run upserts the open ones (``last_seen_at`` and the
    volume move, ``first_seen_at`` stays), sets ``resolved_at`` on the ones whose
    condition cleared, and clears it again — with a fresh ``first_seen_at`` — when
    a resolved condition comes back.

    Both kinds hang on the DEPRECATED event, because that is the page and the
    catalog row where the migration is being run. ``related_event_id`` is the
    successor for ``successor_silent`` (NULL for ``sunset_overdue``), so the
    successor's own page can list the finding too.

    ``kind`` is a checked string rather than a native enum: the set is closed but
    a new Postgres enum type would buy nothing a CHECK does not, and costs an
    autocommit block on every future value.
    """

    __tablename__ = "lifecycle_findings"
    __table_args__ = (
        UniqueConstraint("event_id", "kind", name="uq_lifecycle_finding_event_kind"),
        CheckConstraint(
            "kind IN ('sunset_overdue', 'successor_silent')",
            name="ck_lifecycle_finding_kind",
        ),
        Index("ix_lifecycle_finding_project_open", "project_id", "resolved_at"),
        Index("ix_lifecycle_finding_related_event", "related_event_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    related_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime())
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime())
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    #: ``sunset_overdue``: the deprecated event's volume over the last 24 hours.
    volume_24h: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: ``successor_silent``: the successor's volume over the last 7 days (0 while
    #: open; the last observed value once resolved).
    successor_volume_7d: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
