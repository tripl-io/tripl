"""the "event + properties" scan setup preset (F23.4c, #306)

* ``scan_configs.setup_preset``: ``custom`` (every existing config) or
  ``event_properties``. NOT NULL, server default ``custom``.
* ``scan_configs.event_name_column`` / ``properties_column``: the preset's two
  columns; NULL on a custom config.
* ``scan_dry_run_jobs``: the same three, so a draft's dry run plans the preset.
* ``scan_preview_jobs.event_name_column`` / ``properties_column``: a preview
  asked with both summarises the events and properties its sample rows yield.

Downgrade drops the columns.

Revision ID: e4c6a8f0b2d5
Revises: c0e2a4b6d8f1
Create Date: 2026-10-17 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4c6a8f0b2d5"
down_revision: str | None = "c0e2a4b6d8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("scan_configs", "scan_dry_run_jobs"):
        op.add_column(
            table,
            sa.Column(
                "setup_preset",
                sa.String(length=32),
                nullable=False,
                server_default="custom",
            ),
        )
    for table in ("scan_configs", "scan_dry_run_jobs", "scan_preview_jobs"):
        op.add_column(table, sa.Column("event_name_column", sa.String(length=255), nullable=True))
        op.add_column(table, sa.Column("properties_column", sa.String(length=255), nullable=True))


def downgrade() -> None:
    for table in ("scan_preview_jobs", "scan_dry_run_jobs", "scan_configs"):
        op.drop_column(table, "properties_column")
        op.drop_column(table, "event_name_column")
    for table in ("scan_dry_run_jobs", "scan_configs"):
        op.drop_column(table, "setup_preset")
