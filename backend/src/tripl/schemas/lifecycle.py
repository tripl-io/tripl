"""Lifecycle enforcement payloads (#258): findings and successor adoption."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

LifecycleFindingKindValue = Literal["sunset_overdue", "successor_silent"]


class LifecycleFindingResponse(BaseModel):
    """One finding of the daily sunset watch.

    ``event_id`` is the DEPRECATED event (main branch) the finding hangs on;
    ``related_event_id`` is its successor for ``successor_silent`` and null for
    ``sunset_overdue``. ``volume_24h`` is set on ``sunset_overdue``,
    ``successor_volume_7d`` on ``successor_silent``. ``resolved_at`` is null
    while the condition still holds.
    """

    id: uuid.UUID
    event_id: uuid.UUID
    event_name: str
    kind: LifecycleFindingKindValue
    related_event_id: uuid.UUID | None = None
    related_event_name: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None
    volume_24h: int | None = None
    successor_volume_7d: int | None = None


class LifecycleFindingListResponse(BaseModel):
    items: list[LifecycleFindingResponse]
    total: int


class EventMigrationSide(BaseModel):
    event_id: uuid.UUID
    name: str
    #: Volume over the last 7 days divided by 7. A bucket straddling the
    #: window's start counts only the fraction of it inside the window.
    daily_avg_7d: float


class EventMigrationResponse(BaseModel):
    """Successor adoption for a retired event: "Old 1,240/day → New 3,800/day".

    ``ratio`` is ``new / old`` daily volume — how many times the old event's
    volume the successor now receives (1,240 -> 3,800 is ``3.06``). Above 1 the
    successor has overtaken the old event. Null when the old event received
    nothing in the window.
    """

    old: EventMigrationSide
    new: EventMigrationSide
    ratio: float | None = None
