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

``?revision=`` validates against a stored plan revision instead of the live
rows (``pinned_plan_snapshot``): the event types, their fields and contracts,
the events, their field values, the variables and their per-event overrides
all come from the revision's payload, so the same calls get the same verdicts
whatever the plan has become since. Two inputs are NOT in a revision and stay
live: an event type's naming rule, which belongs to its scan config, and an
installed extension's policy rules.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import extensions
from tripl.core.analyzers._event_generator_variables import VariableIndex
from tripl.core.plan_policy import PlanPolicyContext, PolicyCall
from tripl.core.plan_validation import (
    CODE_POLICY_VIOLATION,
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
from tripl.models.project import Project
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.schemas.plan_validation import (
    PlanValidationFinding,
    PlanValidationItemResult,
    PlanValidationPlan,
    PlanValidationRequest,
    PlanValidationResponse,
    PlanValidationSummary,
)
from tripl.services._id_chunks import chunked
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.plan_revision_service import (
    load_revision,
    plan_snapshot_hash,
    with_snapshot_defaults,
)
from tripl.services.scan_config_lookup import (
    governing_name_format,
    load_governing_scan_configs_by_type,
)


async def validate_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    data: PlanValidationRequest,
    revision_id: uuid.UUID | None = None,
) -> PlanValidationResponse:
    """One verdict per item, in order, against ``branch_id`` (``None`` = main).

    With ``revision_id``, against that stored revision of the plan instead;
    ``branch_id`` must then be ``None`` (the route refuses the pair).
    """
    resolved_branch = await resolve_branch_id(session, project_id, branch_id)
    payload: dict[str, Any] | None = None
    plan = PlanValidationPlan()
    if revision_id is None:
        snapshot = await load_plan_snapshot(
            session, project_id=project_id, branch_id=resolved_branch
        )
    else:
        revision = await load_revision(session, project_id, revision_id)
        payload = with_snapshot_defaults(revision.payload or {})
        snapshot = await pinned_plan_snapshot(session, project_id=project_id, payload=payload)
        plan = PlanValidationPlan(
            revision=revision.id,
            plan_hash=plan_snapshot_hash(payload),
            created_at=revision.created_at,
        )

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
    matched = {res.event.id for res in resolutions if res.event is not None}
    contexts = (
        await load_event_contexts(session, matched, variable_tokens=snapshot.variable_tokens)
        if payload is None
        else pinned_event_contexts(payload, matched, variable_tokens=snapshot.variable_tokens)
    )
    verdicts = await asyncio.to_thread(
        _check_all, resolutions, snapshot, contexts, strict=data.strict
    )
    await _add_policy_findings(
        session,
        project_id=project_id,
        branch_id=resolved_branch,
        resolutions=resolutions,
        verdicts=verdicts,
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
                        code=f.code,
                        severity=f.severity,
                        field=f.field,
                        message=f.message,
                        rule=f.rule,
                    )
                    for f in findings
                ],
            )
        )
    return PlanValidationResponse(items=results, summary=summary, plan=plan)


async def _add_policy_findings(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    resolutions: Sequence[Resolution],
    verdicts: Sequence[list[Finding]],
) -> None:
    """Append the installed extensions' policy violations to each call's findings.

    A violation lands on the call its ``item_ref`` names, as a
    ``policy_violation`` finding with the rule in ``rule``; one naming no call
    of this batch is dropped. Nothing is asked when no extension is installed.
    """
    if not extensions.extensions():
        return
    project = (
        await session.execute(
            select(Project.organization_id, Project.slug).where(Project.id == project_id)
        )
    ).one_or_none()
    if project is None:
        return
    calls = [
        PolicyCall(
            ref=res.item.ref,
            event_type=res.event_type.name if res.event_type is not None else res.item.event_type,
            name=res.item.name,
            identity=res.identity,
            event_id=res.event.id if res.event is not None else None,
            field_names=tuple(dict.fromkeys([*res.item.fields, *(res.item.properties or {})])),
            complete=res.item.complete,
        )
        for res in resolutions
    ]
    violations = await extensions.plan_policy_violations(
        session,
        PlanPolicyContext(
            phase="validate",
            organization_id=project.organization_id,
            project_id=project_id,
            project_slug=project.slug,
            branch_id=branch_id,
            calls=calls,
        ),
    )
    position_by_ref: dict[str, int] = {}
    for position, res in enumerate(resolutions):
        position_by_ref.setdefault(res.item.ref, position)
    for violation in violations:
        target = position_by_ref.get(violation.item_ref or "")
        if target is None:
            continue
        verdicts[target].append(
            Finding(
                code=CODE_POLICY_VIOLATION,
                severity=violation.severity,
                field=violation.field,
                message=violation.message,
                rule=violation.rule,
            )
        )


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
                Variable.variable_type,
                Variable.json_schema,
            )
            .where(Variable.project_id == project_id, Variable.branch_id == branch_id)
            .order_by(Variable.name, Variable.id)
        )
    ).all()
    variable_allowed, variable_tokens, variable_types = _variable_maps(variable_rows)

    # Building the identity indexes sorts every identity: off the event loop.
    return await asyncio.to_thread(
        PlanSnapshot,
        types_by_name=types_by_name,
        events=events,
        variable_allowed=variable_allowed,
        variable_tokens=variable_tokens,
        variable_types=variable_types,
    )


