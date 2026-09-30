"""Read and triage ``PropertyDrift`` rows (F23, #306).

Mirrors ``variable_value_drift_service``: the same 30-day read-time retention
and the same notion of active, and an ``accept`` that changes the plan on the
variable's branch (main: property drift, like value drift, is only detected
against main).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from tripl.core.property_schema import PropertySchemaError, check_schema_matches_type
from tripl.models.property_drift import PropertyDrift, PropertyDriftKind
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.schemas.property_drift import (
    PropertyDriftActionRequest,
    PropertyDriftListResponse,
    PropertyDriftResponse,
)
from tripl.services.project_lookup import resolve_project_id
from tripl.services.search_service import reindex_project_branch
from tripl.services.variable_value_drift_service import retention_cutoff


async def list_property_drifts(
    session: AsyncSession,
    slug: str,
    *,
    variable_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    kind: PropertyDriftKind | None = None,
    active_only: bool = False,
) -> PropertyDriftListResponse:
    project_id = await resolve_project_id(session, slug)
    now = datetime.now(UTC)
    query = (
        select(PropertyDrift)
        .where(
            PropertyDrift.project_id == project_id,
            PropertyDrift.detected_at >= retention_cutoff(now),
        )
        .order_by(PropertyDrift.detected_at.desc(), PropertyDrift.id)
    )
    if variable_id is not None:
        query = query.where(PropertyDrift.variable_id == variable_id)
    if event_id is not None:
        query = query.where(PropertyDrift.event_id == event_id)
    if kind is not None:
        query = query.where(PropertyDrift.kind == kind.value)
    if active_only:
        query = query.where(*_active(now))
    drifts = list((await session.execute(query)).scalars().all())
    items = [PropertyDriftResponse.model_validate(drift) for drift in drifts]
    return PropertyDriftListResponse(items=items, total=len(items))


def _active(now: datetime) -> list[ColumnElement[bool]]:
    """Active as value drift judges it: open, or snoozed until a past instant."""
    return [
        PropertyDrift.status.in_({"open", "snoozed"}),
        (PropertyDrift.status != "snoozed")
        | (PropertyDrift.snoozed_until.is_(None))
        | (PropertyDrift.snoozed_until <= now),
    ]


async def _accept(session: AsyncSession, drift: PropertyDrift, variable: Variable) -> None:
    kind = PropertyDriftKind(drift.kind)
    if kind is PropertyDriftKind.type_change:
        observed_type = str(drift.detail.get("observed_type") or "")
        observed_schema = drift.detail.get("observed_schema")
        try:
            check_schema_matches_type(observed_type, observed_schema)
        except PropertySchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        variable.variable_type = observed_type
        variable.json_schema = observed_schema
        return
    assert drift.event_id is not None  # per-event kinds always carry one
    entry = await session.scalar(
        select(VariableEventValueOverride).where(
            VariableEventValueOverride.variable_id == drift.variable_id,
            VariableEventValueOverride.event_id == drift.event_id,
        )
    )
    if kind is PropertyDriftKind.new_property:
        if entry is None:
            session.add(
                VariableEventValueOverride(
                    project_id=variable.project_id,
                    branch_id=variable.branch_id,
                    variable_id=variable.id,
                    event_id=drift.event_id,
                    values=None,
                    required=False,
                )
            )
        return
    # missing_required: the event does not always carry it, and says so.
    if entry is not None:
        entry.required = False


async def apply_property_drift_action(
    session: AsyncSession,
    slug: str,
    drift_id: uuid.UUID,
    data: PropertyDriftActionRequest,
    user: User,
) -> PropertyDriftResponse:
    project_id = await resolve_project_id(session, slug)
    row = (
        await session.execute(
            select(PropertyDrift, Variable)
            .join(Variable, Variable.id == PropertyDrift.variable_id)
            .where(PropertyDrift.id == drift_id, PropertyDrift.project_id == project_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Property drift not found")
    drift, variable = row
    now = datetime.now(UTC)
    if data.action == "accept":
        await _accept(session, drift, variable)
        drift.status = "accepted"
        drift.resolved_at = now
        drift.resolved_by = user.id
        drift.snoozed_until = None
    elif data.action == "snooze":
        drift.status = "snoozed"
        drift.snoozed_until = data.snoozed_until
        drift.resolved_at = None
        drift.resolved_by = user.id
    elif data.action == "false_positive":
        drift.status = "false_positive"
        drift.resolved_at = now
        drift.resolved_by = user.id
        drift.snoozed_until = None
    else:
        drift.status = "open"
        drift.resolved_at = None
        drift.resolved_by = None
        drift.snoozed_until = None
    if data.action == "reopen":
        drift.resolution_note = None
    elif "note" in data.model_fields_set:
        drift.resolution_note = data.note
    await session.commit()
    await session.refresh(drift)
    if data.action == "accept":
        await reindex_project_branch(
            session, project_id=project_id, branch_id=variable.branch_id, slug=slug
        )
    return PropertyDriftResponse.model_validate(drift)


async def open_property_drift_count(session: AsyncSession, project_id: uuid.UUID) -> int:
    """The project's open property drifts: the shared count (``_open_signals``)."""
    from tripl.services._open_signals import open_property_drift_counts

    counts = await open_property_drift_counts(session, [project_id])
    return counts.get(project_id, 0)
