"""OIDC single sign-on of an organization (F20, GH #273).

* :class:`OrgSsoConfig` — the organization's identity provider: issuer, client
  id, the client secret (encrypted with the operator key, :mod:`tripl.crypto`),
  scopes, and the two switches ``enabled`` and ``sso_required``.
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
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin

#: What ``scopes`` holds when the owner names none.
DEFAULT_SSO_SCOPES = "openid email profile"


class OrgSsoConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "org_sso_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    issuer: Mapped[str] = mapped_column(String(512))
    client_id: Mapped[str] = mapped_column(String(255))
    # Fernet ciphertext under the operator's ENCRYPTION_KEY; never returned.
    client_secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    scopes: Mapped[str] = mapped_column(
        String(512), default=DEFAULT_SSO_SCOPES, server_default=DEFAULT_SSO_SCOPES
    )
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
    # PKCE code_verifier, encrypted like every stored secret.
    code_verifier: Mapped[str] = mapped_column(Text)
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
