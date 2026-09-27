from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin


class DuplicateDismissal(UUIDMixin, Base):
    """A pair of events a user marked "not a duplicate" (GH #265, F12).

    One row per project and UNORDERED pair, stored ordered
    (``event_a_id < event_b_id``) so a pair has exactly one spelling and the
    unique constraint can hold it. The duplicates view never links a dismissed
    pair again; deleting either event deletes the dismissal with it.
    """

    __tablename__ = "duplicate_dismissals"
    __table_args__ = (
        UniqueConstraint("project_id", "event_a_id", "event_b_id", name="uq_duplicate_dismissal"),
        CheckConstraint("event_a_id < event_b_id", name="ck_duplicate_dismissal_ordered"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    event_a_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    event_b_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
