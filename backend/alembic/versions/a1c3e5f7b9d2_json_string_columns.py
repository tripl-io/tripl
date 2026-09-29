"""parse String columns as JSON: ``json_string_columns`` (F23.9, #306)

* ``scan_configs.json_string_columns``: the String/STRING columns every read of
  the scan's source parses as JSON. NOT NULL, server default ``[]`` (no column
  parsed, which is every existing config).
* ``scan_dry_run_jobs`` / ``scan_preview_jobs``: the same, so a draft's dry run
  and preview read the source the way the saved scan will.

Downgrade drops the columns.

Revision ID: a1c3e5f7b9d2
Revises: e4c6a8f0b2d5
Create Date: 2026-10-18 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: str | None = "e4c6a8f0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("scan_configs", "scan_dry_run_jobs", "scan_preview_jobs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("json_string_columns", sa.JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "json_string_columns")
