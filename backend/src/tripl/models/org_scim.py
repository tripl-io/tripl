"""SCIM 2.0 provisioning of an organization (F20, GH #273).

* :class:`OrgScimToken` — a bearer token an identity provider provisions the
  organization with. Only the keyed HMAC of the raw token is stored (like
  session tokens, :func:`tripl.auth_utils.hash_session_token`); ``prefix`` is
  what the owner sees in the list. Revoked, never deleted, so the audit trail
  keeps naming it.
* :class:`OrgScimConfig` — the organization's SCIM settings: the group whose
  members hold organization role ``admin``. No owner mapping, by design.
* :class:`ScimUserLink` — "this organization's identity provider provisioned
  this user": the IdP's ``externalId``, the name parts it sent, and whether it
  wants the user active. The row outlives a deprovisioning (``active`` false),
  so the IdP can still read and re-activate the user it deprovisioned. A
  removal by an owner or admin sets ``removed_outside_scim``: the IdP cannot
  undo it.
* :class:`ScimGroupLink` — the IdP's ``externalId`` of an organization group.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, false, true
from sqlalchemy.orm import Mapped, mapped_column

from tripl.models.base import Base, TimestampMixin, UtcDateTime, UUIDMixin

#: Every raw SCIM token starts with this, so a leaked one is recognisable.
SCIM_TOKEN_PREFIX = "tripl_scim_"
SCIM_EXTERNAL_ID_MAX_LENGTH = 255
SCIM_NAME_PART_MAX_LENGTH = 255


class OrgScimToken(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "org_scim_tokens"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # ``tripl_scim_`` plus the first characters of the secret: display only.
    prefix: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class OrgScimConfig(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "org_scim_configs"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), unique=True
    )
    # Members of this group hold organization role ``admin`` (owners keep
    # ``owner``); leaving it demotes to ``member``. Cleared when the group goes.
    admin_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_groups.id", ondelete="SET NULL"), nullable=True
    )


class ScimUserLink(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "scim_user_links"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_scim_user_links_org_user"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(
        String(SCIM_EXTERNAL_ID_MAX_LENGTH), nullable=True
    )
    # The name parts the IdP sent, echoed back as it sent them. The account's
    # own ``users.name`` changes only for an address in the organization's
    # verified domains (see ``scim_user_service``).
    given_name: Mapped[str | None] = mapped_column(String(SCIM_NAME_PART_MAX_LENGTH), nullable=True)
    family_name: Mapped[str | None] = mapped_column(
        String(SCIM_NAME_PART_MAX_LENGTH), nullable=True
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    # An owner or admin removed the membership outside SCIM: the IdP cannot
    # re-activate the user (409) until they are a member again (an accepted
    # invitation), which clears it.
    removed_outside_scim: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )


class ScimGroupLink(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "scim_group_links"
    __table_args__ = (UniqueConstraint("group_id", name="uq_scim_group_links_group"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organization_groups.id", ondelete="CASCADE")
    )
    external_id: Mapped[str | None] = mapped_column(
        String(SCIM_EXTERNAL_ID_MAX_LENGTH), nullable=True
    )
