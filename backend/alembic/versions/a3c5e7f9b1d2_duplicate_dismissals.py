"""duplicate_dismissals: pairs a user marked "not a duplicate" (#265)

Revision ID: a3c5e7f9b1d2
Revises: f2b4d6a8c0e1
Create Date: 2026-09-27 23:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7f9b1d2"
down_revision: str | None = "f2b4d6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "duplicate_dismissals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_a_id", sa.Uuid(), nullable=False),
        sa.Column("event_b_id", sa.Uuid(), nullable=False),
        sa.Column("dismissed_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_a_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_b_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dismissed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "event_a_id", "event_b_id", name="uq_duplicate_dismissal"
        ),
        sa.CheckConstraint("event_a_id < event_b_id", name="ck_duplicate_dismissal_ordered"),
    )
    op.create_index("ix_duplicate_dismissals_project_id", "duplicate_dismissals", ["project_id"])
    op.create_index("ix_duplicate_dismissals_event_a_id", "duplicate_dismissals", ["event_a_id"])
    op.create_index("ix_duplicate_dismissals_event_b_id", "duplicate_dismissals", ["event_b_id"])


def downgrade() -> None:
    op.drop_index("ix_duplicate_dismissals_event_b_id", table_name="duplicate_dismissals")
    op.drop_index("ix_duplicate_dismissals_event_a_id", table_name="duplicate_dismissals")
    op.drop_index("ix_duplicate_dismissals_project_id", table_name="duplicate_dismissals")
    op.drop_table("duplicate_dismissals")
