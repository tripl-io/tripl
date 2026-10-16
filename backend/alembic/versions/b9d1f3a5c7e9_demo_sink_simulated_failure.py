"""demo sink: simulate a failed first attempt

``alert_destinations.simulate_first_attempt_failure`` (boolean, default false):
on a demo project's ``demo_sink`` the first send attempt of each delivery
fails with a labelled simulated error, so the demo can show a failed delivery
and its Retry without any network (``worker/tasks/alerts.py``). Only the demo
seeder sets it.

Revision ID: b9d1f3a5c7e9
Revises: a8c0e2f4b6d8
Create Date: 2026-10-16 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9d1f3a5c7e9"
down_revision: str | None = "a8c0e2f4b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alert_destinations",
        sa.Column(
            "simulate_first_attempt_failure",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("alert_destinations", "simulate_first_attempt_failure")
