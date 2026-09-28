"""The organization management API (F20 PR6, GH #273)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tripl.models.domain_enums import OrganizationRole, OrganizationStatus
from tripl.schemas.project import RESERVED_PROJECT_SLUGS

#: The shape of an organization slug: the same as a project slug's.
ORG_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

#: Slugs no organization may take. The project list plus the default
#: organization's own slug (taken anyway, but refused with a clear message
#: rather than a 409) and the platform console's future segment.
RESERVED_ORG_SLUGS: frozenset[str] = RESERVED_PROJECT_SLUGS | {"default", "platform"}


class OrgCreate(BaseModel):
    """A new organization. The slug is permanent (owner decision 6)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255, pattern=ORG_SLUG_PATTERN)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped

    @field_validator("slug")
    @classmethod
    def _slug_not_reserved(cls, value: str) -> str:
        if value in RESERVED_ORG_SLUGS:
            raise ValueError(f"'{value}' is reserved and cannot be used as an organization slug")
        return value


class OrgUpdate(BaseModel):
    """A rename. ``extra="forbid"``: the slug is immutable, so sending one is a 422."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class OrgDeleteRequest(BaseModel):
    """The typed confirmation: the organization's slug, exactly."""

    confirm_slug: str = Field(min_length=1, max_length=255)


class OrgTransferOwnership(BaseModel):
    """Hand the organization to another member, who becomes an owner."""

    user_id: uuid.UUID


class OrgResponse(BaseModel):
    """One organization, with the caller's role in it."""

    id: uuid.UUID
    slug: str
    name: str
    role: OrganizationRole
    status: OrganizationStatus
    is_default: bool
    created_at: datetime


class OrgMemberRemoved(BaseModel):
    """What removing a member took away with the membership (critique #28)."""

    user_id: uuid.UUID
    project_memberships_removed: int
    api_keys_revoked: int
    #: Unused invitations into the organization the member sent, or that were
    #: addressed to them; each would otherwise have let them back in.
    invitations_revoked: int
    #: The organization's groups the member was in (F20 groups).
    group_memberships_removed: int
