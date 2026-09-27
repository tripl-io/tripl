"""Column- and variable-shaped edges: what reads a field, and where a variable lives.

Split out of ``_dependency_edges`` (which holds the edge builders and the
vocabulary) because a field is the one entity whose dependents are found by
NAME as well as by id: a composition metric's breakdown, an event's or a scan's
metric breakdown columns, and — as ``possible`` edges only — a fact metric's
column, a variable binding or a whole identifier in free SQL
(``_dependency_sql``). Reads only.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from sqlalchemy import or_, select

from tripl.core.name_template import variable_tokens
from tripl.core.variable_retirement import tokens_of
from tripl.models.domain_enums import MetricKind
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.fact_table import FactTable
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.models.variable_value import VariableValue
from tripl.services._dependency_edges import (
    EVENT_BREAKDOWN_COLUMN,
    FACT_TABLE_SQL_MENTIONS,
    FIELD_OF_TYPE,
    METRIC_BREAKDOWN_COLUMN,
    METRIC_SAME_NAME_COLUMN,
    METRIC_SQL_MENTIONS,
    RELATION_LINKS_FIELD,
    SCAN_BREAKDOWN_COLUMN,
    VARIABLE_BINDING_SAME_NAME,
    VARIABLE_BOUND_TO_FIELD,
    VARIABLE_IN_EVENT_VALUE,
    VARIABLE_IN_FIELD_VALUE,
    VARIABLE_IN_META_VALUE,
    VARIABLE_ON_EVENT,
    VARIABLE_OVERRIDE_ON_EVENT,
    Pair,
    _event_edge,
    _fact_table_edge,
    _field_edge,
    _metric_edge,
    _relation_edges,
    _scan_edge,
    _type_edge,
    _variable_edge,
    _variables_by_token,
)
from tripl.services._dependency_locate import (
    EVENT_LOAD,
    VARIABLE_LOAD,
    Scope,
    event_type_identity,
)
from tripl.services._dependency_model import Edge
from tripl.services._dependency_sql import sql_mentions
from tripl.services.fact_table_dependents import metric_used_columns

__all__ = ["field_deps", "variable_deps"]

SCAN_DRIFT_FIELD = "scan tracks distribution drift on column"
SCAN_PLATFORM_COLUMN = "scan reads column as its platform"
SCAN_APP_VERSION_COLUMN = "scan reads column as its app version"
SCAN_SQL_MENTIONS = "scan query mentions the column by name"


# ── fields ──────────────────────────────────────────────────────────────────


def _scopes(config: object) -> list[Mapping[str, object]]:
    """A metric config and its ratio operands, the places ``filter_sql`` lives."""
    if not isinstance(config, Mapping):
        return []
    found: list[Mapping[str, object]] = [config]
    for role in ("numerator", "denominator"):
        operand = config.get(role)
        if isinstance(operand, Mapping):
            found.append(operand)
    return found


def _metric_breakdown_names(metric: MetricDefinition) -> set[str]:
    names = {c for c in metric.breakdown_columns or [] if isinstance(c, str)}
    for extra in (metric.app_version_column, metric.platform_column):
        if isinstance(extra, str) and extra:
            names.add(extra)
    return names


def _metric_sql_mentions(metric: MetricDefinition, name: str) -> bool:
    config = metric.config if isinstance(metric.config, Mapping) else {}
    raw_sql = config.get("metric_sql")
    if isinstance(raw_sql, str) and sql_mentions(raw_sql, name):
        return True
    return any(
        isinstance(scope.get("filter_sql"), str) and sql_mentions(str(scope["filter_sql"]), name)
        for scope in _scopes(config)
    )


def _fact_table_mentions(table: FactTable, name: str) -> bool:
    lowered = name.lower()
    column_names = {
        str(column.get("name", "")).lower()
        for column in table.columns or []
        if isinstance(column, Mapping)
    }
    column_names.update(str(c).lower() for c in table.identifier_columns or [])
    column_names.add((table.timestamp_column or "").lower())
    if lowered in column_names or sql_mentions(table.sql, name):
        return True
    return any(
        isinstance(item, Mapping) and sql_mentions(str(item.get("sql", "")), name)
        for item in table.row_filters or []
    )


async def _field_metric_edges(
    scope: Scope, field_def: FieldDefinition, type_ids: set[uuid.UUID]
) -> list[Edge]:
    name = field_def.name
    event_ids = set(
        (
            await scope.session.scalars(select(Event.id).where(Event.event_type_id.in_(type_ids)))
        ).all()
    )
    edges: list[Edge] = []
    for metric in await scope.metrics():
        kind = str(getattr(metric.kind, "value", metric.kind))
        if kind == MetricKind.event_composition.value:
            reads_type = (
                metric.numerator_event_type_id in type_ids
                or metric.denominator_event_type_id in type_ids
                or metric.numerator_event_id in event_ids
                or metric.denominator_event_id in event_ids
            )
            if reads_type and name in _metric_breakdown_names(metric):
                edges.append(_metric_edge(scope, metric, METRIC_BREAKDOWN_COLUMN))
        elif kind == MetricKind.fact.value:
            if name in metric_used_columns(metric):
                edges.append(_metric_edge(scope, metric, METRIC_SAME_NAME_COLUMN, "possible"))
            elif _metric_sql_mentions(metric, name):
                edges.append(_metric_edge(scope, metric, METRIC_SQL_MENTIONS, "possible"))
        elif _metric_sql_mentions(metric, name):
            edges.append(_metric_edge(scope, metric, METRIC_SQL_MENTIONS, "possible"))
    return edges


def _scan_column_relation(scan: ScanConfig, name: str) -> str | None:
    """Which of the scan's column settings names ``name``, if any."""
    if name in (scan.metric_breakdown_columns or []):
        return SCAN_BREAKDOWN_COLUMN
    if name in (scan.distribution_drift_fields or []):
        return SCAN_DRIFT_FIELD
    if scan.platform_column == name:
        return SCAN_PLATFORM_COLUMN
    if scan.app_version_column == name:
        return SCAN_APP_VERSION_COLUMN
    return None


