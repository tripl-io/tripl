"""organizations.status: active | deleting (F20 PR6)

Deleting an organization is a background job (critique #26): the request marks
the row ``deleting`` and a Celery task purges its projects, sources, keys,
invitations, settings, memberships and finally the row itself. Every read of a
``deleting`` organization answers 404, so the column is what fences the window
between the two.

Every existing row is ``active`` (server default).

Revision ID: e5b7d9f1a3c6
Revises: d4a6c8e0f2b4
Create Date: 2026-09-28 18:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5b7d9f1a3c6"
down_revision: str | None = "d4a6c8e0f2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_ENUM = "organization_status"
_STATUS_VALUES = ("active", "deleting")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(*_STATUS_VALUES, name=_STATUS_ENUM).create(bind, checkfirst=True)
        status_type: sa.types.TypeEngine[str] = postgresql.ENUM(
            *_STATUS_VALUES, name=_STATUS_ENUM, create_type=False
        )
    else:
        status_type = sa.Enum(*_STATUS_VALUES, name=_STATUS_ENUM, create_constraint=False)
    op.add_column(
        "organizations",
        sa.Column("status", status_type, nullable=False, server_default="active"),
    )


def downgrade() -> None:
    op.drop_column("organizations", "status")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(name=_STATUS_ENUM).drop(bind, checkfirst=True)
