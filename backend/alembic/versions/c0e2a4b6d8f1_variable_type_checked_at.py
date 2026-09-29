"""the scan's one-off type check of already-observed properties (F23, #306)

* ``variables.type_checked_at``: when a scan last sampled a scan-minted JSON-path
  variable that was observed before scans inferred types, to type it. NULL: not
  checked yet. Set once, whatever the samples showed, so a property whose values
  stay text or mixed is not sampled again; editing its bindings clears it.

Downgrade drops the column.

Revision ID: c0e2a4b6d8f1
Revises: b9d1f3a5c7e0
Create Date: 2026-10-17 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0e2a4b6d8f1"
down_revision: str | None = "b9d1f3a5c7e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "variables", sa.Column("type_checked_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("variables", "type_checked_at")
