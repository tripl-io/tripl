from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, UUIDMixin
from tripl.models.organization import DEFAULT_ORG_ID, default_org_server_default


class AuditLog(UUIDMixin, Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        # Without this, every ``DELETE FROM plan_branches`` — and every project
        # delete, which cascades to its branches — seq-scans the whole append-only
        # audit log to apply the ``SET NULL`` below.
        Index("ix_audit_log_branch", "branch_id"),
        # THE THREE READ PATHS. Every one of them ends in the same
        # ``ORDER BY created_at DESC, id DESC`` with a LIMIT, so each needs its
        # filter column in front of those two or the page becomes a top-N sort
        # over everything that matched. All three are ASCENDING
        # though the query reads descending: a btree is scanned equally well in
        # either direction as long as EVERY sort column is reversed together,
        # which these are. And all three carry ``id``, because ``created_at``
        # alone is not a total order — ``server_default=now()`` gives every row of
        # one batch the same value, so the pager needs its tie-break inside the
        # index rather than in a sort on top of it.
        #
        # 1. One project's log, the common case. ``list_entries`` resolves the
        # slug to a project and matches the ID, so the leading
        # column is ``project_id`` and not the slug. This also covers the
        # ``ON DELETE SET NULL`` cascade as a prefix: deleting any project scans
        # this table to null the column out, which is the argument that earned
        # ``branch_id`` its own index. Deliberately NOT claimed for the demo
        # reset's purge — that predicate ORs across two columns and the second
        # half is unindexed, so a scan is the honest expectation there.
        Index("ix_audit_log_project_created", "project_id", "created_at", "id"),
        # 2. The fallback, for a slug NO live project answers to: a deleted
        # project's rows keep the label and lose the id, so this is the only way
        # back to them.
        Index("ix_audit_log_project_slug_created", "project_slug", "created_at", "id"),
        # 3. The workspace-wide feed, which filters by nothing at all.
        # Without this the instance audit page sorts the whole
        # table on every load — the same failure as the others, with no predicate
        # to narrow it first.
        Index("ix_audit_log_created", "created_at", "id"),
        # 4. One organization's log (F20). Nothing reads by it yet; it lands with
        # the column so the backfill and the index are one migration, and it is
        # also what the ``ON DELETE SET NULL`` below scans on an org delete.
        Index("ix_audit_log_organization_created", "organization_id", "created_at", "id"),
        # There is deliberately NO standalone ``ix_audit_log_project`` on
        # ``project_id`` alone. It existed for one revision, and e7a1c04b62d8
        # dropped it as superseded once the composite above claimed the same
        # leading column — a btree on ``(project_id, created_at, id)`` serves
        # every predicate a btree on ``(project_id)`` could. The declaration
        # outlived that migration here and made the ORM metadata disagree with
        # the migrated schema, which is what ``alembic check`` reports and what
        # ``create_all``-built test schemas hid. Do not restore it.
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    user_email: Mapped[str] = mapped_column(String(320), default="", server_default="")
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    project_slug: Mapped[str] = mapped_column(String(255), default="", server_default="")
    # The organization the action belongs to. NULLABLE, unlike every other
    # ``organization_id``: platform-level actions (operator settings, platform
    # admin grants) belong to no organization. SET NULL, like ``project_id``, so
    # the append-only log survives the organization. Until writers pass it
    # explicitly (F20 PR4/PR5) every row lands in the default organization.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        default=DEFAULT_ORG_ID,
        server_default=default_org_server_default(),
    )
    # The plan branch the write was scoped to. NULL/"" means the write was not
    # made through a branch-scoped request — main, or an action with no
    # plan-branch dimension at all (alerting, scans, users). It does NOT assert
    # "main".
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_branches.id", ondelete="SET NULL"), nullable=True
    )
    # Denormalized for the same reason as ``user_email`` and ``project_slug``
    # above: deleting a branch hard-deletes its row and the FK sets the id to
    # NULL, which would erase the branch context from exactly the rows that
    # recorded that branch's work. The name survives, so the trail stays
    # readable — and the audit row renders without a second request.
    branch_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    target_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
