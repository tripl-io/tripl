"""SCIM provisioning management bodies: ``/api/v1/orgs/{org}/scim/...`` (F20, GH #273).

The SCIM protocol itself (``/scim/v2/{org}/...``) speaks RFC 7643 JSON and has
no Pydantic models: see :mod:`tripl.services.scim_resources`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OrgScimTokenResponse(BaseModel):
    """One SCIM token of the organization. The secret is never returned again."""

    id: uuid.UUID
    #: ``tripl_scim_`` and the first characters of the secret, for recognising it.
    prefix: str
    created_at: datetime
    created_by_email: str | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class OrgScimTokenCreated(OrgScimTokenResponse):
    """``POST /scim/tokens``: the raw token, shown exactly once."""

    token: str


class OrgScimConfigResponse(BaseModel):
    """The organization's SCIM endpoint and settings."""

    #: What the identity provider is given as the SCIM connector base URL.
    base_url: str
    #: Members of this group hold organization role ``admin``; ``None``: no mapping.
    admin_group_id: uuid.UUID | None
    admin_group_name: str | None
    #: How many live (unrevoked) tokens the organization has.
    active_tokens: int


class OrgScimConfigUpdate(BaseModel):
    """``PUT /scim/config``. ``admin_group_id`` null removes the mapping."""

    model_config = ConfigDict(extra="forbid")

    admin_group_id: uuid.UUID | None = None