def _scan_column_edge(
    scope: Scope, scan: ScanConfig, name: str, type_ids: set[uuid.UUID]
) -> Edge | None:
    """A scan reading the field: ``direct`` when bound to its type, else a hint.

    A column setting on a scan bound to THIS type is a direct read; on an
    unbound scan (every type) it is only a name match; on a scan bound to
    another type it is someone else's column. The scan's ``base_query``
    mentioning the name is always only ``possible``.
    """
    relation = _scan_column_relation(scan, name)
    if relation is not None:
        if scan.event_type_id in type_ids:
            return _scan_edge(scope, scan, relation)
        if scan.event_type_id is None:
            return _scan_edge(scope, scan, relation, "possible")
    if sql_mentions(scan.base_query, name):
        return _scan_edge(scope, scan, SCAN_SQL_MENTIONS, "possible")
    return None


async def field_deps(scope: Scope, field_def: FieldDefinition, event_type: EventType) -> Pair:
    type_ids = await event_type_identity(scope, event_type)
    name = field_def.name
    branch_id = event_type.branch_id

    relations = (
        (
            await scope.session.execute(
                select(EventTypeRelation).where(
                    EventTypeRelation.branch_id == branch_id,
                    or_(
                        EventTypeRelation.source_field_id == field_def.id,
                        EventTypeRelation.target_field_id == field_def.id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    bound = (
        (
            await scope.session.execute(
                select(Variable)
                .options(*VARIABLE_LOAD)
                .join(VariableValue, VariableValue.variable_id == Variable.id)
                .where(VariableValue.field_definition_id == field_def.id)
            )
        )
        .scalars()
        .all()
    )
    same_name = [
        v
        for v in await scope.branch_variables(branch_id)
        if name in {v.source_name, *(v.bindings or [])} and v.id not in {b.id for b in bound}
    ]
    events = (
        (
            await scope.session.execute(
                select(Event).options(*EVENT_LOAD).where(Event.event_type_id == event_type.id)
            )
        )
        .scalars()
        .all()
    )
    downstream: list[Edge] = [
        *await _relation_edges(scope, relations, RELATION_LINKS_FIELD),
        *(_variable_edge(scope, v, VARIABLE_BOUND_TO_FIELD) for v in bound),
        *(_variable_edge(scope, v, VARIABLE_BINDING_SAME_NAME, "possible") for v in same_name),
        *(
            _event_edge(scope, event, EVENT_BREAKDOWN_COLUMN)
            for event in events
            if name in (event.metric_breakdown_columns or [])
        ),
        *await _field_metric_edges(scope, field_def, type_ids),
        *(
            _fact_table_edge(scope, table, FACT_TABLE_SQL_MENTIONS, "possible")
            for table in await scope.fact_tables()
            if _fact_table_mentions(table, name)
        ),
    ]
    for scan in await scope.scan_configs():
        edge = _scan_column_edge(scope, scan, name, type_ids)
        if edge is not None:
            downstream.append(edge)

    values = (
        await scope.session.scalars(
            select(EventFieldValue.value).where(EventFieldValue.field_definition_id == field_def.id)
        )
    ).all()
    tokens = {token for value in values if value for token in variable_tokens(value)}
    upstream = [
        _type_edge(scope, event_type, FIELD_OF_TYPE),
        *(
            _variable_edge(scope, v, VARIABLE_IN_FIELD_VALUE)
            for v in await _variables_by_token(scope, branch_id, tokens)
        ),
    ]
    return upstream, downstream


# ── variables ───────────────────────────────────────────────────────────────


async def variable_deps(scope: Scope, variable: Variable) -> Pair:
    branch_id = variable.branch_id
    contexts = (
        await scope.session.execute(
            select(VariableValue.event_id, VariableValue.field_definition_id).where(
                VariableValue.variable_id == variable.id
            )
        )
    ).all()
    override_event_ids = set(
        (
            await scope.session.scalars(
                select(VariableEventValueOverride.event_id).where(
                    VariableEventValueOverride.variable_id == variable.id
                )
            )
        ).all()
    )
    names = set(tokens_of(variable))
    token_rows = (
        await scope.session.execute(
            select(
                EventFieldValue.event_id,
                EventFieldValue.field_definition_id,
                EventFieldValue.value,
            )
            .join(Event, EventFieldValue.event_id == Event.id)
            .where(
                Event.project_id == scope.project_id,
                Event.branch_id == branch_id,
                EventFieldValue.value.contains("${"),
            )
        )
    ).all()
    token_pairs = {
        (event_id, field_id)
        for event_id, field_id, value in token_rows
        if value and names.intersection(variable_tokens(value))
    }
    # Meta values carry ``${token}``s too (``variable_retirement_service``
    # reads both tables); they have no field, only the event.
    meta_rows = (
        await scope.session.execute(
            select(EventMetaValue.event_id, EventMetaValue.value)
            .join(Event, EventMetaValue.event_id == Event.id)
            .where(
                Event.project_id == scope.project_id,
                Event.branch_id == branch_id,
                EventMetaValue.value.contains("${"),
            )
        )
    ).all()
    meta_event_ids = {
        event_id
        for event_id, value in meta_rows
        if value and names.intersection(variable_tokens(value))
    }

    event_relation: dict[uuid.UUID, str] = {}
    field_relation: dict[uuid.UUID, str] = {}
    for event_id, field_id in contexts:
        event_relation.setdefault(event_id, VARIABLE_ON_EVENT)
        field_relation.setdefault(field_id, VARIABLE_BOUND_TO_FIELD)
    for event_id in override_event_ids:
        event_relation.setdefault(event_id, VARIABLE_OVERRIDE_ON_EVENT)
    for event_id, field_id in token_pairs:
        event_relation.setdefault(event_id, VARIABLE_IN_EVENT_VALUE)
        field_relation.setdefault(field_id, VARIABLE_IN_FIELD_VALUE)
    for event_id in meta_event_ids:
        event_relation.setdefault(event_id, VARIABLE_IN_META_VALUE)

    downstream: list[Edge] = []
    if event_relation:
        events = (
            (
                await scope.session.execute(
                    select(Event).options(*EVENT_LOAD).where(Event.id.in_(event_relation))
                )
            )
            .scalars()
            .all()
        )
        downstream.extend(_event_edge(scope, e, event_relation[e.id]) for e in events)
    if field_relation:
        rows = (
            await scope.session.execute(
                select(FieldDefinition, EventType.name)
                .join(EventType, FieldDefinition.event_type_id == EventType.id)
                .where(FieldDefinition.id.in_(field_relation))
            )
        ).all()
        downstream.extend(
            _field_edge(scope, f, type_name, field_relation[f.id]) for f, type_name in rows
        )
    return [], downstream
