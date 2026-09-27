"""incident_summaries: cached AI summaries of alerting-inbox incidents (#267)

Revision ID: d7e9f1a3b5c7
Revises: a3c5e7f9b1d2
Create Date: 2026-09-27 23:55:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e9f1a3b5c7"
down_revision: str | None = "a3c5e7f9b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_summaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_group_id", sa.Uuid(), nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("sentences", sa.JSON(), nullable=False),
        sa.Column("cause_known", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("generated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "correlation_group_id", name="uq_incident_summary_project_group"
        ),
    )


def downgrade() -> None:
    op.drop_table("incident_summaries")
