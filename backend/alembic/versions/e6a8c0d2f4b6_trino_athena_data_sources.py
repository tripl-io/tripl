"""Trino and Athena data sources

* ``data_source_db_type`` gains ``'trino'`` and ``'athena'`` (autocommit block —
  Postgres cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on every
  supported version; mirrors the Snowflake revision). Their connection settings
  ride in the existing ``extra_params`` JSON column, so no column is added.

Downgrade is a no-op: Postgres cannot drop an enum value, and a row still typed
``trino`` or ``athena`` after a downgrade simply has no adapter to build, which
fails its connection test and scans with "Unsupported db_type".

Revision ID: e6a8c0d2f4b6
Revises: d5f7b9c1e3a5
Create Date: 2026-10-08 18:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e6a8c0d2f4b6"
down_revision: str | None = "d5f7b9c1e3a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE data_source_db_type ADD VALUE IF NOT EXISTS 'trino'")
            op.execute("ALTER TYPE data_source_db_type ADD VALUE IF NOT EXISTS 'athena'")


def downgrade() -> None:
    pass
