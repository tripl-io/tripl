from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.middleware.org_context import require_org_id
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.schemas.ai import AiAskResponse, AiAskSource, AiDescribeResponse, AiFieldSuggestion
from tripl.services import app_settings_service, llm_service, search_service
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id

logger = logging.getLogger(__name__)


# Structured output for the describe feature, so the answer is the object the
# parser below expects rather than prose that asks politely for JSON. Servers
# that do not support it get the request retried without it, and the parser
# still tolerates fenced or plain-text answers from them.
_DESCRIBE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "event_description",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["description", "field_suggestions"],
            "properties": {
                "description": {"type": "string"},
                "field_suggestions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["field_name", "description"],
                        "properties": {
                            "field_name": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove opening fence (```json or ```)
        lines = lines[1:]
        # Remove closing fence if present
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_describe_response(raw: str) -> AiDescribeResponse:
    cleaned = _strip_markdown_fences(raw)
    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("AI describe response is not an object")
        description = data.get("description", "") or ""
        raw_suggestions = data.get("field_suggestions", []) or []
        field_suggestions = [
            AiFieldSuggestion(
                field_name=s.get("field_name", ""),
                description=s.get("description", ""),
            )
            for s in raw_suggestions
            if isinstance(s, dict)
        ]
        return AiDescribeResponse(description=description, field_suggestions=field_suggestions)
    except json.JSONDecodeError, TypeError, ValueError:
        logger.warning("AI describe response was not valid JSON; using raw text as description")
        return AiDescribeResponse(description=raw.strip(), field_suggestions=[])


async def suggest_event_description(
    session: AsyncSession,
    slug: str,
    event_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> AiDescribeResponse:
    project_id = await resolve_project_id(session, slug)
    resolved_branch_id = await resolve_branch_id(session, project_id, branch_id)

    event = await session.scalar(
        select(Event).where(
            Event.id == event_id,
            Event.project_id == project_id,
            Event.branch_id == resolved_branch_id,
        )
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    event_type = await session.scalar(
        select(EventType).where(
            EventType.id == event.event_type_id,
            EventType.project_id == project_id,
            EventType.branch_id == resolved_branch_id,
        )
    )

    field_defs: list[FieldDefinition] = []
    if event_type is not None:
        result = await session.execute(
            select(FieldDefinition)
            .where(FieldDefinition.event_type_id == event_type.id)
            .order_by(FieldDefinition.order)
        )
        field_defs = list(result.scalars().all())

    # Build user prompt
    lines: list[str] = [
        f"Event name: {event.name}",
        f"Source name: {event.source_name or '(none)'}",
        f"Current description: {event.description or '(empty)'}",
    ]
    if event_type is not None:
        lines.append(f"Event type: {event_type.name} ({event_type.display_name})")
        if event_type.description:
            lines.append(f"Event type description: {event_type.description}")
    if field_defs:
        lines.append("Fields:")
        for fd in field_defs:
            opts = f", options: {', '.join(fd.enum_options)}" if fd.enum_options else ""
            desc = f", current description: {fd.description}" if fd.description else ""
            lines.append(
                f"  - {fd.name} (display: {fd.display_name}, type: {fd.field_type}{opts}{desc})"
            )

    user_prompt = "\n".join(lines)
    config = await app_settings_service.get_ai_config(session, org_id=require_org_id())
    raw = await asyncio.to_thread(
        llm_service.complete,
        config.describe_system_prompt,
        user_prompt,
        response_format=_DESCRIBE_RESPONSE_FORMAT,
        config=config,
    )
    if raw is None:
        raise HTTPException(
            status_code=502,
            detail=(
                "AI provider request failed. Check the model and base URL in "
                "service AI settings, and see backend logs for the provider error."
            ),
        )
    return _parse_describe_response(raw)


async def suggest_event_type_descriptions(
    session: AsyncSession,
    slug: str,
    event_type_id: uuid.UUID,
    branch_id: uuid.UUID | None,
) -> AiDescribeResponse:
    project_id = await resolve_project_id(session, slug)
    resolved_branch_id = await resolve_branch_id(session, project_id, branch_id)

    event_type = await session.scalar(
        select(EventType).where(
            EventType.id == event_type_id,
            EventType.project_id == project_id,
            EventType.branch_id == resolved_branch_id,
        )
    )
    if event_type is None:
        raise HTTPException(status_code=404, detail="Event type not found")

    result = await session.execute(
        select(FieldDefinition)
        .where(FieldDefinition.event_type_id == event_type.id)
        .order_by(FieldDefinition.order)
    )
    field_defs = list(result.scalars().all())

    lines: list[str] = [
        f"Event type name: {event_type.name}",
        f"Event type display name: {event_type.display_name}",
        f"Current description: {event_type.description or '(empty)'}",
    ]
    if field_defs:
        lines.append("Fields:")
        for fd in field_defs:
            opts = f", options: {', '.join(fd.enum_options)}" if fd.enum_options else ""
            desc = f", current description: {fd.description}" if fd.description else ""
            lines.append(
                f"  - {fd.name} (display: {fd.display_name}, type: {fd.field_type}{opts}{desc})"
            )

    user_prompt = "\n".join(lines)
    config = await app_settings_service.get_ai_config(session, org_id=require_org_id())
    raw = await asyncio.to_thread(
        llm_service.complete,
        config.describe_system_prompt,
        user_prompt,
        response_format=_DESCRIBE_RESPONSE_FORMAT,
        config=config,
    )
    if raw is None:
        raise HTTPException(
            status_code=502,
            detail=(
                "AI provider request failed. Check the model and base URL in "
                "service AI settings, and see backend logs for the provider error."
            ),
        )
    return _parse_describe_response(raw)


async def ask_plan(
    session: AsyncSession,
    slug: str,
    question: str,
    branch_id: uuid.UUID | None,
) -> AiAskResponse:
    search_resp = await search_service.search_project(
        session,
        slug,
        question,
        branch_id=branch_id,
        limit=12,
    )

    context_lines: list[str] = []
    for idx, item in enumerate(search_resp.items, start=1):
        parts = [f"[{idx}] {item.title}"]
        if item.subtitle:
            parts.append(f"  Subtitle: {item.subtitle}")
        parts.append(f"  Type: {item.entity_type}")
        if item.description:
            parts.append(f"  Description: {item.description}")
        if item.snippet:
            parts.append(f"  Snippet: {item.snippet}")
        if item.variable_values:
            top_vals = item.variable_values[:3]
            for vv in top_vals:
                val_str = ", ".join(vv.values[:5]) if vv.values else ""
                parts.append(f"  Variable [{vv.variable_name}]: {val_str}")
        context_lines.append("\n".join(parts))

    context_text = "\n\n".join(context_lines)
    user_prompt = f"Question: {question}\n\nContext:\n{context_text}"

    config = await app_settings_service.get_ai_config(session, org_id=require_org_id())
    raw = await asyncio.to_thread(
        llm_service.complete, config.ask_system_prompt, user_prompt, config=config
    )
    answer = raw.strip() if raw else "The AI service is unavailable or returned no answer."

    sources = [
        AiAskSource(
            title=item.title,
            entity_type=item.entity_type,
            route_path=item.route_path,
        )
        for item in search_resp.items
    ]

    return AiAskResponse(
        answer=answer,
        sources=sources,
        semantic_used=search_resp.semantic_used,
    )
