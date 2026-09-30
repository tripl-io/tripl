"""Organization SAML 2.0 single sign-on (F20, GH #273)

* ``org_sso_configs`` gains ``protocol`` (``oidc`` | ``saml``; every existing
  row is ``oidc``) and the SAML settings: ``saml_idp_entity_id``,
  ``saml_idp_sso_url``, ``saml_idp_certs`` (one or more PEM certificates),
  ``saml_name_id_format`` and ``saml_email_attribute``. ``issuer`` and
  ``client_id`` become nullable: an organization on SAML need not have them.
* ``sso_login_states`` gains ``request_id``: the AuthnRequest ID a SAML
  response must answer.
* ``saml_assertion_ids``: assertions already accepted, per organization, until
  they expire (replay refusal).

Downgrade drops the SAML table and columns. A configuration on SAML goes back
to OIDC with whatever OIDC settings it had; one that never had them gets empty
strings and ``enabled`` false (it cannot sign anyone in without a provider).

Revision ID: b3d5f7a9c1e4
Revises: a2c4e6f8b0d3
Create Date: 2026-10-07 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d5f7a9c1e4"
down_revision: str | None = "a2c4e6f8b0d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"


def upgrade() -> None:
    with op.batch_alter_table("org_sso_configs") as batch:
        batch.add_column(
            sa.Column("protocol", sa.String(length=8), server_default="oidc", nullable=False)
        )
        batch.add_column(sa.Column("saml_idp_entity_id", sa.String(length=512), nullable=True))
        batch.add_column(sa.Column("saml_idp_sso_url", sa.String(length=2048), nullable=True))
        batch.add_column(sa.Column("saml_idp_certs", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "saml_name_id_format",
                sa.String(length=255),
                server_default=_NAMEID_EMAIL,
                nullable=False,
            )
        )
        batch.add_column(sa.Column("saml_email_attribute", sa.String(length=255), nullable=True))
        batch.alter_column("issuer", existing_type=sa.String(length=512), nullable=True)
        batch.alter_column("client_id", existing_type=sa.String(length=255), nullable=True)
        batch.create_check_constraint("ck_org_sso_configs_protocol", "protocol IN ('oidc', 'saml')")

    with op.batch_alter_table("sso_login_states") as batch:
        batch.add_column(sa.Column("request_id", sa.String(length=128), nullable=True))

    op.create_table(
        "saml_assertion_ids",
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("assertion_id", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "assertion_id", name="uq_saml_assertion_ids_org_assertion"
        ),
    )
    op.create_index(
        "ix_saml_assertion_ids_organization_id", "saml_assertion_ids", ["organization_id"]
    )
    op.create_index("ix_saml_assertion_ids_expires_at", "saml_assertion_ids", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_saml_assertion_ids_expires_at", table_name="saml_assertion_ids")
    op.drop_index("ix_saml_assertion_ids_organization_id", table_name="saml_assertion_ids")
    op.drop_table("saml_assertion_ids")

    with op.batch_alter_table("sso_login_states") as batch:
        batch.drop_column("request_id")

    op.execute(
        "UPDATE org_sso_configs SET enabled = false, sso_required = false "
        "WHERE issuer IS NULL OR client_id IS NULL"
    )
    op.execute("UPDATE org_sso_configs SET issuer = '' WHERE issuer IS NULL")
    op.execute("UPDATE org_sso_configs SET client_id = '' WHERE client_id IS NULL")
    with op.batch_alter_table("org_sso_configs") as batch:
        batch.alter_column("client_id", existing_type=sa.String(length=255), nullable=False)
        batch.alter_column("issuer", existing_type=sa.String(length=512), nullable=False)
        # Dropping the column drops its CHECK with it, whatever it is named.
        batch.drop_column("saml_email_attribute")
        batch.drop_column("saml_name_id_format")
        batch.drop_column("saml_idp_certs")
        batch.drop_column("saml_idp_sso_url")
        batch.drop_column("saml_idp_entity_id")
        batch.drop_column("protocol")
