"""demo projects: the history fill after a demo opens

``projects.demo_history_status`` (nullable string): a demo now opens with the
last two days of its history replayed, and a task of its own replays the rest
once it is ready — ``filling`` while that runs, then ``complete`` or
``failed``. NULL on a real project and on a demo built before the fill
existed, which the code reads as "nothing to fill".

Revision ID: e3a5c7e9b1d4
Revises: d1f3b5c7e9a2
Create Date: 2026-10-16 21:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3a5c7e9b1d4"
down_revision: str | None = "d1f3b5c7e9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("demo_history_status", sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "demo_history_status")
