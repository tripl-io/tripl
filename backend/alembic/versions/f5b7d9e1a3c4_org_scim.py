"""Organization SCIM 2.0 provisioning (F20, GH #273)

* ``org_scim_tokens``: an organization's SCIM bearer tokens — the keyed HMAC of
  the raw token (never the token), a display prefix, who created it, when it
  was last used and when it was revoked.
* ``org_scim_configs``: the group whose members hold organization role
  ``admin`` (ON DELETE SET NULL). One per organization.
* ``scim_user_links``: users an organization's identity provider provisioned,
  with its ``externalId``, the name parts it sent and whether it wants them
  active. Kept after a deprovisioning (``active`` false); ``removed_outside_scim``
  marks a removal by an owner or admin, which the provider cannot undo.
* ``scim_group_links``: the provider's ``externalId`` of a group.
* ``organization_groups`` gains ``managed_by_scim`` (false for every existing
  group): such a group is edited by the provider only.

Downgrade drops all of it.

Revision ID: f5b7d9e1a3c4
Revises: e3a5c7d9f1b2
Create Date: 2026-10-05 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7d9e1a3c4"
down_revision: str | None = "e3a5c7d9f1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[object]]:
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
        "org_scim_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_org_scim_tokens_token_hash", "org_scim_tokens", ["token_hash"], unique=True)
    op.create_index("ix_org_scim_tokens_organization_id", "org_scim_tokens", ["organization_id"])

    op.create_table(
        "org_scim_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("admin_group_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["admin_group_id"], ["organization_groups.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )

    op.create_table(
        "scim_user_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("given_name", sa.String(length=255), nullable=True),
        sa.Column("family_name", sa.String(length=255), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("removed_outside_scim", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_scim_user_links_org_user"),
    )
    op.create_index("ix_scim_user_links_organization_id", "scim_user_links", ["organization_id"])
    op.create_index("ix_scim_user_links_user_id", "scim_user_links", ["user_id"])

    op.create_table(
        "scim_group_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["organization_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", name="uq_scim_group_links_group"),
    )
    op.create_index("ix_scim_group_links_organization_id", "scim_group_links", ["organization_id"])

    with op.batch_alter_table("organization_groups") as batch:
        batch.add_column(
            sa.Column("managed_by_scim", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("organization_groups") as batch:
        batch.drop_column("managed_by_scim")

    op.drop_index("ix_scim_group_links_organization_id", table_name="scim_group_links")
    op.drop_table("scim_group_links")

    op.drop_index("ix_scim_user_links_user_id", table_name="scim_user_links")
    op.drop_index("ix_scim_user_links_organization_id", table_name="scim_user_links")
    op.drop_table("scim_user_links")

    op.drop_table("org_scim_configs")

    op.drop_index("ix_org_scim_tokens_organization_id", table_name="org_scim_tokens")
    op.drop_index("ix_org_scim_tokens_token_hash", table_name="org_scim_tokens")
    op.drop_table("org_scim_tokens")
