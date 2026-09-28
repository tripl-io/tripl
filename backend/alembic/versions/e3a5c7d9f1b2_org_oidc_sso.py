"""Organization OIDC single sign-on (F20, GH #273)

* ``org_sso_configs``: an organization's identity provider (issuer, client id,
  the client secret encrypted with the operator key, scopes) and the switches
  ``enabled`` / ``sso_required``. One per organization.
* ``org_sso_domains``: the email domains it claims, proven by a DNS TXT record
  (``verified_at``). A verified domain belongs to one organization on the whole
  instance (a partial unique index over the verified rows).
* ``user_sso_identities``: ``(issuer, subject)`` of an organization's provider
  linked to a user.
* ``sso_login_states`` / ``sso_link_tickets``: short-lived single-use rows of a
  sign-in in flight and of an existing account waiting to confirm its link.
* ``sso_membership_blocks``: accounts removed from an organization, which an
  SSO sign-in must not add back (lifted by accepting a new invitation).
* ``user_sessions`` gains ``auth_method`` (``password`` | ``sso``, every
  existing session is ``password``) and ``sso_organization_id``; ``api_keys``
  gains ``created_with_sso_org_id``. Both reference organizations ON DELETE SET
  NULL.

Downgrade drops all of it; sessions and keys lose only the new columns.

Revision ID: e3a5c7d9f1b2
Revises: d2f4a6c8e0b1
Create Date: 2026-09-30 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3a5c7d9f1b2"
down_revision: str | None = "d2f4a6c8e0b1"
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
        "org_sso_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "scopes",
            sa.String(length=512),
            server_default="openid email profile",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("sso_required", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id"),
    )

    op.create_table(
        "org_sso_domains",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("verification_token", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "domain", name="uq_org_sso_domains_org_domain"),
    )
    op.create_index("ix_org_sso_domains_organization_id", "org_sso_domains", ["organization_id"])
    op.create_index(
        "uq_org_sso_domains_verified_domain",
        "org_sso_domains",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("verified_at IS NOT NULL"),
        sqlite_where=sa.text("verified_at IS NOT NULL"),
    )

    op.create_table(
        "user_sso_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issuer", "subject", "organization_id", name="uq_user_sso_identities_subject"
        ),
    )
    op.create_index("ix_user_sso_identities_user_id", "user_sso_identities", ["user_id"])
    op.create_index(
        "ix_user_sso_identities_organization_id", "user_sso_identities", ["organization_id"]
    )

    op.create_table(
        "sso_login_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("next_path", sa.String(length=2048), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sso_login_states_state_hash", "sso_login_states", ["state_hash"], unique=True
    )
    op.create_index("ix_sso_login_states_organization_id", "sso_login_states", ["organization_id"])
    op.create_index("ix_sso_login_states_expires_at", "sso_login_states", ["expires_at"])

    op.create_table(
        "sso_link_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("ticket_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("next_path", sa.String(length=2048), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sso_link_tickets_ticket_hash", "sso_link_tickets", ["ticket_hash"], unique=True
    )
    op.create_index("ix_sso_link_tickets_user_id", "sso_link_tickets", ["user_id"])
    op.create_index("ix_sso_link_tickets_organization_id", "sso_link_tickets", ["organization_id"])
    op.create_index("ix_sso_link_tickets_expires_at", "sso_link_tickets", ["expires_at"])

    op.create_table(
        "sso_membership_blocks",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_sso_membership_blocks_org_user"),
    )
    op.create_index(
        "ix_sso_membership_blocks_organization_id", "sso_membership_blocks", ["organization_id"]
    )
    op.create_index("ix_sso_membership_blocks_user_id", "sso_membership_blocks", ["user_id"])

    with op.batch_alter_table("user_sessions") as batch:
        batch.add_column(
            sa.Column(
                "auth_method", sa.String(length=16), server_default="password", nullable=False
            )
        )
        batch.add_column(sa.Column("sso_organization_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_user_sessions_sso_organization_id",
            "organizations",
            ["sso_organization_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_user_sessions_auth_method", "auth_method IN ('password', 'sso')"
        )
    op.create_index(
        "ix_user_sessions_sso_organization_id", "user_sessions", ["sso_organization_id"]
    )

    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(sa.Column("created_with_sso_org_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_api_keys_created_with_sso_org_id",
            "organizations",
            ["created_with_sso_org_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    # Dropping a column drops its foreign key and CHECK with it, whatever they
    # are named (a schema built from the models names them differently).
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_column("created_with_sso_org_id")

    op.drop_index("ix_user_sessions_sso_organization_id", table_name="user_sessions")
    with op.batch_alter_table("user_sessions") as batch:
        batch.drop_column("sso_organization_id")
        batch.drop_column("auth_method")

    op.drop_index("ix_sso_membership_blocks_user_id", table_name="sso_membership_blocks")
    op.drop_index("ix_sso_membership_blocks_organization_id", table_name="sso_membership_blocks")
    op.drop_table("sso_membership_blocks")

    op.drop_index("ix_sso_link_tickets_expires_at", table_name="sso_link_tickets")
    op.drop_index("ix_sso_link_tickets_organization_id", table_name="sso_link_tickets")
    op.drop_index("ix_sso_link_tickets_user_id", table_name="sso_link_tickets")
    op.drop_index("ix_sso_link_tickets_ticket_hash", table_name="sso_link_tickets")
    op.drop_table("sso_link_tickets")

    op.drop_index("ix_sso_login_states_expires_at", table_name="sso_login_states")
    op.drop_index("ix_sso_login_states_organization_id", table_name="sso_login_states")
    op.drop_index("ix_sso_login_states_state_hash", table_name="sso_login_states")
    op.drop_table("sso_login_states")

    op.drop_index("ix_user_sso_identities_organization_id", table_name="user_sso_identities")
    op.drop_index("ix_user_sso_identities_user_id", table_name="user_sso_identities")
    op.drop_table("user_sso_identities")

    op.drop_index("uq_org_sso_domains_verified_domain", table_name="org_sso_domains")
    op.drop_index("ix_org_sso_domains_organization_id", table_name="org_sso_domains")
    op.drop_table("org_sso_domains")

    op.drop_table("org_sso_configs")
