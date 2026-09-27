"""Incident summary routes (F14, #267). Thin: the service does the work."""

import uuid

from fastapi import APIRouter

from tripl.api.deps import EditorUserDep, SessionDep, WriteUserDep
from tripl.schemas.incident_summary import IncidentSummaryResponse
from tripl.services import incident_summary_service

router = APIRouter(prefix="/projects/{slug}", tags=["alerting"])


@router.get("/alert-inbox/{correlation_group_id}/summary", response_model=IncidentSummaryResponse)
async def get_incident_summary(
    session: SessionDep, slug: str, correlation_group_id: uuid.UUID
) -> IncidentSummaryResponse:
    """The cached summary and whether it is current. Never calls the LLM."""
    return await incident_summary_service.get_summary(session, slug, correlation_group_id)


@router.post("/alert-inbox/{correlation_group_id}/summary", response_model=IncidentSummaryResponse)
async def ensure_incident_summary(
    session: SessionDep, slug: str, correlation_group_id: uuid.UUID, current_user: WriteUserDep
) -> IncidentSummaryResponse:
    """The summary for the current facts: cached when unchanged, else generated.

    Any member may call it, but not a ``read`` API key: generating writes a row
    and spends the provider budget, so a read key only gets the GET."""
    return await incident_summary_service.ensure_summary(
        session, slug, correlation_group_id, current_user, force=False
    )


@router.post(
    "/alert-inbox/{correlation_group_id}/summary/regenerate",
    response_model=IncidentSummaryResponse,
)
async def regenerate_incident_summary(
    session: SessionDep, slug: str, correlation_group_id: uuid.UUID, current_user: EditorUserDep
) -> IncidentSummaryResponse:
    """Generate the summary again, whatever the cache holds. Editors only."""
    return await incident_summary_service.ensure_summary(
        session, slug, correlation_group_id, current_user, force=True
    )
