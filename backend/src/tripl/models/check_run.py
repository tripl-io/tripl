from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UUIDMixin

CHECK_CALL_STATUSES = ("ok", "warning", "error", "dynamic")


class CheckRun(UUIDMixin, TimestampMixin, Base):
    """One ``tripl check`` run a CI job uploaded (``tripl check --upload``).

    A run is a summary: the repository it checked, the commit and ref, the
    pipeline, and how many calls ended in each status. Only the latest run on
    a repository's default branch keeps its calls too (``is_current``, rows in
    :class:`CheckRunCall`): that run is what "used in code" on an event shows.
    Recording a newer default-branch run of the same repository deletes the
    calls of the one before and clears its ``is_current``, so a call removed
    from the code disappears from the event. A run on any other ref (a merge
    request, a feature branch, a tag) is a summary from the start, and goes to
    the history only. Every run but the current ones is deleted after
    ``CHECK_RUN_RETENTION_DAYS`` (``maintenance.cleanup_check_runs``).
    """

    __tablename__ = "check_runs"
    __table_args__ = (
        Index("ix_check_runs_project_created", "project_id", "created_at"),
        Index("ix_check_runs_project_repository_current", "project_id", "repository", "is_current"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    # What the CI system calls the repository (GitLab's CI_PROJECT_PATH,
    # GitHub's GITHUB_REPOSITORY): one project can be checked from several.
    repository: Mapped[str] = mapped_column(String(255))
    # The repository's web address, for links to a file and line.
    repository_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    commit_sha: Mapped[str] = mapped_column(String(64))
    ref: Mapped[str] = mapped_column(String(255))
    default_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # ``ref`` is the repository's default branch.
    is_default_branch: Mapped[bool] = mapped_column(Boolean)
    # The latest default-branch run of its repository, which keeps its calls.
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa.false())
    pipeline_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    strict: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa.false())
    calls_total: Mapped[int] = mapped_column(Integer)
    calls_ok: Mapped[int] = mapped_column(Integer)
    calls_warning: Mapped[int] = mapped_column(Integer)
    calls_error: Mapped[int] = mapped_column(Integer)
    calls_dynamic: Mapped[int] = mapped_column(Integer)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class CheckRunCall(UUIDMixin, Base):
    """One tracking call of a current default-branch :class:`CheckRun`: where, and its verdict.

    ``event_id`` is the plan event the call resolved to, NULL when it resolved
    to none or the event has been deleted since; ``identity`` keeps the name
    the check read either way. ``findings`` is the list of findings the check
    reported for the call, each ``{code, severity, message, field, suppressed}``.
    """

    __tablename__ = "check_run_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'warning', 'error', 'dynamic')", name="ck_check_run_call_status"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("check_runs.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    identity: Mapped[str | None] = mapped_column(String(500), nullable=True)
    path: Mapped[str] = mapped_column(String(1000))
    line: Mapped[int] = mapped_column(Integer)
    column: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    findings: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, default=list)
