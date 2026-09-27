"""lifecycle enforcement: first_seen_at, lifecycle findings, the Lifecycle alert family (#258)

* ``events.first_seen_at`` — the earliest metric bucket that counted the event,
  stamped by the metrics worker (``metrics.collect._bump_event_last_seen``) and
  only ever moved EARLIER, so a replay of an older window can correct it and a
  fresh collection never rewrites it. Backfilled below from ``event_metrics``
  in ONE grouped statement (``min(bucket)`` per event with ``count > 0``), which
  reads the ``ix_event_metric_event_bucket`` index rather than a per-row loop.
* ``lifecycle_findings`` — what the daily sunset watch
  (``worker.tasks.lifecycle.check_lifecycle_findings``) found: a deprecated
  event past ``sunset_at`` still receiving volume (``sunset_overdue``) and a
  deprecated event whose successor receives none (``successor_silent``). One
  row per (event, kind), upserted each run, ``resolved_at`` set when the
  condition clears and cleared again if it comes back.
* ``alert_rules.include_lifecycle`` (default OFF) gates the new alert family.
* ``event_changes.source`` — WHO made a history row when it was not a person:
  ``'scan'`` for a transition the metrics worker made from data (auto-live);
  NULL for a person. ``user_id IS NULL`` cannot say this on its own, because
  ``user_id`` is ``ON DELETE SET NULL`` and a deleted user's edits end up NULL
  too. No backfill: every existing row was written by a person.
* ``metric_scope_type`` gains ``'lifecycle'`` and ``alert_drift_type`` gains
  ``'sunset_overdue'`` / ``'successor_silent'`` (the finding kind an alert item
  carries in its shared ``drift_type`` column — without them the delivery
  INSERT fails on the enum, the tripl-jfm3.97 trap). Autocommit block: Postgres
  cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on every supported
  version; mirrors c4e8a2f6b1d3.

Revision ID: d5f7b9c1e3a8
Revises: b8d4f2a6c913
Create Date: 2026-09-27 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f7b9c1e3a8"
down_revision: str | None = "b8d4f2a6c913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE metric_scope_type ADD VALUE IF NOT EXISTS 'lifecycle'")
            op.execute("ALTER TYPE alert_drift_type ADD VALUE IF NOT EXISTS 'sunset_overdue'")
            op.execute("ALTER TYPE alert_drift_type ADD VALUE IF NOT EXISTS 'successor_silent'")

    op.add_column(
        "events",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Bounded backfill: one GROUP BY over the rows that carry an event id and a
    # positive count, joined back once. Events without volume stay NULL.
    op.execute(
        """
        UPDATE events
        SET first_seen_at = seen.first_bucket
        FROM (
            SELECT event_id, MIN(bucket) AS first_bucket
            FROM event_metrics
            WHERE event_id IS NOT NULL AND count > 0
            GROUP BY event_id
        ) AS seen
        WHERE events.id = seen.event_id AND events.first_seen_at IS NULL
        """
    )

    op.create_table(
        "lifecycle_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("related_event_id", sa.Uuid(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("volume_24h", sa.BigInteger(), nullable=True),
        sa.Column("successor_volume_7d", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_event_id"], ["events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "kind", name="uq_lifecycle_finding_event_kind"),
        sa.CheckConstraint(
            "kind IN ('sunset_overdue', 'successor_silent')",
            name="ck_lifecycle_finding_kind",
        ),
    )
    op.create_index(
        "ix_lifecycle_finding_project_open",
        "lifecycle_findings",
        ["project_id", "resolved_at"],
    )
    op.create_index(
        "ix_lifecycle_finding_related_event",
        "lifecycle_findings",
        ["related_event_id"],
    )

    op.add_column(
        "event_changes",
        sa.Column("source", sa.String(length=32), nullable=True),
    )

    op.add_column(
        "alert_rules",
        sa.Column(
            "include_lifecycle",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    # The enum values (metric_scope_type 'lifecycle', alert_drift_type
    # 'sunset_overdue' / 'successor_silent') are left in place: Postgres cannot
    # drop enum values, and rows written under them may still exist.
    op.drop_column("alert_rules", "include_lifecycle")
    op.drop_column("event_changes", "source")
    op.drop_index("ix_lifecycle_finding_related_event", table_name="lifecycle_findings")
    op.drop_index("ix_lifecycle_finding_project_open", table_name="lifecycle_findings")
    op.drop_table("lifecycle_findings")
    op.drop_column("events", "first_seen_at")
