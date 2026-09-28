"""Audit webhook and its delivery outbox (F20, GH #273)

* ``org_audit_webhooks``: an organization's audit webhook (one per
  organization): the https URL, the signing secret encrypted with the operator
  key, ``enabled``, and the last delivery outcome.
* ``audit_webhook_outbox``: one row per audit row to deliver, written in the
  audit row's own transaction; ``pending`` | ``failed`` | ``sent`` | ``dead``,
  the attempt count and when to try next. Both cascade with the organization;
  an outbox row also cascades with its audit row.

Downgrade drops both tables.

Revision ID: f8a0c2e4b6d9
Revises: f5b7d9e1a3c4
Create Date: 2026-10-05 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a0c2e4b6d9"
down_revision: str | None = "f5b7d9e1a3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "org_audit_webhooks",
        sa.Column("id", sa.Uuid(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )

    op.create_table(
        "audit_webhook_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("audit_log_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        _timestamp("created_at"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["audit_log_id"], ["audit_log.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_webhook_outbox_status_next_attempt",
        "audit_webhook_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_audit_webhook_outbox_org_created",
        "audit_webhook_outbox",
        ["organization_id", "created_at"],
    )
    op.create_index("ix_audit_webhook_outbox_audit_log", "audit_webhook_outbox", ["audit_log_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_webhook_outbox_audit_log", table_name="audit_webhook_outbox")
    op.drop_index("ix_audit_webhook_outbox_org_created", table_name="audit_webhook_outbox")
    op.drop_index("ix_audit_webhook_outbox_status_next_attempt", table_name="audit_webhook_outbox")
    op.drop_table("audit_webhook_outbox")
    op.drop_table("org_audit_webhooks")
