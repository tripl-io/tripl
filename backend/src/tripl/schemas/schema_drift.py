import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tripl.models.domain_enums import SchemaDriftStatus, SchemaDriftType
from tripl.schemas.time_guards import require_future_instant

SchemaDriftAction = Literal["accept", "snooze", "false_positive", "reopen"]


class SchemaDriftResponse(BaseModel):
    id: uuid.UUID
    event_type_id: uuid.UUID
    scan_config_id: uuid.UUID | None
    field_name: str
    drift_type: SchemaDriftType
    observed_type: str | None
    declared_type: str | None
    sample_value: str | None
    status: SchemaDriftStatus = SchemaDriftStatus.open
    resolution_note: str | None = None
    snoozed_until: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: uuid.UUID | None = None
    detected_at: datetime

    model_config = {"from_attributes": True}


class SchemaDriftListResponse(BaseModel):
    items: list[SchemaDriftResponse]
    total: int


class SchemaDriftActionRequest(BaseModel):
    action: SchemaDriftAction
    note: str | None = Field(None, max_length=2000)
    snoozed_until: datetime | None = None
    force: bool = Field(
        False,
        description=(
            "Override the guard that refuses to accept a missing_field drift for a "
            "column a scan config's event name format builds event names from "
            ". API-only escape hatch for a project-wide config that "
            "names the column but never scans this event type; requires a note "
            "explaining why, which lands in the audit record. The UI does not "
            "offer it — a warning next to an Accept button is a thing operators "
            "click past, and clicking past it is what caused the outage."
        ),
    )

    @model_validator(mode="after")
    def validate_action(self) -> SchemaDriftActionRequest:
        if self.action == "snooze":
            if self.snoozed_until is None:
                raise ValueError("snoozed_until is required when action is snooze")
            # The two guards answer one question — can this snooze hide anything
            # — half a second apart. A snooze whose end has already passed hides
            # the drift for no time at all: the list works out what is snoozed by
            # comparing the stored instant against now when it is READ
            # (``schema_drift_service``), so an expired one is back in the open
            # rows the moment the 200 lands, and the operator is told a decision
            # was recorded that changed nothing they can see.
            # Only the arriving instant is bounded; a snooze that lapses later is
            # supposed to come back, and that is a different mechanism entirely.
            self.snoozed_until = require_future_instant(
                self.snoozed_until, field_name="snoozed_until"
            )
        elif self.snoozed_until is not None:
            # A snooze date on an accept, a false positive or a reopen is a
            # client that meant to snooze; accepting and discarding it would
            # hide the mistake. Same rule as ``EventCommentActionRequest``.
            raise ValueError("snoozed_until is only meaningful when action is snooze")
        if self.force and self.action != "accept":
            # `force` overrides exactly one guard, and that guard only fires on
            # the accept path. Accepting it elsewhere would make the contract
            # looser than the thing it overrides, and would silently swallow a
            # client that sent it by mistake — on a field whose whole purpose is
            # to be hard to reach by accident.
            raise ValueError("force is only meaningful when action is accept")
        if self.force and not (self.note or "").strip():
            raise ValueError("note is required when force is set")
        return self
