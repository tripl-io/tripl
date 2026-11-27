"""projects.code_url_template: links from "used in code" to the repository

A project may set one URL template (``https://github.com/org/app/blob/{commit}/{path}#L{line}``)
that turns each place a check run found into a link to that file and line.
NULL, the default, shows the places as plain text.

Downgrade drops the column.

Revision ID: c4e6a8b0d2f7
Revises: b2d4f6a8c0e5
Create Date: 2026-11-27 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e6a8b0d2f7"
down_revision: str | None = "b2d4f6a8c0e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("code_url_template", sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "code_url_template")
