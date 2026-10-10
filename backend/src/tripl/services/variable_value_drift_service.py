"""Read/action helpers for VariableValueDrift records.

Mirrors ``schema_drift_service``: the same read-time retention and "active"
rule (``core.drift_activity``), and an accept action that mutates the plan (the
variable's documented values) rather than just resolving the row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.drift_activity import active_drift_clauses, retention_cutoff
from tripl.models.user import User
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.schemas.variable_value_drift import (
    VariableValueDriftActionRequest,
    VariableValueDriftListResponse,
    VariableValueDriftResponse,
)
from tripl.services.project_lookup import resolve_project_id
from tripl.services.search_service import reindex_project_branch


async def list_value_drifts(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
) -> VariableValueDriftListResponse:
    project_id = await resolve_project_id(session, slug)
    cutoff = retention_cutoff()
    query = (
        select(VariableValueDrift)
        .where(
            VariableValueDrift.project_id == project_id,
            VariableValueDrift.detected_at >= cutoff,
        )
        .order_by(VariableValueDrift.detected_at.desc())
    )
    if variable_id is not None:
        query = query.where(VariableValueDrift.variable_id == variable_id)
    if event_id is not None:
        query = query.where(VariableValueDrift.event_id == event_id)
    drifts = list((await session.execute(query)).scalars().all())
    items = [VariableValueDriftResponse.model_validate(drift) for drift in drifts]
    return VariableValueDriftListResponse(items=items, total=len(items))


def _merge_values(base: list[str], extra: list[str]) -> list[str]:
    merged = list(base)
    seen = set(merged)
    for value in extra:
        if value not in seen:
            seen.add(value)
            merged.append(value)
    return merged


async def _apply_acceptance_to_plan(
    session: AsyncSession,
    *,
    drift: VariableValueDrift,
    variable: Variable,
    scope: str,
) -> None:
    novel = list(drift.observed_values or [])
    if not novel:
        return
    if scope == "event":
        override = await session.scalar(
            select(VariableEventValueOverride).where(
                VariableEventValueOverride.variable_id == drift.variable_id,
                VariableEventValueOverride.event_id == drift.event_id,
            )
        )
        if override is None:
            # Seed from the effective documented list so the new override does
            # not silently drop the global values it replaces.
            session.add(
                VariableEventValueOverride(
                    project_id=drift.project_id,
                    branch_id=variable.branch_id,
                    variable_id=drift.variable_id,
                    event_id=drift.event_id,
                    values=_merge_values(list(variable.allowed_values or []), novel),
                )
            )
        elif override.values is None:
            # A property entry with no override of its own: the list it starts
            # from is the global one, as for a new row.
            override.values = _merge_values(list(variable.allowed_values or []), novel)
        else:
            override.values = _merge_values(list(override.values), novel)
        return
    variable.allowed_values = _merge_values(list(variable.allowed_values or []), novel)


async def apply_drift_action(
    session: AsyncSession,
    slug: str,
    drift_id: uuid.UUID,
    data: VariableValueDriftActionRequest,
    user: User,
) -> VariableValueDriftResponse:
    project_id = await resolve_project_id(session, slug)
    row = (
        await session.execute(
            select(VariableValueDrift, Variable)
            .join(Variable, Variable.id == VariableValueDrift.variable_id)
            .where(
                VariableValueDrift.id == drift_id,
                VariableValueDrift.project_id == project_id,
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Value drift not found")

    drift, variable = row
    now = datetime.now(UTC)
    plan_changed = False
    if data.action == "accept":
        await _apply_acceptance_to_plan(session, drift=drift, variable=variable, scope=data.scope)
        plan_changed = True
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
    elif data.action == "reopen":
        drift.status = "open"
        drift.resolved_at = None
        drift.resolved_by = None
        drift.snoozed_until = None
    else:
        raise HTTPException(status_code=422, detail="Unsupported value drift action")

    if data.action == "reopen":
        drift.resolution_note = None
    elif "note" in data.model_fields_set:
        drift.resolution_note = data.note
    await session.commit()
    await session.refresh(drift)

    if plan_changed:
        await reindex_project_branch(
            session,
            project_id=project_id,
            branch_id=variable.branch_id,
            slug=slug,
        )
    return VariableValueDriftResponse.model_validate(drift)


async def get_open_drift_counts(
    session: AsyncSession,
    variable_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    if not variable_ids:
        return {}
    now = datetime.now(UTC)
    cutoff = retention_cutoff(now)
    rows = (
        await session.execute(
            select(VariableValueDrift.variable_id, func.count(VariableValueDrift.id))
            .where(
                VariableValueDrift.variable_id.in_(variable_ids),
                VariableValueDrift.detected_at >= cutoff,
                *active_drift_clauses(VariableValueDrift, now),
            )
            .group_by(VariableValueDrift.variable_id)
        )
    ).all()
    return {variable_id: count for variable_id, count in rows}
