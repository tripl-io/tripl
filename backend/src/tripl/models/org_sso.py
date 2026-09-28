"""Single sign-on of an organization, OIDC or SAML 2.0 (F20, GH #273).

* :class:`OrgSsoConfig` — the organization's identity provider and its
  ``protocol``. OIDC: issuer, client id, the client secret (encrypted with the
  operator key, :mod:`tripl.crypto`) and scopes. SAML: the IdP entity id, its
  SSO (HTTP-Redirect) URL, its signing certificate(s) in PEM (several while a
  certificate rotates; public by nature), the NameID format requested and the
  attribute carrying the email (else the NameID). The fields of the protocol
  not in use stay as they were saved, so switching back loses nothing. Plus
  the two switches ``enabled`` and ``sso_required``.
* :class:`OrgSsoDomain` — an email domain the organization claims. Only a
  domain proven through a DNS TXT record (``verified_at``) counts: SSO sign-in
  accepts only addresses in the organization's verified domains, and a domain
  verified by one organization cannot be verified by another (a partial unique
  index over the verified rows).
* :class:`UserSsoIdentity` — "subject ``sub`` at ``issuer`` is this user, in
  this organization".
* :class:`SsoLoginState` — one sign-in attempt between ``/start`` and
  ``/callback``: the ``state`` digest (keyed HMAC, like session tokens), the
  nonce, the PKCE verifier (encrypted) and where to land. Single use, 10
  minutes.
* :class:`SsoLinkTicket` — an IdP sign-in whose address belongs to an existing
  account: the account is linked only once the person confirms (critique #27).
  Single use, 10 minutes.
* :class:`SsoMembershipBlock` — "this account was removed from this
  organization": signing in through its provider again does not bring it back
  in. Lifted when the account accepts a new invitation.
* :class:`SamlAssertionId` — the ID of every SAML assertion accepted, until it
  expires: a replayed assertion is refused.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin

#: What ``scopes`` holds when the owner names none.
DEFAULT_SSO_SCOPES = "openid email profile"

PROTOCOL_OIDC = "oidc"
PROTOCOL_SAML = "saml"
SSO_PROTOCOLS = (PROTOCOL_OIDC, PROTOCOL_SAML)
#: The NameID format requested when the owner names none.
NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
#: A SAML identity's ``issuer`` (in ``user_sso_identities`` and link tickets)
#: is this prefix and the IdP entity id, so it never equals an OIDC issuer.
SAML_ISSUER_PREFIX = "saml:"
#: The longest IdP entity id: prefixed, it still fits the 512-char issuer columns.
SAML_ENTITY_ID_MAX_LENGTH = 512 - len(SAML_ISSUER_PREFIX)


class OrgSsoConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "org_sso_configs"
    __table_args__ = (
        CheckConstraint("protocol IN ('oidc', 'saml')", name="ck_org_sso_configs_protocol"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    protocol: Mapped[str] = mapped_column(
        String(8), default=PROTOCOL_OIDC, server_default=PROTOCOL_OIDC, nullable=False
    )
    # OIDC. Null only while an organization that started on SAML never set them.
    issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Fernet ciphertext under the operator's ENCRYPTION_KEY; never returned.
    client_secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    scopes: Mapped[str] = mapped_column(
        String(512), default=DEFAULT_SSO_SCOPES, server_default=DEFAULT_SSO_SCOPES
    )
    # SAML 2.0. Null only while an organization on OIDC never set them.
    saml_idp_entity_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    saml_idp_sso_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # One or more PEM certificates, any of which may sign an assertion.
    saml_idp_certs: Mapped[str | None] = mapped_column(Text, nullable=True)
    saml_name_id_format: Mapped[str] = mapped_column(
        String(255), default=NAMEID_EMAIL, server_default=NAMEID_EMAIL, nullable=False
    )
    # The attribute holding the email; null: the NameID is the email.
    saml_email_attribute: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # Members must sign in through the IdP to act in the organization. Owners'
    # browser sessions are exempt (break-glass); their API keys are not. See
    # ``api.deps``.
    sso_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )


class OrgSsoDomain(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "org_sso_domains"
    __table_args__ = (
        UniqueConstraint("organization_id", "domain", name="uq_org_sso_domains_org_domain"),
        # One organization per verified domain on the whole instance.
        Index(
            "uq_org_sso_domains_verified_domain",
            "domain",
            unique=True,
            postgresql_where=sa.text("verified_at IS NOT NULL"),
            sqlite_where=sa.text("verified_at IS NOT NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    # Lowercase, no trailing dot.
    domain: Mapped[str] = mapped_column(String(253))
    # Public: it is what the owner publishes in DNS.
    verification_token: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class UserSsoIdentity(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "user_sso_identities"
    __table_args__ = (
        UniqueConstraint(
            "issuer", "subject", "organization_id", name="uq_user_sso_identities_subject"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))


class SsoLoginState(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "sso_login_states"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nonce: Mapped[str] = mapped_column(String(128))
    # PKCE code_verifier, encrypted like every stored secret. Empty for SAML.
    code_verifier: Mapped[str] = mapped_column(Text)
    # SAML: the AuthnRequest ID the response must answer (``InResponseTo``).
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    next_path: Mapped[str] = mapped_column(String(2048), default="/")
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class SsoLinkTicket(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "sso_link_tickets"

    ticket_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    next_path: Mapped[str] = mapped_column(String(2048), default="/")
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class SsoMembershipBlock(UUIDMixin, TimestampMixin, Base):
    """A member removed from the organization; SSO sign-in does not re-add them.

    Written by ``org_service.remove_member``; deleted when the account accepts
    an invitation to the organization again (an admin's decision).
    """

    __tablename__ = "sso_membership_blocks"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_sso_membership_blocks_org_user"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )


class SamlAssertionId(UUIDMixin, TimestampMixin, Base):
    """A SAML assertion already accepted; kept until it would have expired anyway."""

    __tablename__ = "saml_assertion_ids"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "assertion_id", name="uq_saml_assertion_ids_org_assertion"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    assertion_id: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
