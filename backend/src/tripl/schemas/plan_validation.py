"""Request and response of ``POST /projects/{slug}/plan/validate`` (GH #261, F08).

One validator for every caller: ``tripl check`` (static scan and captured
payloads), CI, and later the QA stream. The body is a batch of calls; the
answer is one verdict per call, in order, plus totals. Nothing is persisted.

A field value of ``null`` means "set at runtime, unknown when the code was
scanned". It is never an error.
"""

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

#: The most calls one request may carry. The CLI batches above this.
MAX_VALIDATION_ITEMS = 5000
MAX_FIELDS_PER_ITEM = 200

FieldKey = Annotated[str, StringConstraints(min_length=1, max_length=255)]
FieldValue = Annotated[str, StringConstraints(max_length=2000)]

PlanValidationCode = Literal[
    "unknown_event_type",
    "unknown_event",
    "deprecated_event",
    "unknown_field",
    "missing_required_field",
    "value_not_allowed",
    "dynamic_value",
    "too_dynamic",
    "wrong_type",
    "policy_violation",
]


class PlanValidationItemIn(BaseModel):
    # Echoed back as-is; the CLI sends "path:line" or the payload line number.
    # Defaults to the item's position in the batch.
    ref: str | None = Field(None, max_length=1000)
    event_type: str | None = Field(None, max_length=100)
    # The event's identity or name, as the call spells it. ``${...}`` holes
    # stand for interpolated parts (``promo_sheet_${id}_shown``).
    name: str | None = Field(None, max_length=500)
    # Scalars only; numbers and booleans are rendered the way the scan renders
    # a JSON value (``core.plan_validation.scalar_text``).
    fields: dict[FieldKey, FieldValue | bool | int | float | None] = Field(
        default_factory=dict, max_length=MAX_FIELDS_PER_ITEM
    )
    # A free-form property dict. Its keys are checked as field names and its
    # scalar values as field values; containers are accepted unchecked.
    properties: dict[FieldKey, Any] | None = Field(None, max_length=MAX_FIELDS_PER_ITEM)
    # Payload mode: the item is the whole event, so a required field it lacks
    # is an error. A static scan cannot know that and leaves this false.
    complete: bool = False


class PlanValidationRequest(BaseModel):
    items: list[PlanValidationItemIn] = Field(..., max_length=MAX_VALIDATION_ITEMS)
    # Report values set at runtime as ``info`` findings (``tripl check --strict``).
    strict: bool = False


class PlanValidationFinding(BaseModel):
    code: PlanValidationCode
    severity: Literal["error", "warning", "info"]
    field: str | None = None
    message: str
    # Set on a ``policy_violation`` finding: the installed extension's rule the
    # call breaks (``naming.event``, say). Community reports no such findings.
    rule: str | None = None


class PlanValidationItemResult(BaseModel):
    ref: str
    status: Literal["ok", "warning", "error"]
    event_id: uuid.UUID | None = None
    # The identity the call was matched by, with ``${key}`` holes for the
    # parts only known at runtime.
    identity: str | None = None
    findings: list[PlanValidationFinding] = Field(default_factory=list)


class PlanValidationSummary(BaseModel):
    ok: int = 0
    warnings: int = 0
    errors: int = 0


class PlanValidationResponse(BaseModel):
    items: list[PlanValidationItemResult]
    summary: PlanValidationSummary
