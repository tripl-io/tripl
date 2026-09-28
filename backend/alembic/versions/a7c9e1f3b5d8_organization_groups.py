"""Organization groups (F20, GH #273).

``organization_groups`` names a set of an organization's members, unique by
name within the organization regardless of case (a unique index on
``(organization_id, lower(name))``); ``organization_group_members`` holds who is in
each. Both cascade away with their organization (and a member row with its
user). That a group member is a member of the group's organization is enforced
by ``org_group_service``, not by a constraint.

Revision ID: a7c9e1f3b5d8
Revises: d4e8f1a2b3c5
Create Date: 2026-09-28 22:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1f3b5d8"
down_revision: str | None = "d4e8f1a2b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), server_default="", nullable=False),
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
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_organization_group_name_ci",
        "organization_groups",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_index(
        "ix_organization_groups_organization_id",
        "organization_groups",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "organization_group_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["organization_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_organization_group_member"),
    )
    op.create_index(
        "ix_organization_group_members_user_id",
        "organization_group_members",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_organization_group_members_user_id", table_name="organization_group_members")
    op.drop_table("organization_group_members")
    op.drop_index("ix_organization_groups_organization_id", table_name="organization_groups")
    op.drop_index("uq_organization_group_name_ci", table_name="organization_groups")
    op.drop_table("organization_groups")
