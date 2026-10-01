"""Baseline schema at the production head.

The PostgreSQL DDL was captured from a fresh database after applying the
complete pre-squash migration chain at commit 19eea9a9. The revision ID stays
the same so a production database already at this head executes no DDL.
Existing databases on an older revision must reach this head with the old
chain before this baseline is deployed.

Revision ID: a1c3e5f7b9d2
Revises:
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_SQL_DIR = Path(__file__).resolve().parent.parent
_SEPARATOR = "\n-- tripl:statement\n"


def _statements(filename: str) -> Iterator[str]:
    sql = (_SQL_DIR / filename).read_text(encoding="utf-8")
    for statement in sql.split(_SEPARATOR):
        if statement.strip():
            yield statement.strip()


def upgrade() -> None:
    connection = op.get_bind()
    for statement in _statements("baseline_schema.sql"):
        connection.exec_driver_sql(statement)
    # The only data seeded by a fresh run of the old chain is the default org.
    connection.exec_driver_sql(
        "INSERT INTO public.organizations (id, slug, name) VALUES "
        "('00000000-0000-0000-0000-00000000d0f1', 'default', 'Default organization')"
    )


def downgrade() -> None:
    connection = op.get_bind()
    for statement in _statements("baseline_drop.sql"):
        connection.exec_driver_sql(statement)
