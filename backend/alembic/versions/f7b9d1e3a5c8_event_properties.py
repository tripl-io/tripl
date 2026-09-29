"""per-event property list on variable_event_value_overrides (F23, #306)

A ``variable_event_value_overrides`` row becomes an event's property entry:

* ``required`` (default false) says whether the event must always carry the
  property;
* ``values`` turns nullable. NULL means "no per-event override, the variable's
  global list applies"; a list keeps meaning what it always meant. Every
  existing row holds a list, so nothing is backfilled.

Downgrade deletes the rows that are only a property entry (``values`` NULL):
the previous release reads every row as an override, and a NULL list as "no
values allowed". Then it restores the NOT NULL column.

Revision ID: f7b9d1e3a5c8
Revises: e6a8b0c2d4f7
Create Date: 2026-10-14 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7b9d1e3a5c8"
down_revision: str | None = "e6a8b0c2d4f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("variable_event_value_overrides") as batch:
        batch.add_column(
            sa.Column("required", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.alter_column("values", existing_type=sa.JSON(), nullable=True, server_default=None)


def downgrade() -> None:
    op.execute('DELETE FROM variable_event_value_overrides WHERE "values" IS NULL')
    with op.batch_alter_table("variable_event_value_overrides") as batch:
        batch.alter_column("values", existing_type=sa.JSON(), nullable=False, server_default="[]")
        batch.drop_column("required")
