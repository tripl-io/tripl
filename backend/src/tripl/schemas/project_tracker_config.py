import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ProjectTrackerConfigUpdate(BaseModel):
    """Partial update — every field optional. ``api_token`` is the RAW token on
    input; it is encrypted at rest and never echoed back. Passing ``""`` clears
    the stored token; omitting / null leaves it unchanged.

    ``tracker_type`` picks the backend. For ``jira`` the credential is the Jira
    API token and ``base_url`` / ``auth_email`` / ``project_key`` /
    ``issue_type`` apply. For ``linear`` ``api_token`` carries the Linear API
    key (same encryption, same never-echoed rule) and ``team_id`` the Linear
    team; the Jira-only fields are ignored. Switching ``tracker_type`` clears
    the stored credential unless the same request supplies a new one, so one
    vendor's secret is never sent to the other."""

    enabled: bool | None = None
    tracker_type: Literal["jira", "linear"] | None = None
    base_url: str | None = None
    project_key: str | None = None
    auth_email: str | None = None
    api_token: str | None = None
    issue_type: str | None = None
    team_id: str | None = None


class ProjectTrackerConfigResponse(BaseModel):
    """``id``/timestamps are None while the project rides the defaults — the row
    is only materialized on the first PATCH. The token is never returned; only
    ``api_token_set`` signals whether one is stored."""

    id: uuid.UUID | None = None
    project_id: uuid.UUID
    enabled: bool
    tracker_type: str
    base_url: str
    project_key: str
    auth_email: str
    issue_type: str
    # Linear team id; "" unless ``tracker_type`` is ``linear``.
    team_id: str = ""
    api_token_set: bool
    #: Fields the project leaves empty and takes from its organization's tracker
    #: defaults (F20 PR12): ``base_url``, ``auth_email``, ``api_token`` and
    #: ``project_key`` for Jira; ``api_token`` and ``team_id`` for Linear. The
    #: Jira site, account and token are one unit: a project setting any of them
    #: inherits none of the three.
    inherited_fields: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}
