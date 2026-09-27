"""Export one branch of the plan as JSON Schema or as a codegen model (GH #262, F09).

The plan is loaded exactly the way ``POST /plan/validate`` loads it
(``plan_validation_service.load_plan_snapshot``: event types with their
governing naming rule and field definitions, events by identity, variables and
their allowed values keyed by every token that names them), plus every
non-archived event's stored field values and per-event variable overrides
(``load_event_contexts``). A few presentation columns the snapshot does not
carry (display names, descriptions, field order) come from one extra query each.

The shaping is pure (``build_json_schemas`` / ``build_codegen_model``) and runs
in a worker thread. Read-only; nothing is written and no lock is taken.

How a stored field value becomes a constraint, per event:

* a literal (``home``, ``9.99``) is the event's value: ``const``, typed by the
  field type (a number field's ``"9.99"`` is the number ``9.99``);
* a whole ``${token}`` is the variable's allowed values (the event's override
  list when it has one, which REPLACES the global list, as in validation):
  ``enum``; no allowed values means free;
* a template with holes (``item_${kind}``) on a string field is an anchored
  ``pattern`` with each hole an alternation of allowed values, or ``.*``.

Field contracts apply on top: ``enum_options`` (``enum``), ``contract_regex``
(``pattern``: JSON Schema's ``pattern`` is unanchored, the same partial-match
semantics validation and the drift job use) and ``contract_min_value`` /
``contract_max_value`` (``minimum`` / ``maximum``, number fields only: JSON
Schema ignores them on strings). When the plan value and a contract both want
the same keyword, the contract's copy goes into ``allOf`` so both hold.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.name_template import VARIABLE_TOKEN_PATTERN
from tripl.core.plan_validation import (
    STATUS_ARCHIVED,
    STATUS_DEPRECATED,
    EventContext,
    PlanEvent,
    PlanEventType,
    PlanField,
    PlanSnapshot,
)
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.plan_revision import PlanRevision
from tripl.models.variable import Variable
from tripl.schemas.plan_export import (
    CodegenEvent,
    CodegenEventType,
    CodegenField,
    CodegenVariable,
    PlanExportCodegenModel,
    PlanExportFormat,
    PlanExportJsonSchemaBundle,
)
from tripl.services.plan_branch_service import resolve_branch_id
from tripl.services.plan_validation_service import load_event_contexts, load_plan_snapshot

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# ECMA-262 syntax characters; everything else is literal in a ``pattern``.
_REGEX_SPECIALS = frozenset("\\^$.*+?()[]{}|/")
_STRINGISH = frozenset({"string", "enum", "url"})


# ---------------------------------------------------------------------------
# Inputs of the pure builders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldMeta:
    display_name: str = ""
    description: str = ""
    order: int = 0


@dataclass(frozen=True)
class TypeMeta:
    display_name: str = ""
    description: str = ""
    # Field name -> presentation metadata.
    fields: Mapping[str, FieldMeta] = field(default_factory=dict)


@dataclass(frozen=True)
class VariableInfo:
    id: uuid.UUID
    name: str
    allowed_values: tuple[str, ...]


@dataclass(frozen=True)
class ExportPlan:
    """Everything the builders read; built once per request."""

    snapshot: PlanSnapshot
    contexts: Mapping[uuid.UUID, EventContext]
    type_meta: Mapping[uuid.UUID, TypeMeta] = field(default_factory=dict)
    event_descriptions: Mapping[uuid.UUID, str] = field(default_factory=dict)
    variables: Sequence[VariableInfo] = ()

    def exported_events(self, event_type: PlanEventType) -> list[PlanEvent]:
        return [
            ev
            for ev in self.snapshot.events
            if ev.event_type_id == event_type.id and ev.status != STATUS_ARCHIVED
        ]

    def ordered_fields(self, event_type: PlanEventType) -> list[PlanField]:
        meta = self.type_meta.get(event_type.id, TypeMeta()).fields
        return sorted(
            event_type.fields.values(),
            key=lambda fd: (meta.get(fd.name, FieldMeta()).order, fd.name),
        )

    def field_values(self, event: PlanEvent, event_type: PlanEventType) -> dict[str, str]:
        """The event's non-empty stored values of its own type's fields."""
        ctx = self.contexts.get(event.id)
        if ctx is None:
            return {}
        return {
            fd.name: ctx.field_values[fd.name]
            for fd in self.ordered_fields(event_type)
            if ctx.field_values.get(fd.name)
        }

    def allowed_for(self, event: PlanEvent, token: str) -> tuple[str, ...]:
        ctx = self.contexts.get(event.id)
        if ctx is not None and token in ctx.overrides:
            return ctx.overrides[token]
        return self.snapshot.variable_allowed.get(token, ())

    def overrides_for(self, event: PlanEvent) -> dict[str, list[str]]:
        """The event's per-event variable overrides, token -> values, tokens sorted."""
        ctx = self.contexts.get(event.id)
        if ctx is None:
            return {}
        return {token: list(ctx.overrides[token]) for token in sorted(ctx.overrides)}

    def token_names(self) -> dict[str, str]:
        """Every token -> the display name of the variable that won it."""
        names: dict[str, str] = {}
        for var in self.variables:
            for token in self.snapshot.variable_tokens.get(var.id, ()):
                names[token] = var.name
        return names


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def whole_token(value: str) -> str | None:
    """``platform`` for ``${platform}``; ``None`` for anything else."""
    match = VARIABLE_TOKEN_PATTERN.fullmatch(value)
    return match.group(1) if match is not None else None


