"""check runs: tripl check results a CI job uploads, and the key scope to upload them

* ``api_key_scope`` gains ``'check_upload'``: a project-bound key that reads
  like a ``read`` key and may also record check runs, nothing else. Autocommit
  block, as for every enum value added: PostgreSQL cannot ``ALTER TYPE ... ADD
  VALUE`` inside a transaction on every supported version.
* ``check_runs``: one uploaded ``tripl check`` run, a summary (repository,
  commit, ref, pipeline, counts per status). ``is_current`` marks the latest
  default-branch run of each repository, the only one that keeps its calls.
* ``check_run_calls``: the calls of a current run, each with its location, the
  event it resolved to and its findings.

Downgrade drops both tables. It cannot drop the enum value; a
``check_upload`` key left behind is a value the older code's SQLAlchemy enum
does not know, so revoke and delete those keys before downgrading.

Revision ID: b2d4f6a8c0e5
Revises: 0d3e4cd8a66d
Create Date: 2026-11-26 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f6a8c0e5"
down_revision: str | None = "0d3e4cd8a66d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE api_key_scope ADD VALUE IF NOT EXISTS 'check_upload'")

    op.create_table(
        "check_runs",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("repository_url", sa.String(2000), nullable=True),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=True),
        sa.Column("is_default_branch", sa.Boolean(), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("pipeline_url", sa.String(2000), nullable=True),
        sa.Column("strict", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("calls_total", sa.Integer(), nullable=False),
        sa.Column("calls_ok", sa.Integer(), nullable=False),
        sa.Column("calls_warning", sa.Integer(), nullable=False),
        sa.Column("calls_error", sa.Integer(), nullable=False),
        sa.Column("calls_dynamic", sa.Integer(), nullable=False),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_check_runs_project_created", "check_runs", ["project_id", "created_at"])
    op.create_index(
        "ix_check_runs_project_repository_current",
        "check_runs",
        ["project_id", "repository", "is_current"],
    )

    op.create_table(
        "check_run_calls",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=True),
        sa.Column("name", sa.String(500), nullable=True),
        sa.Column("identity", sa.String(500), nullable=True),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("column", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ok', 'warning', 'error', 'dynamic')", name="ck_check_run_call_status"
        ),
        sa.ForeignKeyConstraint(["run_id"], ["check_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_check_run_calls_run_id", "check_run_calls", ["run_id"])
    op.create_index("ix_check_run_calls_event_id", "check_run_calls", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_check_run_calls_event_id", table_name="check_run_calls")
    op.drop_index("ix_check_run_calls_run_id", table_name="check_run_calls")
    op.drop_table("check_run_calls")
    op.drop_index("ix_check_runs_project_repository_current", table_name="check_runs")
    op.drop_index("ix_check_runs_project_created", table_name="check_runs")
    op.drop_table("check_runs")
