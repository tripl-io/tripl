"""scan configs: unplanned events go to the catalog or the reconciliation inbox

``scan_configs.auto_create_events`` (boolean, default true): whether a scan adds
an identity no catalog event answers to as a new ``in_review`` event (true, the
behaviour before the column existed) or adds no event for it, so the metrics
collection files it in the reconciliation inbox as a shadow candidate (false).
Event types and fields are still added either way: they are what the
collection counts the unplanned volume under. ``scan_dry_run_jobs`` carries
the same column for a draft's dry run, which has no saved config to read it
from.

Revision ID: d1f3b5c7e9a2
Revises: c0e2a4b6d8f1
Create Date: 2026-10-16 18:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3b5c7e9a2"
down_revision: str | None = "c0e2a4b6d8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ("scan_configs", "scan_dry_run_jobs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "auto_create_events",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "auto_create_events")
