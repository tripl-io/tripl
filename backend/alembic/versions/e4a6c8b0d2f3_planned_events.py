"""planned events: windows in which a project expects its numbers to move (F18)

* ``planned_events``: one campaign, sale or holiday per row, with its window
  ``[starts_at, ends_at)``, the direction it expects (NULL: either) and an
  optional chart scope.
* ``metric_anomalies.planned_event_id``: the planned event that expected this
  anomaly. A tagged row is drawn but raises no alert, notification or open
  signal.

Downgrade drops the column and the table.

Revision ID: e4a6c8b0d2f3
Revises: d3f5a7c9e1b2
Create Date: 2026-10-03 14:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a6c8b0d2f3"
down_revision: str | None = "d3f5a7c9e1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planned_events",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("direction", sa.String(8), nullable=True),
        sa.Column("scope_type", sa.String(16), nullable=True),
        sa.Column("scope_ref", sa.String(120), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_planned_event_window"),
        sa.CheckConstraint(
            "(scope_type IS NULL) = (scope_ref IS NULL)", name="ck_planned_event_scope_pair"
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('spike', 'drop')",
            name="ck_planned_event_direction",
        ),
        sa.CheckConstraint(
            "scope_type IS NULL OR scope_type IN "
            "('project_total', 'event_type', 'event', 'metric')",
            name="ck_planned_event_scope_type",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_planned_event_project_window",
        "planned_events",
        ["project_id", "starts_at", "ends_at"],
    )
    op.add_column("metric_anomalies", sa.Column("planned_event_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "metric_anomalies_planned_event_id_fkey",
        "metric_anomalies",
        "planned_events",
        ["planned_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_metric_anomalies_planned_event_id", "metric_anomalies", ["planned_event_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_metric_anomalies_planned_event_id", table_name="metric_anomalies")
    op.drop_constraint(
        "metric_anomalies_planned_event_id_fkey", "metric_anomalies", type_="foreignkey"
    )
    op.drop_column("metric_anomalies", "planned_event_id")
    op.drop_index("ix_planned_event_project_window", table_name="planned_events")
    op.drop_table("planned_events")
