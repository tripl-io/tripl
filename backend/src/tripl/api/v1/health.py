"""Event health score reads (F15, #268).

Every route is a read: no write gate, so a viewer member may call them, and the
membership gate mounted in ``api.v1.router`` answers 404 to a non-member. They
take no branch parameter and always answer about the MAIN plan's non-archived
events — the rows scans write facts for.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tripl.api.deps import SessionDep
from tripl.schemas.health import (
    EventHealth,
    EventHealthListResponse,
    EventTypeHealthListResponse,
    ProjectHealthResponse,
)
from tripl.services import event_health_service

router = APIRouter(prefix="/projects/{slug}", tags=["health"])

# The catalog chunks its loaded ids client-side. The ids travel in the query
# string (about 41 bytes each); 150 keeps the request line near 6 KB, under the
# 8 KB a proxy with nginx's default header buffers accepts before answering 414.
# Must equal HEALTH_BATCH_MAX_IDS in frontend/src/api/health.ts.
MAX_HEALTH_IDS = 150


@router.get("/health/events", response_model=EventHealthListResponse)
async def list_events_health(
    session: SessionDep,
    slug: str,
    ids: Annotated[list[uuid.UUID], Query(min_length=1, max_length=MAX_HEALTH_IDS)],
) -> EventHealthListResponse:
    """Health of up to 150 events; ids outside the scored population are omitted."""
    return await event_health_service.list_events_health(session, slug, ids)


@router.get("/health/event-types", response_model=EventTypeHealthListResponse)
async def list_event_types_health(session: SessionDep, slug: str) -> EventTypeHealthListResponse:
    """Per event type: mean score, grade counts, component averages, worst events."""
    items = await event_health_service.event_type_health(session, slug)
    return EventTypeHealthListResponse(items=items)


@router.get("/health", response_model=ProjectHealthResponse)
async def get_project_health(
    session: SessionDep,
    slug: str,
    trend_days: int = Query(30, ge=1, le=365),
) -> ProjectHealthResponse:
    """Plan health: mean score, distribution, worst five and the daily trend."""
    return await event_health_service.project_health(session, slug, trend_days)


@router.get("/events/{event_id}/health", response_model=EventHealth)
async def get_event_health(session: SessionDep, slug: str, event_id: uuid.UUID) -> EventHealth:
    """One event's score with its component breakdown. 404 off the main plan."""
    return await event_health_service.get_event_health(session, slug, event_id)
