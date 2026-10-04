from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripl.models.event import Event
from tripl.models.field_definition import FieldDefinition
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue, VariableValueKind
from tripl.schemas.variable import SUMMARY_EVENT_LIMIT, SUMMARY_VALUE_LIMIT, VariableEventRef
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.property_events_service import get_listed_event_counts
from tripl.services.variable_value_drift_service import get_open_drift_counts


def _extend_unique(target: list[str], values: Iterable[str], *, limit: int) -> None:
    """Append the novel entries of *values* to *target*, holding it at *limit*.

    The cap has to survive being entered again, because this runs once per
    CONTEXT row against the one accumulator its variable shares. Checking the
    length only after an append let every re-entry add one more novel value
    before breaking, so a variable whose first context already supplied twenty
    distinct values left a hundred contexts later with 119 of them — against a
    cap the response schema states as hard. The early return is
    what makes the limit a property of the accumulator rather than of one call.
    """
    if len(target) >= limit:
        return
    seen = set(target)
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        target.append(value)
        if len(target) >= limit:
            break


async def attach_variable_summaries(
    session: AsyncSession,
    variables: list[Variable],
) -> None:
    variable_ids = [variable.id for variable in variables]
    if not variable_ids:
        return

    # Joined to Event so the row's event-name preview ships with the list
    # instead of costing the client one /values request per variable.
    rows = await session.execute(
        select(VariableValue, Event.name)
        .join(Event, VariableValue.event_id == Event.id)
        .where(VariableValue.variable_id.in_(variable_ids))
    )
    contexts = rows.all()
    event_ids_by_variable: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    event_names_by_variable: dict[uuid.UUID, set[str]] = defaultdict(set)
    event_refs_by_variable: dict[uuid.UUID, set[tuple[str, uuid.UUID]]] = defaultdict(set)
    context_counts: dict[uuid.UUID, int] = defaultdict(int)
    low_counts: dict[uuid.UUID, int] = defaultdict(int)
    high_counts: dict[uuid.UUID, int] = defaultdict(int)
    sample_values: dict[uuid.UUID, list[str]] = defaultdict(list)

    for context, event_name in contexts:
        event_ids_by_variable[context.variable_id].add(context.event_id)
        event_names_by_variable[context.variable_id].add(event_name)
        event_refs_by_variable[context.variable_id].add((event_name, context.event_id))
        context_counts[context.variable_id] += 1
        if context.value_kind == VariableValueKind.low.value:
            low_counts[context.variable_id] += 1
        else:
            high_counts[context.variable_id] += 1
        _extend_unique(
            sample_values[context.variable_id],
            context.values or [],
            limit=SUMMARY_VALUE_LIMIT,
        )

    drift_counts = await get_open_drift_counts(session, variable_ids)
    listed_counts = await get_listed_event_counts(session, variables)

    # Where ``excluded_from_scans`` bites on this row, and where it must not.
    #
    # A count of FACT reports what is stored: context_count, the context-kind
    # split, sample_values, event_names and event_count all stay true for an
    # excluded variable, because the rows ARE there. Exclusion stopped being a
    # delete, so zeroing them would print absence as zero — the exact confusion
    # this branch removed everywhere else, and the one that makes an operator
    # believe a variable was never observed when its whole history is one click
    # of Restore away.
    #
    # A count of WORK reports what somebody still has to act on, and there is no
    # work on a variable taken out of scanning: nothing will refresh or reopen
    # its drifts, and ``worker.tasks.metrics.signals`` no longer raises alerts
    # for them, so a badge here would send the operator to a queue with nothing
    # actionable in it. The drift rows survive and the drift LIST still shows
    # them; only the "needs attention" count stands down.
    for variable in variables:
        variable.event_count = len(event_ids_by_variable.get(variable.id, set()))  # type: ignore[attr-defined]
        variable.context_count = context_counts.get(variable.id, 0)  # type: ignore[attr-defined]
        # The event property lists (F23), apart from what scans observed.
        listed, required = listed_counts.get(variable.id, (0, 0))
        variable.listed_event_count = listed  # type: ignore[attr-defined]
        variable.required_event_count = required  # type: ignore[attr-defined]
        variable.low_context_count = low_counts.get(variable.id, 0)  # type: ignore[attr-defined]
        variable.high_context_count = high_counts.get(variable.id, 0)  # type: ignore[attr-defined]
        variable.sample_values = sample_values.get(variable.id, [])  # type: ignore[attr-defined]
        variable.open_drift_count = (  # type: ignore[attr-defined]
            0 if variable.excluded_from_scans else drift_counts.get(variable.id, 0)
        )
        event_names = sorted(event_names_by_variable.get(variable.id, set()))
        variable.event_names = event_names[:SUMMARY_EVENT_LIMIT]  # type: ignore[attr-defined]
        # Two events can share a name (one per event type), so the refs are
        # keyed by id; sorted on (name, id) so the order is stable (AU-29).
        variable.event_refs = [  # type: ignore[attr-defined]
            VariableEventRef(id=event_id, name=name)
            for name, event_id in sorted(
                event_refs_by_variable.get(variable.id, set()),
                key=lambda ref: (ref[0], str(ref[1])),
            )[:SUMMARY_EVENT_LIMIT]
        ]


async def list_variable_values(
    session: AsyncSession,
    slug: str,
    variable_id: uuid.UUID,
    branch_id: uuid.UUID | None = None,
) -> list[VariableValue]:
    project_id = await resolve_project_id(session, slug)
    branch_id = await resolve_branch_id(session, project_id, branch_id)
    variable = await session.scalar(
        select(Variable).where(
            Variable.id == variable_id,
            Variable.project_id == project_id,
            Variable.branch_id == branch_id,
        )
    )
    if variable is None:
        raise HTTPException(status_code=404, detail="Property not found")

    rows = await session.execute(
        select(VariableValue)
        .join(Event, VariableValue.event_id == Event.id)
        .join(FieldDefinition, VariableValue.field_definition_id == FieldDefinition.id)
        .where(
            VariableValue.project_id == project_id,
            VariableValue.branch_id == branch_id,
            VariableValue.variable_id == variable_id,
        )
        .options(
            selectinload(VariableValue.variable),
            selectinload(VariableValue.event),
            selectinload(VariableValue.field_definition),
        )
        .order_by(Event.name.asc(), FieldDefinition.order.asc(), FieldDefinition.name.asc())
    )
    return list(rows.scalars().all())


async def attach_event_field_variable_values(
    session: AsyncSession,
    events: list[Event],
) -> None:
    event_ids = [event.id for event in events]
    if not event_ids:
        return

    rows = await session.execute(
        select(VariableValue)
        .where(VariableValue.event_id.in_(event_ids))
        .options(selectinload(VariableValue.variable))
    )
    contexts_by_field: dict[tuple[uuid.UUID, uuid.UUID], list[VariableValue]] = defaultdict(list)
    for context in rows.scalars().all():
        # Stamped onto the row because the popover renders CONTEXTS, and the flag
        # it needs lives one hop away on the Variable — a hop Pydantic's
        # ``from_attributes`` will not take on its own. The eager load above is
        # already paid for ``variable_name``, so this costs no extra query.
        context.excluded_from_scans = context.variable.excluded_from_scans  # type: ignore[attr-defined]
        contexts_by_field[(context.event_id, context.field_definition_id)].append(context)

    for event in events:
        for field_value in event.field_values:
            field_value.variable_values = contexts_by_field.get(  # type: ignore[attr-defined]
                (event.id, field_value.field_definition_id),
                [],
            )
