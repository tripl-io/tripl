from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tripl.models.domain_enums import ShadowEventStatus


class ShadowEventCandidateResponse(BaseModel):
    id: uuid.UUID
    scan_config_id: uuid.UUID
    scan_config_name: str
    event_type_id: uuid.UUID | None
    event_type_name: str | None
    event_name: str
    observed_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    status: ShadowEventStatus
    accepted_event_id: uuid.UUID | None
    # Up to five column -> value dicts from the rows the latest collection saw
    # for this identity, so a reviewer sees what the event looks like before
    # accepting it. Empty until a collection observes the candidate.
    sample_properties: list[dict[str, str]] = Field(default_factory=list)


class ShadowEventListResponse(BaseModel):
    items: list[ShadowEventCandidateResponse]
    total: int
    new_count: int


class ShadowEventAcceptRequest(BaseModel):
    # Defaults to the candidate's detected event type / observed identity.
    event_type_id: uuid.UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=500)


class ShadowEventAcceptResponse(BaseModel):
    candidate_id: uuid.UUID
    event_id: uuid.UUID
    status: ShadowEventStatus


class ShadowEventDismissResponse(BaseModel):
    candidate_id: uuid.UUID
    status: ShadowEventStatus


# One request's worth of inbox rows. The inbox pages 100 at a time, so a
# selection is rarely larger; the bound keeps one request's work (an event
# create and a search reindex per accepted row) finite.
MAX_SHADOW_BATCH = 200


class ShadowEventBatchItem(BaseModel):
    candidate_id: uuid.UUID
    # Accept only, and optional there, exactly as on the single accept route.
    event_type_id: uuid.UUID | None = None
    name: str | None = Field(None, min_length=1, max_length=500)


class ShadowEventBatchRequest(BaseModel):
    """Accept or dismiss many inbox rows in one request."""

    action: Literal["accept", "dismiss"]
    items: list[ShadowEventBatchItem] = Field(min_length=1, max_length=MAX_SHADOW_BATCH)

    @field_validator("items")
    @classmethod
    def _unique_candidates(cls, items: list[ShadowEventBatchItem]) -> list[ShadowEventBatchItem]:
        ids = [item.candidate_id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("items repeats a candidate; list each one once")
        return items


class ShadowEventBatchItemResult(BaseModel):
    """What happened to one row. A refused row does not stop the rest."""

    candidate_id: uuid.UUID
    ok: bool
    status: ShadowEventStatus | None = None
    # The event an accepted row created.
    event_id: uuid.UUID | None = None
    # Why a row was refused, in the single route's own words, and its status
    # code (404 unknown, 409 already resolved, 422 no event type, ...).
    error: str | None = None
    error_status: int | None = None


class ShadowEventBatchResponse(BaseModel):
    results: list[ShadowEventBatchItemResult]
    succeeded: int
    failed: int


class DeadEventItem(BaseModel):
    event_id: uuid.UUID
    name: str
    event_type_id: uuid.UUID
    event_type_name: str
    last_seen_at: datetime | None
    created_at: datetime


class DeadEventListResponse(BaseModel):
    items: list[DeadEventItem]
    total: int
    days: int


class CoverageBucket(BaseModel):
    bucket: datetime
    total_count: int
    matched_count: int


class CoverageSummary(BaseModel):
    total_count: int
    matched_count: int
    coverage_pct: float


class CoverageResponse(BaseModel):
    items: list[CoverageBucket]
    summary: CoverageSummary
    days: int
