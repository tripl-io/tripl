"""variables carry a JSON Schema fragment (F23, #306)

* ``variables.json_schema`` holds the property's type the way JSON Schema
  spells it: ``type``, ``format``, ``items``, ``properties``, ``required`` and
  the numeric, string and array constraints. NULL means "only what
  ``variable_type`` says", which is every existing row, so nothing is
  backfilled.

Downgrade drops the column; the previous release has no reader for it.

Revision ID: e6a8b0c2d4f7
Revises: d5f7a9b1c3e6
Create Date: 2026-10-13 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6a8b0c2d4f7"
down_revision: str | None = "d5f7a9b1c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("variables", sa.Column("json_schema", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("variables", "json_schema")
