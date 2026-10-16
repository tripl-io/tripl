"""demo warehouse data sources

* ``data_source_db_type`` gains ``'demo'``: a demo project's source that reads
  the demo warehouse (``core/demo_warehouse``), a PostgreSQL database on
  tripl's own server or its in-memory engine. Autocommit block — Postgres
  cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on every supported
  version; mirrors the Trino/Athena revision. The connection comes from
  settings, so no column is added.

The warehouse database and its reader role are not created here: Alembic runs
inside the main database, and ``CREATE DATABASE`` cannot run in a transaction.
``python -m tripl.migrate`` provisions them in a step after this upgrade
(``core/demo_warehouse/provision.py``).

Downgrade is a no-op: PostgreSQL cannot drop an enum value. It is not a clean
one either: a ``demo`` row left behind is a value the older code's SQLAlchemy
enum does not know, so LOADING it raises ``LookupError`` — listing the data
sources, or opening and collecting for the project that owns it, answers 500.
Delete the demo projects (their sources cascade) before downgrading.

Revision ID: a8c0e2f4b6d8
Revises: f7b9d1e3a5c7
Create Date: 2026-10-15 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a8c0e2f4b6d8"
down_revision: str | None = "f7b9d1e3a5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE data_source_db_type ADD VALUE IF NOT EXISTS 'demo'")


def downgrade() -> None:
    pass