def typed_value(field_type: str, text: str) -> tuple[bool, Any]:
    """A plan string as the JSON value a payload carries; ``(False, None)`` if it cannot be."""
    if field_type == "number":
        try:
            number = float(text)
        except ValueError:
            return False, None
        if not math.isfinite(number):
            return False, None
        if number.is_integer() and all(ch not in text for ch in ".eE"):
            return True, int(number)
        return True, number
    if field_type == "boolean":
        lowered = text.strip().lower()
        if lowered in {"true", "false"}:
            return True, lowered == "true"
        return False, None
    if field_type == "json":
        try:
            return True, json.loads(text)
        except ValueError:
            return False, None
    return True, text


def regex_escape(text: str) -> str:
    """Escape ``text`` for an ECMA-262 (and Python ``re``) pattern."""
    return "".join(f"\\{ch}" if ch in _REGEX_SPECIALS else ch for ch in text)


def template_pattern(template: str, allowed: Mapping[str, tuple[str, ...]]) -> str:
    """``item_${kind}`` -> ``^item_(?:hat|car)$``; a hole with no list is ``.*``."""
    parts: list[str] = []
    cursor = 0
    for match in VARIABLE_TOKEN_PATTERN.finditer(template):
        parts.append(regex_escape(template[cursor : match.start()]))
        values = allowed.get(match.group(1), ())
        parts.append("(?:" + "|".join(regex_escape(v) for v in values) + ")" if values else ".*")
        cursor = match.end()
    parts.append(regex_escape(template[cursor:]))
    return "^" + "".join(parts) + "$"


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


def _type_keywords(fd: PlanField) -> dict[str, Any]:
    match fd.field_type:
        case "number":
            return {"type": "number"}
        case "boolean":
            return {"type": "boolean"}
        case "url":
            return {"type": "string", "format": "uri"}
        case "json":
            return {}
        case _:
            return {"type": "string"}


def _contract_keywords(fd: PlanField) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if fd.field_type == "enum" and fd.enum_options:
        out["enum"] = list(fd.enum_options)
    if fd.regex:
        out["pattern"] = fd.regex
    if fd.field_type == "number":
        if fd.min_value is not None:
            out["minimum"] = fd.min_value
        if fd.max_value is not None:
            out["maximum"] = fd.max_value
    return out


