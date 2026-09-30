"""property drift and the per-event required threshold (F23, #306)

* ``events.required_presence_threshold``: the presence rate at or above which a
  scanned JSON property counts as always carried by the event. NULL: the
  default.
* ``property_drifts``: a new property, a missing required property or a type
  change seen by a scan, triaged with the ``schema_drift_status`` workflow
  (the existing PostgreSQL enum type is reused, ``create_type=False``).

Downgrade drops the table and the column.

Revision ID: b9d1f3a5c7e0
Revises: a8c0e2f4b6d9
Create Date: 2026-10-16 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b9d1f3a5c7e0"
down_revision: str | None = "a8c0e2f4b6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("events", sa.Column("required_presence_threshold", sa.Float(), nullable=True))
    status_enum = postgresql.ENUM(name="schema_drift_status", create_type=False)
    op.create_table(
        "property_drifts",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("variable_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("scan_config_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("detail", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("status", status_enum, server_default="open", nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variable_id"], ["variables.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scan_config_id"], ["scan_configs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("variable_id", "event_id", "kind", name="uq_property_drift"),
    )
    op.create_index(
        "uq_property_drift_variable",
        "property_drifts",
        ["variable_id", "kind"],
        unique=True,
        postgresql_where=sa.text("event_id IS NULL"),
        sqlite_where=sa.text("event_id IS NULL"),
    )
    op.create_index("ix_property_drifts_variable_id", "property_drifts", ["variable_id"])
    op.create_index("ix_property_drifts_event_id", "property_drifts", ["event_id"])
    op.create_index(
        "ix_property_drifts_project_detected", "property_drifts", ["project_id", "detected_at"]
    )


def downgrade() -> None:
    op.drop_table("property_drifts")
    op.drop_column("events", "required_presence_threshold")
