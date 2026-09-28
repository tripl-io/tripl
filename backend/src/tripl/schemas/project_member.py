import uuid
from datetime import datetime

from pydantic import BaseModel

from tripl.models.domain_enums import ProjectMemberRole


class ProjectMemberCreate(BaseModel):
    user_id: uuid.UUID
    #: ``none`` opts an organization member out of the project whatever the
    #: organization's default project role; refused (422) for an org owner/admin.
    role: ProjectMemberRole = ProjectMemberRole.viewer


class ProjectMemberUpdate(BaseModel):
    role: ProjectMemberRole


class ProjectMemberResponse(BaseModel):
    user_id: uuid.UUID
    # ``name`` falls back to the email, the convention every other user-facing
    # response (event-type owners, reviewers) follows for a nameless account.
    name: str
    email: str
    role: ProjectMemberRole
    added_at: datetime
