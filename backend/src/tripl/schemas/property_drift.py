import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from tripl.models.domain_enums import SchemaDriftStatus
from tripl.models.property_drift import PropertyDriftKind
from tripl.schemas.time_guards import require_future_instant

PropertyDriftAction = Literal["accept", "snooze", "false_positive", "reopen"]


class PropertyDriftResponse(BaseModel):
    id: uuid.UUID
    variable_id: uuid.UUID
    variable_name: str
    event_id: uuid.UUID | None = None
    event_name: str | None = None
    scan_config_id: uuid.UUID | None = None
    kind: PropertyDriftKind
    detail: dict[str, Any] = Field(
        default={},
        description="What the scan saw: presence_rate (and threshold) for new_property and"
        " missing_required; expected_type, observed_type and observed_schema for type_change."
        " For an object property whose sampled objects disagree with its sub-schema,"
        " type_change also carries nested_changes (each a path, a change of new_key,"
        " missing_required or type_change, and for a type change the expected and observed"
        " types), and observed_schema is the stored sub-schema with those changes applied.",
    )
    status: SchemaDriftStatus = SchemaDriftStatus.open
    resolution_note: str | None = None
    snoozed_until: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: uuid.UUID | None = None
    detected_at: datetime

    model_config = {"from_attributes": True}


class PropertyDriftListResponse(BaseModel):
    items: list[PropertyDriftResponse]
    total: int


class PropertyDriftActionRequest(BaseModel):
    """Triage one property drift.

    ``accept`` changes the plan to match what the scan saw: a new property
    joins the event's property list, a missing one stops being required, and
    a type change retypes the variable to the observed type.
    """

    action: PropertyDriftAction
    note: str | None = Field(None, max_length=2000)
    snoozed_until: datetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> PropertyDriftActionRequest:
        if self.action == "snooze":
            if self.snoozed_until is None:
                raise ValueError("snoozed_until is required when action is snooze")
            self.snoozed_until = require_future_instant(
                self.snoozed_until, field_name="snoozed_until"
            )
        elif self.snoozed_until is not None:
            raise ValueError("snoozed_until is only meaningful when action is snooze")
        return self
