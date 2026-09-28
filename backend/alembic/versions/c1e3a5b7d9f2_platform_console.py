"""Platform console: organization suspension and read-only step-ins (F20 PR14)

* ``organization_status`` gains ``suspended``; ``organizations`` gains
  ``suspended_at`` and ``suspended_reason`` (NULL unless suspended). A platform
  admin suspends an organization from the console: its members get 403 on every
  org-scoped request and the scheduled jobs skip its projects.
* ``platform_step_ins``: a platform admin's time-limited, read-only step-in to
  one organization, with the reason they gave. Kept after it ends as the record
  of who looked and why; it goes with its user or its organization.

Downgrade maps every ``suspended`` organization back to ``active`` (the release
before this one has no such state), then rebuilds the enum type without the
value: PostgreSQL cannot drop an enum label in place.

Revision ID: c1e3a5b7d9f2
Revises: b8d0f2a4c6e9
Create Date: 2026-09-29 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1e3a5b7d9f2"
down_revision: str | None = "b8d0f2a4c6e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_ENUM = "organization_status"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
        # every supported PostgreSQL version; nothing below uses the new label.
        with op.get_context().autocommit_block():
            op.execute(f"ALTER TYPE {_STATUS_ENUM} ADD VALUE IF NOT EXISTS 'suspended'")

    op.add_column(
        "organizations",
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("organizations", sa.Column("suspended_reason", sa.Text(), nullable=True))

    op.create_table(
        "platform_step_ins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_step_ins_user_org", "platform_step_ins", ["user_id", "organization_id"]
    )
    op.create_index(
        "ix_platform_step_ins_organization_id", "platform_step_ins", ["organization_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_platform_step_ins_organization_id", table_name="platform_step_ins")
    op.drop_index("ix_platform_step_ins_user_org", table_name="platform_step_ins")
    op.drop_table("platform_step_ins")

    op.execute("UPDATE organizations SET status = 'active' WHERE status = 'suspended'")
    op.drop_column("organizations", "suspended_reason")
    op.drop_column("organizations", "suspended_at")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE organizations ALTER COLUMN status DROP DEFAULT")
        op.execute(f"ALTER TYPE {_STATUS_ENUM} RENAME TO {_STATUS_ENUM}_old")
        op.execute(f"CREATE TYPE {_STATUS_ENUM} AS ENUM ('active', 'deleting')")
        op.execute(
            f"ALTER TABLE organizations ALTER COLUMN status TYPE {_STATUS_ENUM} "
            f"USING status::text::{_STATUS_ENUM}"
        )
        op.execute("ALTER TABLE organizations ALTER COLUMN status SET DEFAULT 'active'")
        op.execute(f"DROP TYPE {_STATUS_ENUM}_old")
