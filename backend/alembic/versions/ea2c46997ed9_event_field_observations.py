"""event field observations: the values a scan saw for one event field

* ``event_field_observations``: one row per (event, field) whose breakdown rows
  disagreed in the last scan that observed the event — a structured event fired
  on several screens. ``values`` holds the top 20 values with their row counts;
  ``distinct_count`` and ``total_count`` describe the full set. Main-branch
  events only: a branch copy reads its main twin's row.

No backfill: the next manual scan, or the next collection with a declared
``scan_lookback_hours``, fills it. Downgrade drops the table; the rows are
scan-observed and the next scan after a re-upgrade writes them again.

Revision ID: ea2c46997ed9
Revises: a7c9e1f3b5d2
Create Date: 2026-10-06 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea2c46997ed9"
down_revision: str | None = "a7c9e1f3b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_field_observations",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("field_definition_id", sa.Uuid(), nullable=False),
        sa.Column("scan_config_id", sa.Uuid(), nullable=True),
        sa.Column("values", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("distinct_count", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.BigInteger(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["field_definition_id"], ["field_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["scan_config_id"], ["scan_configs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "field_definition_id", name="uq_event_field_observation"),
    )
    op.create_index(
        "ix_event_field_observations_project", "event_field_observations", ["project_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_event_field_observations_project", table_name="event_field_observations")
    op.drop_table("event_field_observations")
