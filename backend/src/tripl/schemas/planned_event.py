from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from tripl.models.domain_enums import AnomalyDirection, ChartAnnotationScopeType

PLANNED_EVENT_LABEL_MAX = 200
PLANNED_EVENT_DESCRIPTION_MAX = 2000


class PlannedEventCreate(BaseModel):
    """A window in which the project expects its numbers to move (F18).

    ``direction`` NULL expects either way. The scope pairs like a chart
    annotation's: both NULL covers every series in the project.
    """

    label: str = Field(min_length=1, max_length=PLANNED_EVENT_LABEL_MAX)
    description: str | None = Field(default=None, max_length=PLANNED_EVENT_DESCRIPTION_MAX)
    starts_at: datetime
    ends_at: datetime
    direction: AnomalyDirection | None = None
    scope_type: ChartAnnotationScopeType | None = None
    scope_ref: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def _check_window_and_scope(self) -> PlannedEventCreate:
        if not self.label.strip():
            raise ValueError("label must not be blank")
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("starts_at and ends_at must carry a timezone")
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if (self.scope_type is None) != (self.scope_ref is None):
            raise ValueError("scope_type and scope_ref must both be provided or both be null")
        return self


class PlannedEventUpdate(BaseModel):
    """A partial edit; the merged event is validated as a whole by the route."""

    label: str | None = Field(default=None, min_length=1, max_length=PLANNED_EVENT_LABEL_MAX)
    description: str | None = Field(default=None, max_length=PLANNED_EVENT_DESCRIPTION_MAX)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    direction: AnomalyDirection | None = None
    scope_type: ChartAnnotationScopeType | None = None
    scope_ref: str | None = Field(default=None, min_length=1, max_length=120)


class PlannedEventResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    label: str
    description: str | None
    starts_at: datetime
    ends_at: datetime
    direction: AnomalyDirection | None
    scope_type: ChartAnnotationScopeType | None
    scope_ref: str | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # The series' name (event, event type, metric or scan), filled on the list
    # for the project-wide Annotations page; null when project-wide or when the
    # series no longer exists.
    scope_name: str | None = None

    model_config = {"from_attributes": True}
