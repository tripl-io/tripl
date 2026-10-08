"""Greenplum and Redshift data sources

* ``data_source_db_type`` gains ``'greenplum'`` and ``'redshift'`` (autocommit
  block — Postgres cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on every
  supported version; mirrors the Snowflake revision). Both connect over libpq with
  the PostgreSQL connection settings, so no column is added.

Downgrade is a no-op: Postgres cannot drop an enum value, and a row still typed
``greenplum`` or ``redshift`` after a downgrade simply has no adapter to build,
which fails its connection test and scans with "Unsupported db_type".

Revision ID: d5f7b9c1e3a5
Revises: c4e6a8b0d2f4
Create Date: 2026-10-08 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d5f7b9c1e3a5"
down_revision: str | None = "c4e6a8b0d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE data_source_db_type ADD VALUE IF NOT EXISTS 'greenplum'")
            op.execute("ALTER TYPE data_source_db_type ADD VALUE IF NOT EXISTS 'redshift'")


def downgrade() -> None:
    pass
