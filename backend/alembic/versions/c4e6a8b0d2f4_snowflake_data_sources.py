"""Snowflake data sources

* ``data_source_db_type`` gains ``'snowflake'`` (autocommit block — Postgres
  cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on every supported
  version; mirrors the Databricks revision). A Snowflake source stores its
  warehouse, role, auth type and schema scope in the existing ``extra_params``
  JSON column and its password or private key in ``password_encrypted``, so no
  column is added.

Downgrade is a no-op: Postgres cannot drop an enum value, and a row still typed
``snowflake`` after a downgrade simply has no adapter to build, which fails its
connection test and scans with "Unsupported db_type" — the honest outcome.

Revision ID: c4e6a8b0d2f4
Revises: b3d5f7a9c1e2
Create Date: 2026-10-07 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c4e6a8b0d2f4"
down_revision: str | None = "b3d5f7a9c1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE data_source_db_type ADD VALUE IF NOT EXISTS 'snowflake'")


def downgrade() -> None:
    pass