def _variable_maps(
    rows: Iterable[Any],
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[uuid.UUID, tuple[str, ...]],
    dict[str, tuple[str, Mapping[str, Any] | None]],
]:
    """``(allowed, tokens, types)`` of ``PlanSnapshot``, from variables in name order.

    A ``${token}`` in the plan may name a variable by its display name, its
    source name or any binding. Same tokens, same order and the same
    first-by-name-wins rule as the scan's ``VariableIndex``, so a token here
    resolves to the variable the scan would have adopted for it. ``rows`` need
    only ``id``, ``name``, ``source_name``, ``bindings``, ``allowed_values``,
    ``variable_type`` and ``json_schema``.
    """
    variable_allowed: dict[str, tuple[str, ...]] = {}
    variable_tokens: dict[uuid.UUID, tuple[str, ...]] = {}
    variable_types: dict[str, tuple[str, Mapping[str, Any] | None]] = {}
    for row in rows:
        allowed = tuple(str(value) for value in row.allowed_values or [])
        won: list[str] = []
        # ``tokens_of`` reads only name / source_name / bindings, which the row has.
        for token in VariableIndex.tokens_of(cast(Variable, row)):
            if token not in variable_allowed:
                variable_allowed[token] = allowed
                variable_types[token] = (str(row.variable_type), row.json_schema)
                won.append(token)
        variable_tokens[row.id] = tuple(won)
    return variable_allowed, variable_tokens, variable_types


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _pinned_variables(payload: Mapping[str, Any]) -> list[SimpleNamespace]:
    """The revision's variables as the rows ``_variable_maps`` reads, in its order."""
    rows = [
        SimpleNamespace(
            id=variable_id,
            name=str(raw.get("name") or ""),
            source_name=raw.get("source_name"),
            bindings=raw.get("bindings") or [],
            allowed_values=raw.get("allowed_values") or [],
            variable_type=raw.get("variable_type"),
            json_schema=raw.get("json_schema"),
            overrides=_dicts(raw.get("event_value_overrides")),
        )
        for raw in _dicts(payload.get("variables"))
        if (variable_id := _uuid(raw.get("id"))) is not None
    ]
    # The live query's order: by name, then id.
    return sorted(rows, key=lambda row: (row.name, row.id))


async def pinned_plan_snapshot(
    session: AsyncSession, *, project_id: uuid.UUID, payload: Mapping[str, Any]
) -> PlanSnapshot:
    """``load_plan_snapshot`` read from a revision's payload instead of the live rows.

    The naming rule is the one thing taken from today: it is the scan config's,
    not the plan's, and no revision stores it. It is looked up by the event
    type's id, so a type deleted since has none, as a type no config names has.
    """
    raw_types = sorted(
        (
            (type_id, raw)
            for raw in _dicts(payload.get("event_types"))
            if (type_id := _uuid(raw.get("id"))) is not None
        ),
        key=lambda pair: (pair[1].get("order") or 0, str(pair[1].get("name") or "")),
    )
    configs_by_type = await load_governing_scan_configs_by_type(
        session, project_id=project_id, event_type_ids=[type_id for type_id, _ in raw_types]
    )
    types_by_name = {
        str(raw.get("name") or ""): PlanEventType(
            id=type_id,
            name=str(raw.get("name") or ""),
            name_format=governing_name_format(configs_by_type.get(type_id, [])),
            fields={
                str(fd.get("name") or ""): PlanField(
                    name=str(fd.get("name") or ""),
                    field_type=str(fd.get("field_type")),
                    is_required=bool(fd.get("is_required")),
                    enum_options=tuple(str(option) for option in fd.get("enum_options") or []),
                    regex=fd.get("contract_regex") or None,
                    min_value=fd.get("contract_min_value"),
                    max_value=fd.get("contract_max_value"),
                )
                for fd in _dicts(raw.get("field_definitions"))
            },
        )
        for type_id, raw in raw_types
    }
    raw_events = sorted(
        (
            (event_id, type_id, raw)
            for raw in _dicts(payload.get("events"))
            if (event_id := _uuid(raw.get("id"))) is not None
            and (type_id := _uuid(raw.get("event_type_id"))) is not None
        ),
        key=lambda row: (row[2].get("order") or 0, str(row[0])),
    )
    events = [
        PlanEvent(
            id=event_id,
            event_type_id=type_id,
            name=str(raw.get("name") or ""),
            identity=str(
                raw["source_name"] if raw.get("source_name") is not None else raw.get("name") or ""
            ),
            status=str(raw.get("status")),
        )
        for event_id, type_id, raw in raw_events
    ]
    variable_allowed, variable_tokens, variable_types = _variable_maps(_pinned_variables(payload))
    return await asyncio.to_thread(
        PlanSnapshot,
        types_by_name=types_by_name,
        events=events,
        variable_allowed=variable_allowed,
        variable_tokens=variable_tokens,
        variable_types=variable_types,
    )


