"""Organization groups: ``/api/v1/orgs/{org}/groups`` (F20, GH #273)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tripl.models.organization_group import GROUP_DESCRIPTION_MAX_LENGTH, GROUP_NAME_MAX_LENGTH

_NUL = "\x00"


def _clean_name(value: str) -> str:
    # Postgres ``text`` cannot hold U+0000: refuse it here (422) instead of
    # letting asyncpg fail the INSERT (500).
    if _NUL in value:
        raise ValueError("name must not contain a NUL character")
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    return stripped


def _clean_description(value: str) -> str:
    if _NUL in value:
        raise ValueError("description must not contain a NUL character")
    return value.strip()


class OrgGroupCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=GROUP_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=GROUP_DESCRIPTION_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return _clean_description(value)


class OrgGroupUpdate(BaseModel):
    """A rename and/or a new description. Omitted fields are left alone; ``null`` is a 422."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=GROUP_NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=GROUP_DESCRIPTION_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be null; omit it to keep the current name")
        return _clean_name(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("description cannot be null; send an empty string to clear it")
        return _clean_description(value)


class OrgGroupMemberAdd(BaseModel):
    """Add one member of the organization to the group."""

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID


class OrgGroupMemberResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str
    added_at: datetime


class OrgGroupResponse(BaseModel):
    """One group, with how many members it has."""

    id: uuid.UUID
    name: str
    description: str
    member_count: int
    created_at: datetime
    updated_at: datetime


class OrgGroupDetail(OrgGroupResponse):
    """One group with its members, by name."""

    members: list[OrgGroupMemberResponse]
