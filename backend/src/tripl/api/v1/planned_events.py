"""API for planned events (F18): windows in which anomalies are expected, not alerted."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from tripl.api.deps import EditorUserDep, SessionDep
from tripl.models.domain_enums import ChartAnnotationScopeType
from tripl.schemas.planned_event import (
    PlannedEventCreate,
    PlannedEventResponse,
    PlannedEventUpdate,
)
from tripl.schemas.text_filters import FreeTextFilter
from tripl.services import audit_service, planned_event_service
from tripl.services.annotation_scope_names import resolve_scope_names

router = APIRouter(
    prefix="/projects/{slug}/planned-events",
    tags=["planned-events"],
)

OptionalDateTimeQuery = Annotated[datetime | None, Query()]


def _audit_payload(event: PlannedEventResponse) -> dict[str, object]:
    return {
        "starts_at": event.starts_at.isoformat(),
        "ends_at": event.ends_at.isoformat(),
        "direction": event.direction,
        "scope_type": event.scope_type,
        "scope_ref": event.scope_ref,
    }


@router.get("", response_model=list[PlannedEventResponse])
async def list_planned_events(
    session: SessionDep,
    slug: str,
    # Annotated[] for ruff's B008, as on the chart annotations list.
    scope_type: Annotated[ChartAnnotationScopeType | None, Query()] = None,
    scope_ref: Annotated[FreeTextFilter | None, Query()] = None,
    time_from: OptionalDateTimeQuery = None,
    time_to: OptionalDateTimeQuery = None,
) -> list[PlannedEventResponse]:
    rows = await planned_event_service.list_planned_events(
        session,
        slug,
        scope_type=scope_type,
        scope_ref=scope_ref,
        time_from=time_from,
        time_to=time_to,
    )
    names = (
        await resolve_scope_names(
            session, rows[0].project_id, ((row.scope_type, row.scope_ref) for row in rows)
        )
        if rows
        else {}
    )
    return [
        PlannedEventResponse.model_validate(row).model_copy(
            update={"scope_name": names.get((str(row.scope_type), str(row.scope_ref)))}
        )
        for row in rows
    ]


@router.post("", response_model=PlannedEventResponse, status_code=status.HTTP_201_CREATED)
async def create_planned_event(
    session: SessionDep,
    slug: str,
    data: PlannedEventCreate,
    current_user: EditorUserDep,
) -> PlannedEventResponse:
    event = await planned_event_service.create_planned_event(
        session,
        slug,
        label=data.label,
        description=data.description,
        starts_at=data.starts_at,
        ends_at=data.ends_at,
        direction=data.direction,
        scope_type=data.scope_type,
        scope_ref=data.scope_ref,
        user_id=current_user.id,
    )
    response = PlannedEventResponse.model_validate(event)
    await audit_service.record(
        session,
        user=current_user,
        action="planned_event.create",
        target_type="planned_event",
        target_id=event.id,
        target_name=event.label,
        project_slug=slug,
        payload=_audit_payload(response),
    )
    return response


@router.patch("/{planned_event_id}", response_model=PlannedEventResponse)
async def update_planned_event(
    session: SessionDep,
    slug: str,
    planned_event_id: uuid.UUID,
    data: PlannedEventUpdate,
    current_user: EditorUserDep,
) -> PlannedEventResponse:
    event = await planned_event_service.get_planned_event(session, slug, planned_event_id)
    planned_event_service.ensure_editable(event)
    changes = data.model_dump(exclude_unset=True)
    current = {
        "label": event.label,
        "description": event.description,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "direction": event.direction,
        "scope_type": event.scope_type,
        "scope_ref": event.scope_ref,
    }
    # The window and the scope pair are rules over the whole event, so the
    # merged result is validated, not the patch alone.
    try:
        merged = PlannedEventCreate.model_validate({**current, **changes})
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=jsonable_encoder(exc.errors(include_url=False, include_context=False)),
        ) from exc
    applied = {field: getattr(merged, field) for field in changes}
    event = await planned_event_service.update_planned_event(session, event, applied)
    response = PlannedEventResponse.model_validate(event)
    await audit_service.record(
        session,
        user=current_user,
        action="planned_event.update",
        target_type="planned_event",
        target_id=event.id,
        target_name=event.label,
        project_slug=slug,
        payload={**_audit_payload(response), "fields": sorted(changes)},
    )
    return response


@router.delete("/{planned_event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_planned_event(
    session: SessionDep,
    slug: str,
    planned_event_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    event = await planned_event_service.get_planned_event(session, slug, planned_event_id)
    label = event.label
    await planned_event_service.delete_planned_event(session, event)
    await audit_service.record(
        session,
        user=current_user,
        action="planned_event.delete",
        target_type="planned_event",
        target_id=planned_event_id,
        target_name=label,
        project_slug=slug,
    )