def pinned_event_contexts(
    payload: Mapping[str, Any],
    event_ids: Iterable[uuid.UUID],
    *,
    variable_tokens: Mapping[uuid.UUID, tuple[str, ...]],
) -> dict[uuid.UUID, EventContext]:
    """``load_event_contexts`` read from a revision's payload.

    A revision keys an override by its event's type and name, not its id, so
    the matched events are looked up by that pair.
    """
    wanted = set(event_ids)
    if not wanted:
        return {}
    values: dict[uuid.UUID, dict[str, str]] = {}
    by_name: dict[tuple[str, str], uuid.UUID] = {}
    for raw in _dicts(payload.get("events")):
        event_id = _uuid(raw.get("id"))
        if event_id is None or event_id not in wanted:
            continue
        by_name[(str(raw.get("event_type_name") or ""), str(raw.get("name") or ""))] = event_id
        values[event_id] = {
            str(entry.get("field_name") or ""): str(entry.get("value") or "")
            for entry in _dicts(raw.get("field_values"))
        }
    overrides: dict[uuid.UUID, dict[str, tuple[str, ...]]] = defaultdict(dict)
    required_tokens: dict[uuid.UUID, set[str]] = defaultdict(set)
    for variable in _pinned_variables(payload):
        tokens = variable_tokens.get(variable.id, ())
        for override in variable.overrides:
            key = (
                str(override.get("event_type_name") or ""),
                str(override.get("event_name") or ""),
            )
            found = by_name.get(key)
            if found is None:
                continue
            if override.get("required"):
                required_tokens[found].update(tokens)
            listed = override.get("values")
            if listed is None:
                # A property entry without an override: the global list applies.
                continue
            for token in tokens:
                overrides[found][token] = tuple(str(value) for value in listed)
    return {
        event_id: EventContext(
            field_values=values.get(event_id, {}),
            overrides=overrides.get(event_id, {}),
            required_tokens=frozenset(required_tokens.get(event_id, ())),
        )
        for event_id in sorted(wanted)
    }


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
    required_tokens: dict[uuid.UUID, set[str]] = defaultdict(set)
    for chunk in chunked(ids):
        for event_id, field_name, value in (
            await session.execute(
                select(EventFieldValue.event_id, FieldDefinition.name, EventFieldValue.value)
                .join(FieldDefinition, FieldDefinition.id == EventFieldValue.field_definition_id)
                .where(EventFieldValue.event_id.in_(chunk))
            )
        ).all():
            values[event_id][field_name] = value or ""
        for event_id, variable_id, override, required in (
            await session.execute(
                select(
                    VariableEventValueOverride.event_id,
                    VariableEventValueOverride.variable_id,
                    VariableEventValueOverride.values,
                    VariableEventValueOverride.required,
                ).where(VariableEventValueOverride.event_id.in_(chunk))
            )
        ).all():
            if required:
                required_tokens[event_id].update(variable_tokens.get(variable_id, ()))
            if override is None:
                # A property entry without an override: the global list applies.
                continue
            listed = tuple(str(v) for v in override)
            for token in variable_tokens.get(variable_id, ()):
                overrides[event_id][token] = listed
    return {
        event_id: EventContext(
            field_values=values.get(event_id, {}),
            overrides=overrides.get(event_id, {}),
            required_tokens=frozenset(required_tokens.get(event_id, ())),
        )
        for event_id in ids
    }
