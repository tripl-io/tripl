"""metric_anomaly_attributions: why a volume anomaly happened (F02, #255)

One row per ``metric_anomalies`` row, written by the metrics worker right after
it writes the anomalies: the delta (actual - expected), up to three breakdown
columns with their top values' contributions and "% of change explained", and
the release that crossed the activation gate shortly before, if any. Stored at
detection time so alerts and the UI quote the same numbers.

The row goes with its anomaly (ON DELETE CASCADE): the worker replaces anomaly
rows on every re-score and recomputes the attribution for the new row.

No backfill. Anomalies scored before this migration read as ``not_computed``
until a later scan or replay re-scores their bucket.

Revision ID: b8d4f2a6c913
Revises: a7c3e9f1b254
Create Date: 2026-09-27 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f2a6c913"
down_revision: str | None = "a7c3e9f1b254"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metric_anomaly_attributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("anomaly_id", sa.Uuid(), nullable=False),
        sa.Column("delta", sa.Float(), nullable=False),
        sa.Column("columns", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("release", sa.JSON(), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["anomaly_id"], ["metric_anomalies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("anomaly_id", name="uq_metric_anomaly_attribution_anomaly"),
    )


def downgrade() -> None:
    op.drop_table("metric_anomaly_attributions")
