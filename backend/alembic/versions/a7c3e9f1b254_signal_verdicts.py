"""signal verdicts: tracking_bug / false_positive / real_issue + expected reason (F01, #254)

Extends the signal triage table instead of adding a parallel one:

* ``signal_triage_action`` gains ``tracking_bug``, ``false_positive`` and
  ``real_issue``. Like ``acknowledged`` / ``expected`` they pin ONE bucket, so
  the existing ``(action = 'muted') = (bucket IS NULL)`` check already covers
  them. Postgres cannot ``ALTER TYPE ... ADD VALUE`` inside a transaction on
  every supported version, hence the autocommit block (mirrors c4e8a2f6b1d3).
* ``signal_expected_reason`` — new native enum (campaign | release |
  seasonality | other) and the nullable ``signal_triage.expected_reason``
  column it types. Only ``expected`` rows set it; it documents, it does not
  suppress later buckets.

Nothing is back-filled: existing ``expected`` rows read as "expected, no reason".

Revision ID: a7c3e9f1b254
Revises: e3b9d5a1c7f4
Create Date: 2026-09-27 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9f1b254"
down_revision: str | None = "e3b9d5a1c7f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REASON_ENUM = "signal_expected_reason"
_REASON_VALUES = ("campaign", "release", "seasonality", "other")
_NEW_ACTIONS = ("tracking_bug", "false_positive", "real_issue")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            for value in _NEW_ACTIONS:
                op.execute(f"ALTER TYPE signal_triage_action ADD VALUE IF NOT EXISTS '{value}'")
    postgresql.ENUM(*_REASON_VALUES, name=_REASON_ENUM).create(bind, checkfirst=True)
    op.add_column(
        "signal_triage",
        sa.Column(
            "expected_reason",
            postgresql.ENUM(*_REASON_VALUES, name=_REASON_ENUM, create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Rows carrying the new verdicts would violate the old enum's meaning, so
    # they go; the enum VALUES stay (Postgres cannot drop an enum value).
    op.execute(
        "DELETE FROM signal_triage WHERE action IN ('tracking_bug', 'false_positive', 'real_issue')"
    )
    op.drop_column("signal_triage", "expected_reason")
    postgresql.ENUM(name=_REASON_ENUM).drop(op.get_bind(), checkfirst=True)
