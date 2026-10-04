from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    insert,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tripl.models.base import Base, TimestampMixin, UUIDMixin
from tripl.models.enum_types import db_enum

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import ExecutionContext

    from tripl.models.project import Project


class BranchKind(enum.StrEnum):
    main = "main"
    working = "working"


class BranchStatus(enum.StrEnum):
    """Review workflow states for a working branch.

    The ``main`` branch is stored with ``merged`` here: the migration that added
    branches, ``default_branch_id`` and
    ``plan_branch_service.ensure_main_branch_id`` all write it so. That status
    says nothing about main, so code must decide main-ness by ``kind == main``
    BEFORE it reads the status. The read-only refusal of writes to a merged or
    closed branch (``api.deps.get_branch_id_override``,
    ``event_photo_service._get_plan_writable_event``) splits main off first for
    exactly this reason: a write naming main by its id lands on main, and a
    photo write on a main event is accepted, because both split main off before
    they look at the status. Were the status read first, main's stored
    ``merged`` would refuse them both with a 409 — which is what this field
    means for a WORKING branch and what it must never be allowed to mean here.
    """

    draft = "draft"
    ready_for_review = "ready_for_review"
    changes_requested = "changes_requested"
    approved = "approved"
    merged = "merged"
    closed = "closed"


class PlanBranch(UUIDMixin, TimestampMixin, Base):
    """A branch of a project's tracking plan.

    Every project has exactly one ``kind="main"`` row (the live plan); working
    branches hold isolated copies of the plan entities (``branch_id`` on the
    design-time tables) and carry the Draft→…→Merged review workflow.
    """

    __tablename__ = "plan_branches"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_plan_branch_project_name"),
        Index("ix_plan_branch_project", "project_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(
        db_enum(BranchKind, "plan_branch_kind"), default=BranchKind.working.value
    )
    status: Mapped[str] = mapped_column(
        db_enum(BranchStatus, "plan_branch_status"),
        default=BranchStatus.draft.value,
        server_default=BranchStatus.draft.value,
    )
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Snapshot of main captured when the branch was opened — the merge base.
    base_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Whether every branch row that stands for a main row records which one
    # (``Event.origin_id``, ``EventTypeRelation.origin_id``). True for every
    # branch ``deep_copy_plan_to_branch`` fills; a branch opened before origin
    # ids is True only when the migration could link every copy. Where it is
    # True, a base row no copy names was deleted on the branch and a row with no
    # origin was authored there, so several of them under one name are still
    # told apart; where it is False, such a name keeps the one-row-per-name
    # handling it always had.
    origin_ids_complete: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="false", nullable=False
    )
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    project: Mapped[Project] = relationship()


def default_branch_id(context: ExecutionContext) -> uuid.UUID | None:
    """Column default for ``branch_id`` on the top-level plan tables.

    Fires only when an INSERT omits ``branch_id`` — i.e. the worker's scan
    auto-discovery and raw-ORM test seeds; the API services set it explicitly,
    so async request paths never hit this. Resolves the project's main branch
    (creating it on first use) via the active connection, so it works for both
    sync and async sessions and keeps NOT NULL ``branch_id`` satisfiable from
    every insert path.
    """
    params = context.get_current_parameters()  # type: ignore[attr-defined]
    project_id = params.get("project_id")
    if project_id is None:
        return None
    conn = context.connection
    table = PlanBranch.__table__
    existing = conn.execute(
        select(table.c.id).where(
            table.c.project_id == project_id,
            table.c.kind == BranchKind.main.value,
        )
    ).first()
    if existing is not None:
        return cast(uuid.UUID, existing[0])
    new_id = uuid.uuid4()
    conn.execute(
        insert(PlanBranch).values(
            id=new_id,
            project_id=project_id,
            name="main",
            kind=BranchKind.main.value,
            status=BranchStatus.merged.value,
            description="",
        )
    )
    return new_id
