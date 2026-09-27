"""project_health_snapshots: daily plan health for the trend and digest (#268)

Revision ID: c7e9a1b3d5f6
Revises: a3c5e7f9b1d2
Create Date: 2026-09-27 23:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e9a1b3d5f6"
down_revision: str | None = "a3c5e7f9b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_health_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("scored_events", sa.Integer(), nullable=False),
        sa.Column("healthy_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("unhealthy_count", sa.Integer(), nullable=False),
        sa.Column("component_averages", sa.JSON(), nullable=False),
        sa.Column("worst_events", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "day", name="uq_project_health_snapshot_day"),
    )
    op.create_index(
        "ix_project_health_snapshot_project_day",
        "project_health_snapshots",
        ["project_id", "day"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_health_snapshot_project_day", table_name="project_health_snapshots")
    op.drop_table("project_health_snapshots")
