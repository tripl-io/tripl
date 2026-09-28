from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import BranchIdDep, EditorUserDep, SessionDep
from tripl.middleware.org_context import require_org_id
from tripl.schemas.ai import (
    AiAskRequest,
    AiAskResponse,
    AiDescribeEventRequest,
    AiDescribeEventTypeRequest,
    AiDescribeResponse,
    AiStatusResponse,
)
from tripl.services import ai_service, app_settings_service, llm_service

router = APIRouter(prefix="/projects/{slug}/ai", tags=["ai"])


async def _require_ai_enabled(session: AsyncSession) -> None:
    config = await app_settings_service.get_ai_config(session, org_id=require_org_id())
    if not llm_service.is_enabled(config):
        raise HTTPException(
            status_code=503,
            detail=(
                "AI features are disabled for this organization. Enable them in "
                "Settings -> Organization -> AI (or ask the operator), and configure an API key."
            ),
        )


@router.get("/status", response_model=AiStatusResponse)
async def ai_status(session: SessionDep, slug: str) -> AiStatusResponse:
    config = await app_settings_service.get_ai_config(session, org_id=require_org_id())
    return AiStatusResponse(enabled=llm_service.is_enabled(config))


@router.post("/describe-event", response_model=AiDescribeResponse)
async def describe_event(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    payload: AiDescribeEventRequest,
    _current_user: EditorUserDep,
) -> AiDescribeResponse:
    await _require_ai_enabled(session)
    return await ai_service.suggest_event_description(session, slug, payload.event_id, branch_id)


@router.post("/describe-event-type", response_model=AiDescribeResponse)
async def describe_event_type(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    payload: AiDescribeEventTypeRequest,
    _current_user: EditorUserDep,
) -> AiDescribeResponse:
    await _require_ai_enabled(session)
    return await ai_service.suggest_event_type_descriptions(
        session,
        slug,
        payload.event_type_id,
        branch_id,
    )


@router.post("/ask", response_model=AiAskResponse)
async def ask_plan(
    session: SessionDep,
    slug: str,
    branch_id: BranchIdDep,
    payload: AiAskRequest,
) -> AiAskResponse:
    await _require_ai_enabled(session)
    return await ai_service.ask_plan(session, slug, payload.question, branch_id)