def _plan_value_keywords(
    plan: ExportPlan, event: PlanEvent, fd: PlanField, value: str | None
) -> dict[str, Any]:
    if not value:
        return {}
    token = whole_token(value)
    if token is not None:
        allowed = plan.allowed_for(event, token)
        typed = [tv for ok, tv in (typed_value(fd.field_type, v) for v in allowed) if ok]
        return {"enum": _dedupe(typed)} if typed else {}
    if VARIABLE_TOKEN_PATTERN.search(value) is None:
        ok, typed_literal = typed_value(fd.field_type, value)
        return {"const": typed_literal} if ok else {}
    if fd.field_type not in _STRINGISH:
        return {}
    tokens = {m.group(1) for m in VARIABLE_TOKEN_PATTERN.finditer(value)}
    return {"pattern": template_pattern(value, {t: plan.allowed_for(event, t) for t in tokens})}


def _dedupe(values: Sequence[Any]) -> list[Any]:
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def field_schema(
    plan: ExportPlan, event: PlanEvent, fd: PlanField, value: str | None, meta: FieldMeta
) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    if meta.display_name:
        schema["title"] = meta.display_name
    if meta.description:
        schema["description"] = meta.description
    schema.update(_type_keywords(fd))
    schema.update(_plan_value_keywords(plan, event, fd, value))
    all_of: list[dict[str, Any]] = []
    for key, constraint in _contract_keywords(fd).items():
        if key in schema:
            all_of.append({key: constraint})
        else:
            schema[key] = constraint
    if all_of:
        schema["allOf"] = all_of
    return schema


def event_schema(plan: ExportPlan, event_type: PlanEventType, event: PlanEvent) -> dict[str, Any]:
    type_meta = plan.type_meta.get(event_type.id, TypeMeta())
    values = plan.field_values(event, event_type)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for fd in plan.ordered_fields(event_type):
        properties[fd.name] = field_schema(
            plan, event, fd, values.get(fd.name), type_meta.fields.get(fd.name, FieldMeta())
        )
        if fd.is_required:
            required.append(fd.name)
    schema: dict[str, Any] = {
        "$schema": JSON_SCHEMA_DIALECT,
        "title": event.name,
    }
    description = plan.event_descriptions.get(event.id, "")
    if description:
        schema["description"] = description
    if event.status == STATUS_DEPRECATED:
        schema["deprecated"] = True
    schema["type"] = "object"
    schema["properties"] = properties
    schema["required"] = required
    # Annotation only; a 2020-12 validator ignores unknown keywords.
    schema["x-tripl"] = {
        "event_type": event_type.name,
        "identity": event.identity,
        "name": event.name,
        "status": event.status,
    }
    return schema


def _unique_key(key: str, taken: set[str]) -> str:
    candidate, n = key, 2
    while candidate in taken:
        candidate = f"{key}#{n}"
        n += 1
    taken.add(candidate)
    return candidate


def build_json_schemas(plan: ExportPlan) -> dict[str, dict[str, Any]]:
    """``<event_type>/<identity>`` -> schema, in plan order, archived excluded.

    Two events of a type sharing an identity (a plan the scan would flag) keep
    both, the later one keyed ``…#2``.
    """
    schemas: dict[str, dict[str, Any]] = {}
    taken: set[str] = set()
    for event_type in plan.snapshot.types_by_name.values():
        for event in plan.exported_events(event_type):
            key = _unique_key(f"{event_type.name}/{event.identity}", taken)
            schemas[key] = event_schema(plan, event_type, event)
    return schemas


# ---------------------------------------------------------------------------
# Codegen model
# ---------------------------------------------------------------------------


#: The most values one templated field value may expand to in the codegen model;
#: past it the field is free.
MAX_TEMPLATE_EXPANSION = 1000


def expand_template(
    template: str, allowed: Mapping[str, tuple[str, ...]], *, limit: int = MAX_TEMPLATE_EXPANSION
) -> list[str] | None:
    """``item_${kind}`` with ``kind`` in (hat, car) -> ``[item_hat, item_car]``.

    ``None`` when a hole has no allowed values or the product exceeds ``limit``.
    """
    results = [""]
    cursor = 0
    for match in VARIABLE_TOKEN_PATTERN.finditer(template):
        values = allowed.get(match.group(1), ())
        if not values or len(results) * len(values) > limit:
            return None
        literal = template[cursor : match.start()]
        results = [prefix + literal + value for prefix in results for value in values]
        cursor = match.end()
    tail = template[cursor:]
    return [prefix + tail for prefix in results]


