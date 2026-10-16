"""anomaly settings: platform breakdown detection switch

``project_anomaly_settings.detect_platform_breakdowns`` (boolean, default
true): whether the detector scores the scan's platform column as a breakdown
(per-platform volume anomalies and platform parity). True reproduces the
behaviour before the column existed; only the demo seeder sets it false.

Revision ID: c0e2a4b6d8f1
Revises: b9d1f3a5c7e9
Create Date: 2026-10-16 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c0e2a4b6d8f1"
down_revision: str | None = "b9d1f3a5c7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_anomaly_settings",
        sa.Column(
            "detect_platform_breakdowns",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("project_anomaly_settings", "detect_platform_breakdowns")
