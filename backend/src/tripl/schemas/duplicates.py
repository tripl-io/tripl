"""Duplicate detection and naming lint (GH #265, F12): request and response shapes.

``POST /projects/{slug}/events/duplicate-check`` is read-like: it checks up to
:data:`MAX_DUPLICATE_CANDIDATES` would-be events against the catalog and
writes nothing. ``GET /projects/{slug}/duplicates`` lists clusters of existing
events that look like one event spelled several ways, and
``POST /projects/{slug}/duplicates/dismiss`` records "these two are not
duplicates". Merging is NOT an endpoint here: the UI sets the successor and
deprecates through the existing event update.
"""

import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tripl.schemas.event import EventFieldValueIn

MAX_DUPLICATE_CANDIDATES = 500

LintCode = Literal["case", "separator", "verb_order", "prefix"]


class DuplicateCandidateIn(BaseModel):
    name: str = Field("", max_length=500)
    event_type_id: uuid.UUID
    description: str | None = Field(None, max_length=10_000)
    # Under a naming rule the name is generated from these, so a form that has
    # not rendered it yet may send the values and an empty name.
    field_values: list[EventFieldValueIn] = Field(default_factory=list, max_length=500)
    # The event being edited, so it is never reported as its own duplicate.
    event_id: uuid.UUID | None = None


class DuplicateCheckRequest(BaseModel):
    candidates: list[DuplicateCandidateIn] = Field(
        ..., min_length=1, max_length=MAX_DUPLICATE_CANDIDATES
    )


class DuplicateHit(BaseModel):
    event_id: uuid.UUID
    name: str
    event_type_id: uuid.UUID
    status: str
    # Combined score in 0..1 (lexical, blended with an embedding cosine when
    # one is available); the UI shows it as a percentage.
    score: float
    # Human-readable evidence: "similar name", "same event type",
    # "semantic match", "3 shared field values", "same name".
    reasons: list[str] = Field(default_factory=list)


class NameLintIssue(BaseModel):
    code: LintCode
    message: str
    suggestion: str


class NamingConventionOut(BaseModel):
    case: Literal["snake", "camel", "pascal", "kebab", "space", "mixed"]
    space_style: Literal["title", "lower", "sentence"] | None = None
    separator: str | None = None
    verb_position: Literal["first", "last", "unknown"]
    prefix: str | None = None
    confidence: float
    sample_size: int


class DuplicateCheckResult(BaseModel):
    # The name that was checked: the candidate's own, or the one its naming
    # rule renders from ``field_values``.
    name: str
    duplicates: list[DuplicateHit] = Field(default_factory=list)
    lint: list[NameLintIssue] = Field(default_factory=list)
    # A name that fixes every lint issue at once; null when there is none.
    suggestion: str | None = None
    # False when the event type has a naming rule: the rule, not the author,
    # decides the spelling, so nothing is linted.
    lint_applicable: bool = True
    convention: NamingConventionOut | None = None


class DuplicateCheckResponse(BaseModel):
    items: list[DuplicateCheckResult]
    threshold: float
    # Whether an embedding cosine took part (search embeddings configured and
    # the provider answered); otherwise the scores are lexical only.
    semantic_used: bool = False


class DuplicateClusterEvent(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    event_type_id: uuid.UUID
    # Events counted in the last 7 days (0 when never collected).
    volume_7d: int = 0


class DuplicateCluster(BaseModel):
    events: list[DuplicateClusterEvent]
    # The strongest pairwise score inside the cluster.
    score: float


class DuplicateClusterPage(BaseModel):
    items: list[DuplicateCluster]
    # Opaque; pass back as ``?cursor=`` for the next page. Null on the last page.
    next_cursor: str | None = None
    total: int
    threshold: float
    # True when the catalog was larger than the cap the clustering reads.
    truncated: bool = False


class DuplicateDismissRequest(BaseModel):
    event_a_id: uuid.UUID
    event_b_id: uuid.UUID

    @model_validator(mode="after")
    def _distinct(self) -> DuplicateDismissRequest:
        if self.event_a_id == self.event_b_id:
            raise ValueError("event_a_id and event_b_id must differ")
        return self


class DuplicateDismissResponse(BaseModel):
    event_a_id: uuid.UUID
    event_b_id: uuid.UUID
    created: bool
