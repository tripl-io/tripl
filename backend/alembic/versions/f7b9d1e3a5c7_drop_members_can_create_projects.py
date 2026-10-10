"""Drop organizations.members_can_create_projects

The column arrived with organizations (F20 PR1) and nothing ever read it: no
service, route or screen gated project creation on it, so it suggested a
setting that did not exist. Every organization member may create projects of
their own (``api.deps.get_editor_user`` on ``POST /projects``).

Downgrade re-adds the column with its old default, ``true``. That is the only
value a row could ever have held, so nothing is lost either way.

Revision ID: f7b9d1e3a5c7
Revises: e6a8c0d2f4b6
Create Date: 2026-10-10 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7b9d1e3a5c7"
down_revision: str | None = "e6a8c0d2f4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("organizations", "members_can_create_projects")


def downgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "members_can_create_projects",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
