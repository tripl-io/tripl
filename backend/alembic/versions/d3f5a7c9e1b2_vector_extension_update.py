"""bring the pgvector extension up to the version the server ships

A newer ``pgvector/pgvector`` image updates the shared library, but a database
created on the older one keeps its extension objects at the old version until
``ALTER EXTENSION vector UPDATE`` runs. This runs it when the installed version
is behind the server's default, so an image upgrade needs no manual step.

Only the extension's owner (or a superuser) may run that statement, and
PostgreSQL checks ownership before it compares versions. Where an administrator
created the extensions, as on a managed PostgreSQL, tripl's database role does
not own ``vector``: the statement is then skipped and a warning names both
versions, for the owner to run it. Nothing happens when the versions already
match or the extension is not installed.

Downgrade does nothing: an extension is not moved back to an older version.

Revision ID: d3f5a7c9e1b2
Revises: c2e4a6b8d0f1
Create Date: 2026-10-03 10:00:00.000000

"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5a7c9e1b2"
down_revision: str | None = "c2e4a6b8d0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic's own logger, which alembic.ini sends to the console, so the warning
# reaches the migrate job's output. A RAISE WARNING in a DO block would not:
# asyncpg drops server notices when no listener is registered.
logger = logging.getLogger("alembic.runtime.migration")

# pg_has_role(..., 'USAGE') is true for members of the owning role and for
# superusers: the same test PostgreSQL's ownership check applies.
_VECTOR_STATE = sa.text(
    "SELECT e.extversion, a.default_version, pg_has_role(e.extowner, 'USAGE') "
    "FROM pg_extension e JOIN pg_available_extensions a ON a.name = e.extname "
    "WHERE e.extname = 'vector'"
)


def upgrade() -> None:
    row = op.get_bind().execute(_VECTOR_STATE).first()
    if row is None:
        return
    installed, shipped, may_alter = row
    if installed == shipped:
        return
    if not may_alter:
        logger.warning(
            "pgvector is at version %s and the server ships %s, but this database role "
            "does not own the extension. Its owner or a superuser must run "
            "ALTER EXTENSION vector UPDATE.",
            installed,
            shipped,
        )
        return
    op.execute("ALTER EXTENSION vector UPDATE")


def downgrade() -> None:
    pass
