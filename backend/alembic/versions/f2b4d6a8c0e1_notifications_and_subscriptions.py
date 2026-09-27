"""notifications: subscriptions, notifications, user_notification_prefs (#259)

* ``subscriptions`` — who watches which event / event type / metric / branch,
  why (``reasons``), and whether the thread is muted.
* ``notifications`` — the in-app notification center, one row per recipient;
  ``emailed_at`` makes the email paths idempotent.
* ``user_notification_prefs`` — email frequency and mention emails; no row
  means the defaults (daily digest, mention emails on).

Existing event type owners are backfilled as ``owner`` subscribers of their
type, so ownership granted before this migration notifies the same way as
ownership granted after it.

Revision ID: f2b4d6a8c0e1
Revises: e7a3c5b9d1f2
Create Date: 2026-09-27 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b4d6a8c0e1"
down_revision: str | None = "e7a3c5b9d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENTITY_TYPES = "'event', 'event_type', 'metric', 'branch'"
_KINDS = (
    "'comment', 'reply', 'mention', 'open_question', 'signal', "
    "'branch_review_requested', 'branch_approved', 'branch_merged', 'lifecycle'"
)


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("reasons", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("muted", sa.Boolean(), server_default="false", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "entity_type", "entity_id", name="uq_subscription_user_entity"
        ),
        sa.CheckConstraint(f"entity_type IN ({_ENTITY_TYPES})", name="ck_subscription_entity_type"),
    )
    op.create_index("ix_subscription_entity", "subscriptions", ["entity_type", "entity_id"])
    op.create_index("ix_subscriptions_project_id", "subscriptions", ["project_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("emailed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(f"kind IN ({_KINDS})", name="ck_notification_kind"),
        sa.CheckConstraint(f"entity_type IN ({_ENTITY_TYPES})", name="ck_notification_entity_type"),
    )
    op.create_index(
        "ix_notification_user_read_created",
        "notifications",
        ["user_id", "read_at", "created_at"],
    )
    op.create_index(
        "ix_notification_throttle",
        "notifications",
        ["user_id", "entity_type", "entity_id", "kind", "created_at"],
    )
    op.create_index(
        "ix_notification_entity_kind",
        "notifications",
        ["entity_type", "entity_id", "kind"],
    )
    op.create_index("ix_notifications_project_id", "notifications", ["project_id"])

    op.create_table(
        "user_notification_prefs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email_mode", sa.String(length=16), server_default="daily", nullable=False),
        sa.Column("mentions_email", sa.Boolean(), server_default="true", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint(
            "email_mode IN ('off', 'instant', 'daily', 'weekly')",
            name="ck_user_notification_prefs_email_mode",
        ),
    )

    # Owners granted before this migration: subscribe them to their (main) type.
    op.execute(
        """
        INSERT INTO subscriptions
            (id, user_id, project_id, entity_type, entity_id, reasons, muted,
             created_at, updated_at)
        SELECT gen_random_uuid(), o.user_id, et.project_id, 'event_type',
               o.event_type_id, '["owner"]'::json, false, now(), now()
        FROM event_type_owners o
        JOIN event_types et ON et.id = o.event_type_id
        ON CONFLICT ON CONSTRAINT uq_subscription_user_entity DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("user_notification_prefs")
    op.drop_index("ix_notifications_project_id", table_name="notifications")
    op.drop_index("ix_notification_entity_kind", table_name="notifications")
    op.drop_index("ix_notification_throttle", table_name="notifications")
    op.drop_index("ix_notification_user_read_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_subscriptions_project_id", table_name="subscriptions")
    op.drop_index("ix_subscription_entity", table_name="subscriptions")
    op.drop_table("subscriptions")
