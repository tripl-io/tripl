"""One property across the events that carry it (F23.8, #306).

The Properties catalog page reads the property's events from here, and its bulk
edit writes the same ``variable_event_value_overrides`` entries the single
``upsert_event_override`` does, with the same patch semantics, on the branch
the request names. Kept apart from ``variable_service`` so the per-event
writes stay one call each there and the many-event variants live together here.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.property_drift import effective_threshold
from tripl.models.event import Event
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import (
    VariableEventValueOverride,
    copy_override_values,
)
from tripl.models.variable_value import VariableValue
from tripl.schemas.property_events import (
    PropertyEventResponse,
    PropertyEventsBulkDelete,
    PropertyEventsBulkResult,
    PropertyEventsBulkUpsert,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id


async def _get_variable(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    variable_id: uuid.UUID,
) -> Variable:
    variable = await session.scalar(
        select(Variable).where(
            Variable.id == variable_id,
            Variable.project_id == project_id,
            Variable.branch_id == branch_id,
        )
    )
    if variable is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return variable


async def list_property_events(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> list[PropertyEventResponse]:
    """Every event whose property list carries the variable, by event name.

    Each row has the entry (required, override) and what the last scan measured
    (presence against the event's own threshold), the same figures
    ``GET /events/{id}/properties`` gives from the event's side.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variable = await _get_variable(session, project_id, branch_id, variable_id)
    rows = (
        await session.execute(
            select(VariableEventValueOverride, Event)
            .join(Event, Event.id == VariableEventValueOverride.event_id)
            .where(
                VariableEventValueOverride.branch_id == branch_id,
                VariableEventValueOverride.variable_id == variable_id,
            )
            .order_by(Event.name, Event.id)
        )
    ).all()
    presence: dict[uuid.UUID, float] = {}
    for event_id, rate in await session.execute(
        select(VariableValue.event_id, func.max(VariableValue.presence_rate))
        .where(
            VariableValue.branch_id == branch_id,
            VariableValue.variable_id == variable_id,
            VariableValue.presence_rate.is_not(None),
        )
        .group_by(VariableValue.event_id)
    ):
        presence[event_id] = rate
    global_values = list(variable.allowed_values or [])
    return [
        PropertyEventResponse(
            event_id=event.id,
            event_name=event.name,
            event_type_id=event.event_type_id,
            status=str(event.status),
            required=entry.required,
            values=copy_override_values(entry.values),
            effective_values=list(entry.values) if entry.values is not None else global_values,
            presence_rate=presence.get(event.id),
            required_presence_threshold=event.required_presence_threshold,
            suggested_required=(
                None
                if event.id not in presence
                else presence[event.id] >= effective_threshold(event.required_presence_threshold)
            ),
        )
        for entry, event in rows
    ]


async def get_listed_event_counts(
    session: AsyncSession,
    variables: list[Variable],
) -> dict[uuid.UUID, tuple[int, int]]:
    """How many events list each variable, and on how many it is required.

    For the Properties list: ``(listed, required)`` per variable id, read on
    each variable's own branch (a branch copies entries with its variables).
    """
    if not variables:
        return {}
    branch_of = {variable.id: variable.branch_id for variable in variables}
    rows = await session.execute(
        select(
            VariableEventValueOverride.variable_id,
            VariableEventValueOverride.branch_id,
            func.count(VariableEventValueOverride.id),
            func.count(VariableEventValueOverride.id).filter(
                VariableEventValueOverride.required.is_(True)
            ),
        )
        .where(VariableEventValueOverride.variable_id.in_(list(branch_of)))
        .group_by(VariableEventValueOverride.variable_id, VariableEventValueOverride.branch_id)
    )
    return {
        variable_id: (listed, required)
        for variable_id, branch_id, listed, required in rows
        if branch_of.get(variable_id) == branch_id
    }


async def _load_events(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    event_ids: list[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """The (deduplicated) events by id with their names; 404 when one is not on the branch.

    All or nothing, like ``event_service.bulk_delete_events``: an id from another
    branch or project would otherwise be skipped silently and the result would
    count edits that never happened.
    """
    wanted = set(event_ids)
    found = {
        event_id: name
        for event_id, name in await session.execute(
            select(Event.id, Event.name).where(
                Event.project_id == project_id,
                Event.branch_id == branch_id,
                Event.id.in_(wanted),
            )
        )
    }
    if len(found) != len(wanted):
        raise HTTPException(status_code=404, detail="Event not found")
    return found


async def _entries_by_event(
    session: AsyncSession,
    branch_id: uuid.UUID | None,
    variable_id: uuid.UUID,
    event_ids: set[uuid.UUID],
) -> dict[uuid.UUID, VariableEventValueOverride]:
    result = await session.execute(
        select(VariableEventValueOverride).where(
            VariableEventValueOverride.branch_id == branch_id,
            VariableEventValueOverride.variable_id == variable_id,
            VariableEventValueOverride.event_id.in_(event_ids),
        )
    )
    return {entry.event_id: entry for entry in result.scalars().all()}


async def bulk_upsert_property_events(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    data: PropertyEventsBulkUpsert,
    branch_id: uuid.UUID | None = None,
) -> tuple[str, list[tuple[uuid.UUID, str]], PropertyEventsBulkResult]:
    """Apply one patch to the property's entry on every listed event.

    Returns the property's name, the (id, name) of each event touched (for the
    audit row) and the counts. One commit: a 404 leaves every entry as it was.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variable = await _get_variable(session, project_id, branch_id, variable_id)
    events = await _load_events(session, project_id, branch_id, data.event_ids)
    existing = await _entries_by_event(session, branch_id, variable_id, set(events))
    patch = data.model_dump(exclude_unset=True, exclude={"event_ids"})
    created = 0
    for event_id in events:
        entry = existing.get(event_id)
        if entry is None:
            entry = VariableEventValueOverride(
                project_id=project_id,
                branch_id=branch_id,
                variable_id=variable_id,
                event_id=event_id,
                values=None,
                required=False,
            )
            session.add(entry)
            created += 1
        if "values" in patch:
            entry.values = copy_override_values(patch["values"])
        if "required" in patch:
            entry.required = patch["required"]
    await session.commit()
    touched = sorted(events.items(), key=lambda item: item[1])
    return (
        variable.name,
        touched,
        PropertyEventsBulkResult(created=created, updated=len(events) - created),
    )


async def bulk_delete_property_events(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    data: PropertyEventsBulkDelete,
    branch_id: uuid.UUID | None = None,
) -> tuple[str, list[tuple[uuid.UUID, str]], PropertyEventsBulkResult]:
    """Take the property off every listed event that carries it.

    An event that does not carry it is not an error — the request's end state
    already holds — and is left out of the returned list, so the audit row names
    only entries that were deleted.
    """
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variable = await _get_variable(session, project_id, branch_id, variable_id)
    events = await _load_events(session, project_id, branch_id, data.event_ids)
    existing = await _entries_by_event(session, branch_id, variable_id, set(events))
    for entry in existing.values():
        await session.delete(entry)
    await session.commit()
    removed = sorted(((event_id, events[event_id]) for event_id in existing), key=lambda i: i[1])
    return (
        variable.name,
        removed,
        PropertyEventsBulkResult(created=0, updated=0, removed=len(removed)),
    )


def property_event_ids(
    project_id: uuid.UUID, branch_id: uuid.UUID | None, ref: str
) -> Select[tuple[uuid.UUID]]:
    """Ids of the events whose property list carries the property ``ref`` names.

    ``ref`` is the variable's id or its name; both are read on the branch, since
    a branch copies variables under new ids and the name is what survives the
    copy. A ref that names nothing matches no event.
    """
    try:
        by_id = Variable.id == uuid.UUID(ref)
    except ValueError:
        by_id = None
    match = Variable.name == ref if by_id is None else or_(by_id, Variable.name == ref)
    variables = select(Variable.id).where(
        Variable.project_id == project_id, Variable.branch_id == branch_id, match
    )
    return (
        select(VariableEventValueOverride.event_id)
        .where(
            VariableEventValueOverride.branch_id == branch_id,
            VariableEventValueOverride.variable_id.in_(variables),
        )
        .correlate(None)
    )
