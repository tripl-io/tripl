"""A platform admin's read-only step-in to one organization (F20 PR14, GH #273).

A support tool with guard rails (owner decision): read-only, a mandatory
reason, a time limit, and an audit trail in the TARGET organization. While a
row is live — ``ended_at`` is NULL and ``expires_at`` is in the future — the
admin acts in that organization as a ``member`` whose project role is
``viewer`` on every project (``services.org_resolution`` binds it,
``services.project_access`` reads it), and every write of theirs there is
refused (``api.deps``). A browser session only: an API key never steps in.

Rows are kept after they end or expire, as the record of who looked and why;
they go with their user or their organization (``ON DELETE CASCADE``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UtcDateTime, UUIDMixin


class PlatformStepIn(UUIDMixin, Base):
    __tablename__ = "platform_step_ins"
    __table_args__ = (
        Index("ix_platform_step_ins_user_org", "user_id", "organization_id"),
        Index("ix_platform_step_ins_organization_id", "organization_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
