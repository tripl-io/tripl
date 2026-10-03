"""holiday calendars: a country's public holidays as planned events (F18)

* ``project_anomaly_settings.holiday_country``: the project's calendar, an
  ISO 3166-1 alpha-2 code, NULL for none.
* ``planned_events.source``: ``manual`` for the ones people add, ``holiday``
  for the rows the calendar writes. Existing rows are ``manual``.

Downgrade drops the holiday rows and both columns.

Revision ID: f5b7d9c1e3a4
Revises: e4a6c8b0d2f3
Create Date: 2026-10-03 18:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7d9c1e3a4"
down_revision: str | None = "e4a6c8b0d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_anomaly_settings", sa.Column("holiday_country", sa.String(2), nullable=True)
    )
    op.add_column(
        "planned_events",
        sa.Column("source", sa.String(8), nullable=False, server_default="manual"),
    )
    op.create_check_constraint(
        "ck_planned_event_source", "planned_events", "source IN ('manual', 'holiday')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM planned_events WHERE source = 'holiday'")
    op.drop_constraint("ck_planned_event_source", "planned_events", type_="check")
    op.drop_column("planned_events", "source")
    op.drop_column("project_anomaly_settings", "holiday_country")
