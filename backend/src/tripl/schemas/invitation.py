from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator

from tripl.models.domain_enums import OrganizationRole
from tripl.schemas.auth import PASSWORD_MAX_LENGTH, validate_password_strength


class InvitationCreate(BaseModel):
    """What an owner submits to invite one person."""

    email: EmailStr
    # The ORGANIZATION role the invitee joins with. Defaults to member because
    # that is what self-service registration produces, so inviting is not
    # quietly more privileged than the door it replaces. Inviting an owner
    # takes an owner.
    role: OrganizationRole = OrganizationRole.member


class InvitationResponse(BaseModel):
    """A pending invitation as shown on the Members screen.

    Carries no token and no digest of one: the raw token is returned exactly
    once, by the create call, and listing invitations must never hand out a way
    to redeem them.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    email: str
    # The organization role the invitee joins with, read from
    # ``invitations.org_role``.
    role: OrganizationRole = Field(validation_alias="org_role")
    invited_by_user_id: uuid.UUID | None
    expires_at: datetime
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_expired(self) -> bool:
        """Whether this link has aged out.

        Computed rather than stored so it cannot go stale, and surfaced because
        expired-but-unused rows are deliberately still listed — an owner needs
        to see that a link they sent no longer works.
        """
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC)


class InvitationCreatedResponse(BaseModel):
    """The mint response — the ONLY place the redeem link ever appears.

    ``accept_path`` is a path, not an absolute URL: the backend does not reliably
    know its own public origin (it sits behind a proxy, and the SPA may be served
    from a different host), so the client joins it to its own origin rather than
    the server guessing wrong and producing an unusable link.
    """

    invitation: InvitationResponse
    accept_path: str
    expires_at: datetime


class InvitationPreview(BaseModel):
    """What the redeem screen may show before an account exists.

    Reveals only what the person holding the link already has, so it stays
    unauthenticated. Notably it does NOT confirm whether the instance has other
    users or what they are.
    """

    email: str
    role: OrganizationRole
    expires_at: datetime


class InvitationAcceptRequest(BaseModel):
    """Redeeming an invitation.

    Carries no email: the address comes from the invitation itself, so a link
    cannot be turned into an account for someone else. Password strength is the
    same policy as registration — enforced here at the schema boundary, so an
    invalid password never reaches the service.
    """

    # Required to create the account. A signed-in account accepting an
    # invitation into another organization sends none (F20 PR6): it keeps its
    # own password and gains a membership.
    password: str | None = Field(default=None, max_length=PASSWORD_MAX_LENGTH)
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def _enforce_password_policy(cls, value: str | None) -> str | None:
        return None if value is None else validate_password_strength(value)
