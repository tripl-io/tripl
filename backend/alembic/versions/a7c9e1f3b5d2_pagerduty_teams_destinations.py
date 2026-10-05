"""PagerDuty and Microsoft Teams alert destinations

* ``alert_destination_type`` gains ``'pagerduty'`` and ``'teams'`` (autocommit
  block — Postgres cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on
  every supported version; mirrors the earlier ``demo_sink`` revision). The
  same enum types ``alert_deliveries.channel``, so deliveries to the new
  channels need nothing more.
* ``alert_destinations.pagerduty_routing_key_encrypted`` /
  ``pagerduty_severity``: the Events API v2 integration key (Fernet-encrypted)
  and the severity sent with each event (NULL reads as ``error``).
* ``alert_destinations.teams_webhook_url_encrypted``: the Teams incoming-webhook
  or Workflows URL (Fernet-encrypted; the URL is the credential).

Downgrade drops the three columns. The enum values are left in place
(Postgres cannot drop enum values); a row still typed ``pagerduty`` or
``teams`` after a downgrade has no config to send with and fails its
deliveries, which is the honest outcome.

Revision ID: a7c9e1f3b5d2
Revises: f5b7d9c1e3a4
Create Date: 2026-10-05 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1f3b5d2"
down_revision: str | None = "f5b7d9c1e3a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE alert_destination_type ADD VALUE IF NOT EXISTS 'pagerduty'")
            op.execute("ALTER TYPE alert_destination_type ADD VALUE IF NOT EXISTS 'teams'")
    op.add_column(
        "alert_destinations",
        sa.Column("pagerduty_routing_key_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "alert_destinations",
        sa.Column("pagerduty_severity", sa.String(16), nullable=True),
    )
    op.add_column(
        "alert_destinations",
        sa.Column("teams_webhook_url_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alert_destinations", "teams_webhook_url_encrypted")
    op.drop_column("alert_destinations", "pagerduty_severity")
    op.drop_column("alert_destinations", "pagerduty_routing_key_encrypted")
