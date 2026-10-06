"""Databricks data sources

* ``data_source_db_type`` gains ``'databricks'`` (autocommit block — Postgres
  cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on every supported
  version; mirrors the PagerDuty/Teams revision). A Databricks source stores its
  HTTP path, auth type and schema scope in the existing ``extra_params`` JSON
  column and its token or OAuth secret in ``password_encrypted``, so no column
  is added.

Downgrade is a no-op: Postgres cannot drop an enum value, and a row still typed
``databricks`` after a downgrade simply has no adapter to build, which fails its
connection test and scans with "Unsupported db_type" — the honest outcome.

Revision ID: b3d5f7a9c1e2
Revises: ea2c46997ed9
Create Date: 2026-10-06 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b3d5f7a9c1e2"
down_revision: str | None = "ea2c46997ed9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE data_source_db_type ADD VALUE IF NOT EXISTS 'databricks'")


def downgrade() -> None:
    pass
