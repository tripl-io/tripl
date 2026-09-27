"""owner alert routing: alert_rules.notify_owners + alert_owner_notifications (#260)

* ``alert_rules.notify_owners`` (default OFF): also email the owners of every
  event type / catalog metric a delivery of the rule touches.
* ``alert_owner_notifications`` — one row per owner email, rule-driven (tied to
  a delivery, unique per (delivery, user) so a retry never re-sends) or manual
  (an editor's "Notify owners", no delivery, keyed by ``target_key`` for its
  cooldown). ``status`` and ``source`` are
  strings with CHECKs, not native enums; see the model's docstring.

Revision ID: e7a3c5b9d1f2
Revises: d5f7b9c1e3a8
Create Date: 2026-09-27 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7a3c5b9d1f2"
down_revision: str | None = "d5f7b9c1e3a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alert_rules",
        sa.Column(
            "notify_owners",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )

    op.create_table(
        "alert_owner_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_group_id", sa.Uuid(), nullable=True),
        sa.Column("target_key", sa.String(length=512), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="rule", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.Uuid(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["delivery_id"], ["alert_deliveries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["triggered_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_id", "user_id", name="uq_alert_owner_notification_delivery_user"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_alert_owner_notification_status",
        ),
        sa.CheckConstraint(
            "source IN ('rule', 'manual')",
            name="ck_alert_owner_notification_source",
        ),
    )
    op.create_index(
        "ix_alert_owner_notification_project_created",
        "alert_owner_notifications",
        ["project_id", "created_at"],
    )
    op.create_index(
        "ix_alert_owner_notification_group",
        "alert_owner_notifications",
        ["correlation_group_id"],
    )
    op.create_index(
        "ix_alert_owner_notification_target_user",
        "alert_owner_notifications",
        ["project_id", "target_key", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_alert_owner_notification_target_user", table_name="alert_owner_notifications")
    op.drop_index("ix_alert_owner_notification_group", table_name="alert_owner_notifications")
    op.drop_index(
        "ix_alert_owner_notification_project_created", table_name="alert_owner_notifications"
    )
    op.drop_table("alert_owner_notifications")
    op.drop_column("alert_rules", "notify_owners")