def _type_field(
    plan: ExportPlan,
    events: Sequence[PlanEvent],
    fd: PlanField,
    meta: FieldMeta,
    token_names: Mapping[str, str],
    values_by_event: Mapping[uuid.UUID, Mapping[str, str]],
) -> CodegenField:
    """One field of a type: the union of what its events allow, or free.

    Closed only when it is safe to generate an enum from it: a string-like
    field that EVERY exported event of the type fills with a literal, a
    variable with allowed values or a template whose holes all have them. An
    event that leaves the field unset says nothing about its value, so the
    field is free (``None``); so is a number, boolean or json field. A free
    enum field falls back to its ``enum_options``; a closed one is narrowed to
    them, as validation checks both.
    """
    collected: list[str] = []
    free = fd.field_type not in _STRINGISH or not events
    tokens: set[str | None] = set()
    bearing = 0
    for event in events:
        value = values_by_event[event.id].get(fd.name)
        if not value:
            free = True
            continue
        bearing += 1
        token = whole_token(value)
        tokens.add(token)
        if token is not None:
            allowed = plan.allowed_for(event, token)
            if allowed:
                collected.extend(allowed)
            else:
                free = True
        elif VARIABLE_TOKEN_PATTERN.search(value) is not None:
            holes = {m.group(1) for m in VARIABLE_TOKEN_PATTERN.finditer(value)}
            expanded = expand_template(value, {t: plan.allowed_for(event, t) for t in holes})
            if expanded is None:
                free = True
            else:
                collected.extend(expanded)
        else:
            collected.append(value)
    options = list(fd.enum_options) if fd.field_type == "enum" else []
    values: list[str] | None = None
    if not free:
        values = list(dict.fromkeys(collected))
        if options:
            values = [v for v in values if v in options] or options
    elif options:
        values = options
    variable: str | None = None
    if bearing and len(tokens) == 1:
        (only,) = tokens
        if only is not None:
            variable = token_names.get(only, only)
    return CodegenField(
        name=fd.name,
        display_name=meta.display_name,
        description=meta.description,
        required=fd.is_required,
        type=fd.field_type,
        values=values,
        variable=variable,
    )


def build_codegen_model(
    plan: ExportPlan,
) -> tuple[list[CodegenEventType], list[CodegenVariable]]:
    token_names = plan.token_names()
    event_types: list[CodegenEventType] = []
    for event_type in plan.snapshot.types_by_name.values():
        meta = plan.type_meta.get(event_type.id, TypeMeta())
        events = plan.exported_events(event_type)
        values_by_event = {ev.id: plan.field_values(ev, event_type) for ev in events}
        event_types.append(
            CodegenEventType(
                name=event_type.name,
                display_name=meta.display_name,
                description=meta.description,
                name_rule=event_type.name_format,
                fields=[
                    _type_field(
                        plan,
                        events,
                        fd,
                        meta.fields.get(fd.name, FieldMeta()),
                        token_names,
                        values_by_event,
                    )
                    for fd in plan.ordered_fields(event_type)
                ],
                events=[
                    CodegenEvent(
                        identity=ev.identity,
                        name=ev.name,
                        description=plan.event_descriptions.get(ev.id, ""),
                        status=ev.status,
                        field_values=values_by_event[ev.id],
                        overrides=plan.overrides_for(ev),
                        deprecated=ev.status == STATUS_DEPRECATED,
                    )
                    for ev in events
                ],
            )
        )
    variables = [
        CodegenVariable(
            name=var.name,
            allowed_values=list(var.allowed_values),
            tokens=list(plan.snapshot.variable_tokens.get(var.id, ())),
        )
        for var in plan.variables
    ]
    return event_types, variables


def content_hash(content: object) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Loading and the service entry point
# ---------------------------------------------------------------------------


