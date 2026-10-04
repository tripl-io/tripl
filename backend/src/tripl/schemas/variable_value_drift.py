import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tripl.models.domain_enums import SchemaDriftStatus
from tripl.schemas.time_guards import require_future_instant

VariableValueDriftAction = Literal["accept", "snooze", "false_positive", "reopen"]
VariableValueDriftAcceptScope = Literal["global", "event"]


class VariableValueDriftResponse(BaseModel):
    id: uuid.UUID
    variable_id: uuid.UUID
    variable_name: str
    event_id: uuid.UUID
    event_name: str
    scan_config_id: uuid.UUID | None
    observed_values: list[str] = []
    status: SchemaDriftStatus = SchemaDriftStatus.open
    resolution_note: str | None = None
    snoozed_until: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: uuid.UUID | None = None
    detected_at: datetime

    model_config = {"from_attributes": True}


class VariableValueDriftListResponse(BaseModel):
    items: list[VariableValueDriftResponse]
    total: int


class VariableValueDriftActionRequest(BaseModel):
    action: VariableValueDriftAction
    # accept only: "global" appends the novel values to the variable's
    # allowed_values; "event" appends to (or creates) the per-event override,
    # seeded from the currently effective documented list.
    scope: VariableValueDriftAcceptScope = "global"
    note: str | None = Field(None, max_length=2000)
    snoozed_until: datetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> VariableValueDriftActionRequest:
        if self.action == "snooze":
            if self.snoozed_until is None:
                raise ValueError("snoozed_until is required when action is snooze")
            # Same pair of guards, same reasoning, as ``SchemaDriftActionRequest``
            # — an end that has already passed hides this drift for no time at
            # all, because ``variable_value_drift_service`` decides what is
            # snoozed by reading the stored instant against now, and the frontend
            # repeats that reading in ``lib/variableDrift.ts`` so the row is back
            # among the open ones on the very next render.
            self.snoozed_until = require_future_instant(
                self.snoozed_until, field_name="snoozed_until"
            )
        elif self.snoozed_until is not None:
            # Refused rather than discarded, as on ``SchemaDriftActionRequest``
            # and ``EventCommentActionRequest``.
            raise ValueError("snoozed_until is only meaningful when action is snooze")
        return self
