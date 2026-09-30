"""The platform console API (F20 PR14, GH #273): organizations, users, step-ins.

Metadata and counts only: nothing here carries a project's content.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tripl.models.domain_enums import OrganizationRole, OrganizationStatus

REASON_MAX_LENGTH = 500
STEP_IN_TTL_DEFAULT_MINUTES = 60
STEP_IN_TTL_MIN_MINUTES = 5
STEP_IN_TTL_MAX_MINUTES = 240


def _clean_reason(value: str) -> str:
    if "\x00" in value:
        raise ValueError("reason must not contain NUL bytes")
    stripped = value.strip()
    if not stripped:
        raise ValueError("reason must not be blank")
    return stripped


class PlatformOrgItem(BaseModel):
    """One organization in the console list."""

    id: uuid.UUID
    slug: str
    name: str
    status: OrganizationStatus
    is_default: bool
    created_at: datetime
    suspended_at: datetime | None
    suspended_reason: str | None
    member_count: int
    project_count: int
    owner_emails: list[str]


class PlatformOrgList(BaseModel):
    items: list[PlatformOrgItem]
    total: int


class PlatformOrgMember(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str | None
    role: OrganizationRole


class PlatformOrgProject(BaseModel):
    """A project's metadata: never its plan, data or settings."""

    slug: str
    name: str
    created_at: datetime


class PlatformOrgDetail(PlatformOrgItem):
    members: list[PlatformOrgMember]
    projects: list[PlatformOrgProject]


class OrgSuspendRequest(BaseModel):
    """Why the organization is suspended. Shown to platform admins only."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _clean_reason(value)


class PlatformUserItem(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    is_platform_admin: bool
    email_verified: bool
    created_at: datetime
    org_count: int


class PlatformUserList(BaseModel):
    items: list[PlatformUserItem]
    total: int


class PlatformAdminGrant(BaseModel):
    """Grant (``true``) or revoke (``false``) the platform-admin flag."""

    model_config = ConfigDict(extra="forbid")

    grant: bool


class StepInRequest(BaseModel):
    """Start a read-only step-in: a mandatory reason and a time limit in minutes."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    ttl_minutes: int = Field(
        default=STEP_IN_TTL_DEFAULT_MINUTES,
        ge=STEP_IN_TTL_MIN_MINUTES,
        le=STEP_IN_TTL_MAX_MINUTES,
    )

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return _clean_reason(value)


class StepInResponse(BaseModel):
    """One step-in. ``active`` is "not ended and not expired" at response time."""

    id: uuid.UUID
    org_slug: str
    org_name: str
    reason: str
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    active: bool
