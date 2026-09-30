"""Observed value contexts for rows "Update from main" creates (PL-8).

Split out of ``_plan_branch_update_apply``. A ``VariableValue`` (variable,
event, field) is what a scan observed; it is not in the snapshot, so a row
rebuilt from the snapshot would come without main's. A branch opened now
copies them (``plan_branch_service._copy_main_into_new_branch``); so does the
update, for every row it creates.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.variable import Variable
from tripl.models.variable_value import VariableValue


async def copy_value_contexts(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID,
    created: dict[str, list[tuple[Any, dict[str, Any]]]],
    find_event: Callable[[uuid.UUID], Awaitable[Event | None]],
) -> None:
    """Main's observed ``VariableValue`` contexts for the rows this update created.

    A branch opened now would copy them (``_copy_main_into_new_branch``);
    the snapshot carries none, so a row rebuilt from it would otherwise
    have no observed values on the branch. A context is copied when its
    variable, event and field all have a branch row: the event by origin,
    the variable by source name, else name, the field by (type, name).
    """
    event_rows = {uuid.UUID(str(item["id"])): row.id for row, item in created["event"]}
    variable_rows = {uuid.UUID(str(item["id"])): row.id for row, item in created["variable"]}
    if not event_rows and not variable_rows:
        return
    main_branch_id = (
        await session.scalar(select(Event.branch_id).where(Event.id.in_(list(event_rows))).limit(1))
        if event_rows
        else await session.scalar(
            select(Variable.branch_id).where(Variable.id.in_(list(variable_rows))).limit(1)
        )
    )
    if main_branch_id is None:
        return
    contexts = list(
        (
            await session.execute(
                select(VariableValue).where(
                    VariableValue.branch_id == main_branch_id,
                    or_(
                        VariableValue.event_id.in_(list(event_rows)),
                        VariableValue.variable_id.in_(list(variable_rows)),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not contexts:
        return
    branch_variables = list(
        (await session.execute(select(Variable).where(Variable.branch_id == branch_id)))
        .scalars()
        .all()
    )
    by_source = {v.source_name: v.id for v in branch_variables if v.source_name}
    by_name = {v.name: v.id for v in branch_variables}
    branch_fields = {
        (type_name, field_name): field_id
        for field_id, type_name, field_name in (
            await session.execute(
                select(FieldDefinition.id, EventType.name, FieldDefinition.name)
                .join(EventType, EventType.id == FieldDefinition.event_type_id)
                .where(EventType.branch_id == branch_id)
            )
        ).all()
    }
    existing = {
        (row.variable_id, row.event_id, row.field_definition_id)
        for row in (
            await session.execute(select(VariableValue).where(VariableValue.branch_id == branch_id))
        )
        .scalars()
        .all()
    }
    for context in contexts:
        variable_id = await _branch_variable_for(
            session, context.variable_id, variable_rows, by_source, by_name
        )
        event_id = event_rows.get(context.event_id)
        if event_id is None:
            target = await find_event(context.event_id)
            event_id = target.id if target is not None else None
        field_id = await _branch_field_for(session, context.field_definition_id, branch_fields)
        if variable_id is None or event_id is None or field_id is None:
            continue
        if (variable_id, event_id, field_id) in existing:
            continue
        existing.add((variable_id, event_id, field_id))
        session.add(
            VariableValue(
                id=uuid.uuid4(),
                project_id=project_id,
                branch_id=branch_id,
                variable_id=variable_id,
                event_id=event_id,
                field_definition_id=field_id,
                source_column=context.source_column,
                value_kind=context.value_kind,
                observed_count=context.observed_count,
                values=list(context.values or []),
                presence_rate=context.presence_rate,
            )
        )
    await session.flush()


async def _branch_variable_for(
    session: AsyncSession,
    main_variable_id: uuid.UUID,
    created: dict[uuid.UUID, uuid.UUID],
    by_source: dict[str, uuid.UUID],
    by_name: dict[str, uuid.UUID],
) -> uuid.UUID | None:
    if main_variable_id in created:
        return created[main_variable_id]
    main_variable = await session.get(Variable, main_variable_id)
    if main_variable is None:
        return None
    if main_variable.source_name:
        return by_source.get(main_variable.source_name)
    return by_name.get(main_variable.name)


async def _branch_field_for(
    session: AsyncSession, main_field_id: uuid.UUID, branch_fields: dict[tuple[str, str], uuid.UUID]
) -> uuid.UUID | None:
    found = (
        await session.execute(
            select(EventType.name, FieldDefinition.name)
            .join(EventType, EventType.id == FieldDefinition.event_type_id)
            .where(FieldDefinition.id == main_field_id)
        )
    ).first()
    if found is None:
        return None
    return branch_fields.get((found[0], found[1]))
