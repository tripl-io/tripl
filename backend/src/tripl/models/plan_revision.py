from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin
from tripl.models.enum_types import db_enum


class PlanRevisionKind(enum.StrEnum):
    """What produced a revision.

    ``snapshot`` is a user-taken one (``POST /plan-revisions``); ``branch_base``
    is the merge base captured when a branch opens; ``merge`` is the live plan
    right after a branch merged into it. Stored rather than parsed back out of
    ``summary``, which is free text a user can also write.
    """

    snapshot = "snapshot"
    branch_base = "branch_base"
    merge = "merge"


class PlanRevision(UUIDMixin, Base):
    """Immutable snapshot of a project's tracking plan at a point in time.

    Each row stores the full schema payload (event_types, events, variables,
    relations, meta_fields) so a diff between two revisions does not need to
    re-query — useful when entities have since been deleted.
    """

    __tablename__ = "plan_revisions"
    __table_args__ = (Index("ix_plan_revisions_project_created", "project_id", "created_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    kind: Mapped[str] = mapped_column(
        db_enum(PlanRevisionKind, "plan_revision_kind"),
        default=PlanRevisionKind.snapshot.value,
        server_default=PlanRevisionKind.snapshot.value,
    )
    # The branch a ``branch_base`` / ``merge`` revision belongs to; NULL for a
    # user snapshot and once the branch is deleted. ``use_alter`` because
    # ``plan_branches.base_revision_id`` already points the other way, and the
    # cycle would otherwise leave create_all / drop_all unable to order the two.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "plan_branches.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_plan_revisions_branch_id",
        ),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
