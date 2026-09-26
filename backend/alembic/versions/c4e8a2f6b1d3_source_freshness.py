"""source freshness: scan lag columns and the "data is late" alert family (#269)

* ``scan_configs.last_event_at`` — newest event time (bucket resolution) seen by
  the latest successful scheduled collection.
* ``scan_configs.last_collection_at`` — when that collection finished.
  Freshness (fresh/late/overdue/unknown) is computed from these two, not stored.
* ``alert_rules.include_source_freshness`` (default OFF) gates delivery of the
  new family.
* ``alert_drift_type`` gains ``'source_late'`` / ``'source_overdue'`` — the
  freshness status an alert item carries in its ``drift_type`` column.
* ``metric_scope_type`` gains ``'source_freshness'`` (autocommit block —
  Postgres cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on every
  supported version; mirrors d1c2b3a4f5e6).

Existing configs start with both columns NULL, which reads as ``unknown`` until
their next collection records them.

Revision ID: c4e8a2f6b1d3
Revises: a7c3e9f1b2d4
Create Date: 2026-09-26 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2f6b1d3"
down_revision: str | None = "a7c3e9f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE metric_scope_type ADD VALUE IF NOT EXISTS 'source_freshness'")
            # The freshness status rides the shared ``drift_type`` column of
            # alert_delivery_items / alert_pending_items; without these the
            # delivery INSERT fails on the enum (the tripl-jfm3.97 trap).
            op.execute("ALTER TYPE alert_drift_type ADD VALUE IF NOT EXISTS 'source_late'")
            op.execute("ALTER TYPE alert_drift_type ADD VALUE IF NOT EXISTS 'source_overdue'")

    op.add_column(
        "scan_configs",
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scan_configs",
        sa.Column("last_collection_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "alert_rules",
        sa.Column(
            "include_source_freshness",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    # The enum values (metric_scope_type 'source_freshness', alert_drift_type
    # 'source_late' / 'source_overdue') are left in place: Postgres cannot drop
    # enum values, and rows written under them may still exist.
    op.drop_column("alert_rules", "include_source_freshness")
    op.drop_column("scan_configs", "last_collection_at")
    op.drop_column("scan_configs", "last_event_at")
