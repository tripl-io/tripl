"""Incident summary (F14, #267): a short, cited, AI-written account of one incident."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

IncidentSummaryFactKind = Literal[
    "incident", "scope", "attribution", "release", "similar", "note", "comment"
]
IncidentSummaryRole = Literal["what_broke", "cause", "release", "history", "discussion"]
IncidentSummaryState = Literal["disabled", "missing", "stale", "ready", "failed"]
IncidentSummaryDisabledReason = Literal["ai_off", "demo"]


class IncidentSummaryFact(BaseModel):
    # 1-based; what a sentence's ``fact_ids`` (rendered ``[n]``) refer to.
    id: int
    kind: IncidentSummaryFactKind
    # Already redacted: no sensitive field values, no drift sample values.
    text: str
    # In-app route, or null when the fact has nowhere to link.
    href: str | None = None


class IncidentSummarySentence(BaseModel):
    # No inline ``[n]`` markers: citations are ``fact_ids``, rendered by the UI.
    text: str
    role: IncidentSummaryRole
    # Non-empty and a subset of the body's fact ids, except on the backend's own
    # unknown-cause sentence.
    fact_ids: list[int]
    # False for the fixed "The cause is unknown ..." sentence the backend adds.
    generated: bool = True


class IncidentSummaryBody(BaseModel):
    sentences: list[IncidentSummarySentence]
    # The facts this body was generated from; its citations resolve here.
    facts: list[IncidentSummaryFact]
    cause_known: bool
    facts_hash: str
    generated_at: datetime


class IncidentSummaryResponse(BaseModel):
    correlation_group_id: uuid.UUID
    state: IncidentSummaryState
    disabled_reason: IncidentSummaryDisabledReason | None = None
    # Null when disabled: no facts are gathered then.
    current_facts_hash: str | None = None
    # Set for ``ready`` and ``stale`` (the previous body), null otherwise.
    summary: IncidentSummaryBody | None = None
