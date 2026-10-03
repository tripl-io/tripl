"""bring the pgvector extension up to the version the server ships

A newer ``pgvector/pgvector`` image updates the shared library, but a database
created on the older one keeps its extension objects at the old version until
``ALTER EXTENSION vector UPDATE`` runs. This runs it, so an image upgrade needs
no manual step. Without a target version it moves to the server's default and
does nothing when the extension is already there.

Downgrade does nothing: an extension is not moved back to an older version.

Revision ID: d3f5a7c9e1b2
Revises: c2e4a6b8d0f1
Create Date: 2026-10-03 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d3f5a7c9e1b2"
down_revision: str | None = "c2e4a6b8d0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER EXTENSION vector UPDATE")


def downgrade() -> None:
    pass
