"""Validate a batch of tracking calls against one branch of the plan (GH #261, F08).

The rules are ``core.plan_validation``; this module only loads the plan they
read, and loads it once per request whatever the batch size:

1. event types of the branch, each with its governing naming rule (resolved the
   way the event-type listing resolves it, through the main counterpart for a
   branch copy) and its field definitions;
2. every event of the branch as ``(id, type, name, identity, status)`` columns
   only, never the ORM rows with their eager field/meta/tag collections;
3. the branch's variables and their allowed values.

Then every item is resolved to an event, and ONLY for the events actually hit
are the stored field values and per-event variable overrides loaded, in
chunks. Read-only throughout: nothing is written and no lock is taken.

The pure work (building the identity indexes, resolving and checking every
item) is CPU-bound over up to 5000 items, so it runs in a worker thread and
never blocks the event loop; only the queries stay on it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers._event_generator_variables import VariableIndex
from tripl.core.plan_validation import (
    EventContext,
    Finding,
    PlanEvent,
    PlanEventType,
    PlanField,
    PlanSnapshot,
    Resolution,
    ValidationItem,
    check_item,
    item_status,
    resolve_item,
    scalar_text,
)
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.schemas.plan_validation import (
    PlanValidationFinding,
    PlanValidationItemResult,
    PlanValidationRequest,
    PlanValidationResponse,
    PlanValidationSummary,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.scan_config_lookup import (
    governing_name_format,
    load_governing_scan_configs_by_type,
)

_ID_CHUNK = 1000


async def validate_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    data: PlanValidationRequest,
) -> PlanValidationResponse:
    """One verdict per item, in order, against ``branch_id`` (``None`` = main)."""
    resolved_branch = await resolve_branch_id(session, project_id, branch_id)
    snapshot = await load_plan_snapshot(session, project_id=project_id, branch_id=resolved_branch)

    items = [
        ValidationItem(
            ref=raw.ref if raw.ref is not None else str(position),
            event_type=raw.event_type,
            name=raw.name,
            fields={key: scalar_text(value) for key, value in raw.fields.items()},
            properties=dict(raw.properties) if raw.properties is not None else None,
            complete=raw.complete,
        )
        for position, raw in enumerate(data.items)
    ]
    resolutions = await asyncio.to_thread(_resolve_all, items, snapshot)
    contexts = await load_event_contexts(
        session,
        {res.event.id for res in resolutions if res.event is not None},
        variable_tokens=snapshot.variable_tokens,
    )
    verdicts = await asyncio.to_thread(
        _check_all, resolutions, snapshot, contexts, strict=data.strict
    )

    results: list[PlanValidationItemResult] = []
    summary = PlanValidationSummary()
    for res, findings in zip(resolutions, verdicts, strict=True):
        status = item_status(findings)
        if status == "error":
            summary.errors += 1
        elif status == "warning":
            summary.warnings += 1
        else:
            summary.ok += 1
        results.append(
            PlanValidationItemResult(
                ref=res.item.ref,
                status=status,
                event_id=res.event.id if res.event is not None else None,
                identity=res.identity,
                findings=[
                    PlanValidationFinding(
                        code=f.code, severity=f.severity, field=f.field, message=f.message
                    )
                    for f in findings
                ],
            )
        )
    return PlanValidationResponse(items=results, summary=summary)


def _resolve_all(items: Sequence[ValidationItem], snapshot: PlanSnapshot) -> list[Resolution]:
    return [resolve_item(item, snapshot) for item in items]


def _check_all(
    resolutions: Sequence[Resolution],
    snapshot: PlanSnapshot,
    contexts: Mapping[uuid.UUID, EventContext],
    *,
    strict: bool,
) -> list[list[Finding]]:
    return [
        check_item(
            res,
            snapshot,
            contexts.get(res.event.id) if res.event is not None else None,
            strict=strict,
        )
        for res in resolutions
    ]


async def load_plan_snapshot(
    session: AsyncSession, *, project_id: uuid.UUID, branch_id: uuid.UUID
) -> PlanSnapshot:
    type_rows = list(
        (
            await session.execute(
                select(EventType)
                .where(EventType.project_id == project_id, EventType.branch_id == branch_id)
                .order_by(EventType.order, EventType.created_at)
            )
        )
        .scalars()
        .all()
    )
    configs_by_type = await load_governing_scan_configs_by_type(
        session, project_id=project_id, event_type_ids=[et.id for et in type_rows]
    )
    types_by_name = {
        et.name: PlanEventType(
            id=et.id,
            name=et.name,
            name_format=governing_name_format(configs_by_type.get(et.id, [])),
            fields={fd.name: _plan_field(fd) for fd in et.field_definitions},
        )
        for et in type_rows
    }

    event_rows = (
        await session.execute(
            select(Event.id, Event.event_type_id, Event.name, Event.source_name, Event.status)
            .where(Event.project_id == project_id, Event.branch_id == branch_id)
            .order_by(Event.order, Event.created_at, Event.id)
        )
    ).all()
    events = [
        PlanEvent(
            id=row.id,
            event_type_id=row.event_type_id,
            name=row.name,
            # The scan identity, or the name a row without one would adopt
            # (``_event_identity.index_events_by_identity``).
            identity=row.source_name if row.source_name is not None else row.name,
            status=str(row.status),
        )
        for row in event_rows
    ]

    variable_rows = (
        await session.execute(
            select(
                Variable.id,
                Variable.name,
                Variable.source_name,
                Variable.bindings,
                Variable.allowed_values,
            )
            .where(Variable.project_id == project_id, Variable.branch_id == branch_id)
            .order_by(Variable.name, Variable.id)
        )
    ).all()
    # A ``${token}`` in the plan may name a variable by its display name, its
    # source name or any binding. Same tokens, same order and the same
    # first-by-name-wins rule as the scan's ``VariableIndex``, so a token here
    # resolves to the variable the scan would have adopted for it.
    variable_allowed: dict[str, tuple[str, ...]] = {}
    variable_tokens: dict[uuid.UUID, tuple[str, ...]] = {}
    for row in variable_rows:
        allowed = tuple(str(value) for value in row.allowed_values or [])
        won: list[str] = []
        # ``tokens_of`` reads only name / source_name / bindings, which the row has.
        for token in VariableIndex.tokens_of(cast(Variable, row)):
            if token not in variable_allowed:
                variable_allowed[token] = allowed
                won.append(token)
        variable_tokens[row.id] = tuple(won)

    # Building the identity indexes sorts every identity: off the event loop.
    return await asyncio.to_thread(
        PlanSnapshot,
        types_by_name=types_by_name,
        events=events,
        variable_allowed=variable_allowed,
        variable_tokens=variable_tokens,
    )


def _plan_field(fd: FieldDefinition) -> PlanField:
    return PlanField(
        name=fd.name,
        field_type=str(fd.field_type),
        is_required=bool(fd.is_required),
        enum_options=tuple(str(option) for option in fd.enum_options or []),
        regex=fd.contract_regex or None,
        min_value=fd.contract_min_value,
        max_value=fd.contract_max_value,
    )


async def load_event_contexts(
    session: AsyncSession,
    event_ids: Iterable[uuid.UUID],
    *,
    variable_tokens: Mapping[uuid.UUID, tuple[str, ...]],
) -> dict[uuid.UUID, EventContext]:
    """Stored field values and variable overrides of the matched events only.

    An override is keyed by every token its variable won in the snapshot
    (``PlanSnapshot.variable_tokens``), the same keys as the global lists.
    """
    ids = sorted(set(event_ids))
    if not ids:
        return {}
    values: dict[uuid.UUID, dict[str, str]] = defaultdict(dict)
    overrides: dict[uuid.UUID, dict[str, tuple[str, ...]]] = defaultdict(dict)
    for chunk in _chunks(ids):
        for event_id, field_name, value in (
            await session.execute(
                select(EventFieldValue.event_id, FieldDefinition.name, EventFieldValue.value)
                .join(FieldDefinition, FieldDefinition.id == EventFieldValue.field_definition_id)
                .where(EventFieldValue.event_id.in_(chunk))
            )
        ).all():
            values[event_id][field_name] = value or ""
        for event_id, variable_id, override in (
            await session.execute(
                select(
                    VariableEventValueOverride.event_id,
                    VariableEventValueOverride.variable_id,
                    VariableEventValueOverride.values,
                ).where(VariableEventValueOverride.event_id.in_(chunk))
            )
        ).all():
            if override is None:
                # A property entry without an override: the global list applies.
                continue
            listed = tuple(str(v) for v in override)
            for token in variable_tokens.get(variable_id, ()):
                overrides[event_id][token] = listed
    return {
        event_id: EventContext(
            field_values=values.get(event_id, {}), overrides=overrides.get(event_id, {})
        )
        for event_id in ids
    }


def _chunks(ids: Sequence[uuid.UUID]) -> Iterable[Sequence[uuid.UUID]]:
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start : start + _ID_CHUNK]
