"""property drift alerts and notifications (F23, #306)

* ``alert_rules.include_property_drifts`` (default OFF) gates the new alert
  family: one ``property_drift`` candidate per open ``PropertyDrift`` row.
* ``metric_scope_type`` gains ``'property_drift'`` and ``alert_drift_type``
  gains ``'new_property'`` / ``'missing_required'`` / ``'type_change'`` (the
  drift kind an alert item carries in its shared ``drift_type`` column —
  without them the delivery INSERT fails on the enum, the tripl-jfm3.97 trap).
  Autocommit block: Postgres cannot ``ALTER TYPE ... ADD VALUE`` inside a
  transaction on every supported version; mirrors d5f7b9c1e3a8.
* ``notifications.kind`` gains ``'property_drift'``: watchers of an event are
  told when a scan finds a property drift on it.

Downgrade drops the column and restores the notification kind check (after
deleting the notifications of the removed kind). Enum values stay: Postgres
cannot drop one.

Revision ID: c3e5a7b9d1f2
Revises: e4c6a8f0b2d5
Create Date: 2026-10-17 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5a7b9d1f2"
down_revision: str | None = "e4c6a8f0b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_BEFORE = (
    "'comment', 'reply', 'mention', 'open_question', 'signal', "
    "'branch_review_requested', 'branch_approved', 'branch_merged', 'lifecycle'"
)
_KINDS_AFTER = f"{_KINDS_BEFORE}, 'property_drift'"


def _replace_kind_check(kinds: str) -> None:
    with op.batch_alter_table("notifications") as batch:
        batch.drop_constraint("ck_notification_kind", type_="check")
        batch.create_check_constraint("ck_notification_kind", f"kind IN ({kinds})")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE metric_scope_type ADD VALUE IF NOT EXISTS 'property_drift'")
            op.execute("ALTER TYPE alert_drift_type ADD VALUE IF NOT EXISTS 'new_property'")
            op.execute("ALTER TYPE alert_drift_type ADD VALUE IF NOT EXISTS 'missing_required'")
            op.execute("ALTER TYPE alert_drift_type ADD VALUE IF NOT EXISTS 'type_change'")

    op.add_column(
        "alert_rules",
        sa.Column(
            "include_property_drifts",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )
    _replace_kind_check(_KINDS_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE kind = 'property_drift'")
    _replace_kind_check(_KINDS_BEFORE)
    op.drop_column("alert_rules", "include_property_drifts")