async def export_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    export_format: PlanExportFormat,
) -> PlanExportJsonSchemaBundle | PlanExportCodegenModel:
    """The plan of ``branch_id`` (``None`` = main) in ``export_format``."""
    resolved = await resolve_branch_id(session, project_id, branch_id)
    branch = await session.get(PlanBranch, resolved)
    assert branch is not None  # resolve_branch_id just found or created it
    revision = await _revision_of(session, project_id, branch)
    plan = await load_export_plan(session, project_id=project_id, branch_id=resolved)

    if export_format == "jsonschema":
        schemas = await asyncio.to_thread(build_json_schemas, plan)
        return PlanExportJsonSchemaBundle(
            revision=revision,
            branch=branch.name,
            branch_id=branch.id,
            plan_hash=content_hash(schemas),
            schemas=schemas,
        )
    event_types, variables = await asyncio.to_thread(build_codegen_model, plan)
    body = {
        "event_types": [et.model_dump(mode="json") for et in event_types],
        "variables": [var.model_dump(mode="json") for var in variables],
    }
    return PlanExportCodegenModel(
        revision=revision,
        branch=branch.name,
        branch_id=branch.id,
        plan_hash=content_hash(body),
        event_types=event_types,
        variables=variables,
    )


async def _revision_of(
    session: AsyncSession, project_id: uuid.UUID, branch: PlanBranch
) -> uuid.UUID | None:
    if branch.kind != BranchKind.main.value:
        return branch.base_revision_id
    revision_id: uuid.UUID | None = await session.scalar(
        select(PlanRevision.id)
        .where(PlanRevision.project_id == project_id)
        .order_by(PlanRevision.created_at.desc(), PlanRevision.id.desc())
        .limit(1)
    )
    return revision_id


async def load_export_plan(
    session: AsyncSession, *, project_id: uuid.UUID, branch_id: uuid.UUID
) -> ExportPlan:
    snapshot = await load_plan_snapshot(session, project_id=project_id, branch_id=branch_id)
    exported_ids = [ev.id for ev in snapshot.events if ev.status != STATUS_ARCHIVED]
    contexts = await load_event_contexts(
        session, exported_ids, variable_tokens=snapshot.variable_tokens
    )

    type_ids = [et.id for et in snapshot.types_by_name.values()]
    field_meta: dict[uuid.UUID, dict[str, FieldMeta]] = {type_id: {} for type_id in type_ids}
    if type_ids:
        for row in (
            await session.execute(
                select(
                    FieldDefinition.event_type_id,
                    FieldDefinition.name,
                    FieldDefinition.display_name,
                    FieldDefinition.description,
                    FieldDefinition.order,
                ).where(FieldDefinition.event_type_id.in_(type_ids))
            )
        ).all():
            field_meta.setdefault(row.event_type_id, {})[row.name] = FieldMeta(
                display_name=row.display_name or "",
                description=row.description or "",
                order=row.order or 0,
            )
    type_meta = {
        row.id: TypeMeta(
            display_name=row.display_name or "",
            description=row.description or "",
            fields=field_meta.get(row.id, {}),
        )
        for row in (
            await session.execute(
                select(EventType.id, EventType.display_name, EventType.description).where(
                    EventType.project_id == project_id, EventType.branch_id == branch_id
                )
            )
        ).all()
    }
    event_descriptions = {
        row.id: row.description or ""
        for row in (
            await session.execute(
                select(Event.id, Event.description).where(
                    Event.project_id == project_id,
                    Event.branch_id == branch_id,
                    Event.description != "",
                )
            )
        ).all()
    }
    variables = [
        VariableInfo(
            id=row.id,
            name=row.name,
            allowed_values=tuple(str(v) for v in row.allowed_values or []),
        )
        for row in (
            await session.execute(
                select(Variable.id, Variable.name, Variable.allowed_values)
                .where(Variable.project_id == project_id, Variable.branch_id == branch_id)
                .order_by(Variable.name, Variable.id)
            )
        ).all()
    ]
    return ExportPlan(
        snapshot=snapshot,
        contexts=contexts,
        type_meta=type_meta,
        event_descriptions=event_descriptions,
        variables=variables,
    )
