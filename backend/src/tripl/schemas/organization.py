"""The organization management API (F20 PR6, GH #273)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tripl.models.domain_enums import OrganizationRole, OrganizationStatus, ProjectMemberRole
from tripl.schemas.project import RESERVED_PROJECT_SLUGS

#: The shape of an organization slug: the same as a project slug's.
ORG_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

#: Slugs no organization may take. The project list plus the default
#: organization's own slug (taken anyway, but refused with a clear message
#: rather than a 409) and the platform console's future segment.
RESERVED_ORG_SLUGS: frozenset[str] = RESERVED_PROJECT_SLUGS | {"default", "platform"}
ORG_NAME_MAX_LENGTH = 255
ORG_SLUG_MAX_LENGTH = 255


def clean_org_name(value: str) -> str:
    """An organization name, stripped; blank is refused (``ValueError``)."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    return stripped


def check_org_slug_not_reserved(value: str) -> str:
    """Refuse a slug no organization may take (``ValueError``)."""
    if value in RESERVED_ORG_SLUGS:
        raise ValueError(f"'{value}' is reserved and cannot be used as an organization slug")
    return value


def check_org_slug(value: str) -> str:
    """The full slug rule for a slug not declared through ``Field`` constraints.

    Length, :data:`ORG_SLUG_PATTERN` and the reserved list: what ``OrgCreate``
    enforces, for the hosted sign-up form's ``org_slug`` (``schemas.auth``).
    """
    if not value or len(value) > ORG_SLUG_MAX_LENGTH:
        raise ValueError(f"slug must be 1 to {ORG_SLUG_MAX_LENGTH} characters")
    if re.fullmatch(ORG_SLUG_PATTERN, value) is None:
        raise ValueError(
            "slug may contain only lowercase letters, digits and single hyphens between them"
        )
    return check_org_slug_not_reserved(value)


class OrgCreate(BaseModel):
    """A new organization. The slug is permanent (owner decision 6)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=ORG_NAME_MAX_LENGTH)
    slug: str = Field(min_length=1, max_length=ORG_SLUG_MAX_LENGTH, pattern=ORG_SLUG_PATTERN)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return clean_org_name(value)

    @field_validator("slug")
    @classmethod
    def _slug_not_reserved(cls, value: str) -> str:
        return check_org_slug_not_reserved(value)


class OrgUpdate(BaseModel):
    """A partial update: the name and/or the default project role.

    ``extra="forbid"``: the slug is immutable, so sending one is a 422. At
    least one field must be sent.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    #: The project role an organization member gets on a project they hold no
    #: membership row in: ``none`` (no access), ``viewer`` or ``editor``. Never
    #: ``owner``: ``ProjectMemberRole`` has no such value, so it is a 422.
    default_project_role: ProjectMemberRole | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> OrgUpdate:
        if self.name is None and self.default_project_role is None:
            raise ValueError("send name and/or default_project_role")
        return self

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
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
    #: The project role a member gets on a project they hold no row in.
    default_project_role: ProjectMemberRole
    created_at: datetime
    #: Listed because a platform admin has a live read-only step-in to it, not
    #: a membership (F20 PR14); ``role`` is then ``member``.
    step_in: bool = False


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
