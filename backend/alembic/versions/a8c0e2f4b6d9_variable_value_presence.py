"""presence rate on observed variable contexts (F23, #306)

* ``variable_values.presence_rate``: the share of an event's breakdown rows,
  weighted by their counts, that carried the JSON path. NULL until a scan
  measures it.

Downgrade drops the column.

Revision ID: a8c0e2f4b6d9
Revises: f7b9d1e3a5c8
Create Date: 2026-10-15 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c0e2f4b6d9"
down_revision: str | None = "f7b9d1e3a5c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("variable_values", sa.Column("presence_rate", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("variable_values", "presence_rate")
