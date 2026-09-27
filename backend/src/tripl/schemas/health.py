"""Event health score payloads (F15, #268).

Mirrored by ``frontend/src/types/health.ts``. Every response answers about the
MAIN plan's non-archived events; see ``services/health_score.py`` for how a
score is built and ``services/health_weights.py`` for the fixed weights.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from tripl.services.health_weights import HealthComponentKey, HealthGrade


class HealthComponent(BaseModel):
    """One of the six parts of an event's score.

    ``weight`` is the fixed weight; ``effective_weight`` is its share in percent
    after renormalization over the components that apply (null when excluded).
    ``points`` is ``effective_weight * value``; the points of an event sum to its
    score up to rounding.
    """

    key: HealthComponentKey
    label: str
    weight: int
    applies: bool
    excluded_reason: str | None = None
    value: float | None = None
    effective_weight: float | None = None
    points: float | None = None
    detail: str
    counts: dict[str, int] = Field(default_factory=dict)


class EventHealth(BaseModel):
    event_id: uuid.UUID
    event_type_id: uuid.UUID
    name: str
    score: int
    grade: HealthGrade
    renormalized: bool
    excluded: list[HealthComponentKey]
    top_issue: str | None = None
    components: list[HealthComponent]


class EventHealthBrief(BaseModel):
    event_id: uuid.UUID
    name: str
    score: int
    grade: HealthGrade
    top_issue: str | None = None


class ComponentAverage(BaseModel):
    """Mean component value over the events it applies to (null when none)."""

    key: HealthComponentKey
    value: float | None = None
    applies_count: int


class EventHealthListResponse(BaseModel):
    items: list[EventHealth]
    computed_at: datetime


class EventTypeHealth(BaseModel):
    event_type_id: uuid.UUID
    score: int | None = None
    grade: HealthGrade | None = None
    scored_events: int
    healthy_count: int
    warning_count: int
    unhealthy_count: int
    component_averages: list[ComponentAverage]
    worst: list[EventHealthBrief]


class EventTypeHealthListResponse(BaseModel):
    items: list[EventTypeHealth]


class ProjectHealthTrendPoint(BaseModel):
    day: date
    score: int | None = None
    scored_events: int


class ProjectHealthResponse(BaseModel):
    score: int | None = None
    grade: HealthGrade | None = None
    scored_events: int
    healthy_count: int
    warning_count: int
    unhealthy_count: int
    component_averages: list[ComponentAverage]
    worst: list[EventHealthBrief]
    trend: list[ProjectHealthTrendPoint]
    previous_score: int | None = None
    computed_at: datetime
