"""One-hop resolvers: every edge into and out of one entity.

Each ``*_deps`` function answers for one kind and returns ``(upstream,
downstream)``. ``dependency_service`` picks the function, runs the second hop
and dedupes; nothing here writes, and nothing here refuses anything — the one
blocking dependency check stays ``fact_table_dependents`` (GH #257: warn only).

Two readings of an edge:

* ``direct`` — a stored id (a metric's ``numerator_event_id``, an alert
  filter's values, a relation's field ids, a variable context's field) or a
  column name read in the entity's OWN scope (an event's metric breakdown
  columns, a composition metric's breakdown over events of the field's type);
* ``possible`` — a name match without a scope tying it to this entity: a
  whole identifier inside free SQL (``_dependency_sql``), a fact-table column
  or a variable binding that happens to carry the field's name.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence

from sqlalchemy import or_, select

from tripl.core.name_template import variable_tokens
from tripl.core.variable_retirement import tokens_of
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
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
from tripl.schemas.dependency import DependencyCertainty
from tripl.services._dependency_locate import (
    EVENT_LOAD,
    TYPE_LOAD,
    VARIABLE_LOAD,
    Scope,
    event_identity,
    event_type_identity,
    locate_event,
    locate_event_type,
    locate_fact_table,
    locate_metric,
)
from tripl.services._dependency_model import Edge, url_for
from tripl.services.fact_table_dependents import metrics_depending_on

__all__ = [
    "alert_rule_deps",
    "event_deps",
    "event_type_deps",
    "fact_table_deps",
    "metric_deps",
    "orphan_deps",
    "relation_deps",
]

Pair = tuple[list[Edge], list[Edge]]

# The sentences an edge carries. One place, so the UI reads one vocabulary.
METRIC_USES_EVENT = "metric uses event in its composition"
METRIC_USES_EVENT_TYPE = "metric uses event type in its composition"
METRIC_READS_FACT_TABLE = "metric reads fact table"
METRIC_BREAKDOWN_COLUMN = "metric breakdown uses column"
METRIC_SAME_NAME_COLUMN = "fact metric reads a column with the same name"
METRIC_SQL_MENTIONS = "metric SQL mentions the column by name"
FACT_TABLE_SQL_MENTIONS = "fact table SQL or columns mention the column by name"
ALERT_ON_EVENT = "alert rule filters on event"
ALERT_ON_EVENT_TYPE = "alert rule filters on event type"
ALERT_ON_METRIC = "alert rule is scoped to metric"
ALERT_ON_SCAN = "alert rule is narrowed to scan"
EVENT_OF_TYPE = "event belongs to event type"
FIELD_OF_TYPE = "field belongs to event type"
EVENT_BREAKDOWN_COLUMN = "event metric breakdown uses column"
SCAN_BREAKDOWN_COLUMN = "scan metric breakdown uses column"
SCAN_BOUND_TO_TYPE = "scan is bound to event type"
RELATION_LINKS_TYPE = "relation links event type"
RELATION_LINKS_FIELD = "relation links field"
VARIABLE_BOUND_TO_FIELD = "variable bound to field"
VARIABLE_BINDING_SAME_NAME = "variable binding names a column with the same name"
VARIABLE_ON_EVENT = "variable observed on event"
VARIABLE_OVERRIDE_ON_EVENT = "event overrides variable values"
VARIABLE_IN_EVENT_VALUE = "event uses variable in a field value"
VARIABLE_IN_FIELD_VALUE = "field value uses variable"
VARIABLE_IN_META_VALUE = "event uses variable in a meta value"
EVENT_SUPERSEDED_BY = "superseded by"
EVENT_SUPERSEDES = "supersedes"
DETECTION_OVERRIDE_EVENT = "detection sensitivity override on event"
DETECTION_OVERRIDE_EVENT_TYPE = "detection sensitivity override on event type"
DETECTION_OVERRIDE_METRIC = "detection sensitivity override on metric"


# ── edge builders ───────────────────────────────────────────────────────────


def _event_edge(scope: Scope, event: Event, relation: str) -> Edge:
    type_name = event.event_type.name if event.event_type is not None else None
    return Edge(
        kind="event",
        id=event.id,
        name=event.name,
        relation=relation,
        url_hint=url_for(scope.slug, "event", event.id, event_type_name=type_name),
    )


def _type_edge(scope: Scope, event_type: EventType, relation: str) -> Edge:
    return Edge(
        kind="event_type",
        id=event_type.id,
        name=event_type.display_name or event_type.name,
        relation=relation,
        url_hint=url_for(scope.slug, "event_type", event_type.id),
    )


def _field_edge(
    scope: Scope,
    field_def: FieldDefinition,
    type_name: str,
    relation: str,
    certainty: DependencyCertainty = "direct",
) -> Edge:
    return Edge(
        kind="field",
        id=field_def.id,
        name=f"{type_name}.{field_def.name}",
        relation=relation,
        certainty=certainty,
        url_hint=url_for(scope.slug, "field", field_def.id, event_type_id=field_def.event_type_id),
    )


def _variable_edge(
    scope: Scope, variable: Variable, relation: str, certainty: DependencyCertainty = "direct"
) -> Edge:
    return Edge(
        kind="variable",
        id=variable.id,
        name=variable.name,
        relation=relation,
        certainty=certainty,
        url_hint=url_for(scope.slug, "variable", variable.id),
    )


def _metric_edge(
    scope: Scope, metric: MetricDefinition, relation: str, certainty: DependencyCertainty = "direct"
) -> Edge:
    return Edge(
        kind="metric",
        id=metric.id,
        name=metric.display_name or metric.name,
        relation=relation,
        certainty=certainty,
        url_hint=url_for(scope.slug, "metric", metric.id),
    )


def _fact_table_edge(
    scope: Scope, table: FactTable, relation: str, certainty: DependencyCertainty = "direct"
) -> Edge:
    return Edge(
        kind="fact_table",
        id=table.id,
        name=table.display_name or table.name,
        relation=relation,
        certainty=certainty,
        url_hint=url_for(scope.slug, "fact_table", table.id),
    )


def _alert_edge(scope: Scope, rule: AlertRule, relation: str) -> Edge:
    return Edge(
        kind="alert_rule",
        id=rule.id,
        name=rule.name,
        relation=relation,
        url_hint=url_for(scope.slug, "alert_rule", rule.id),
    )


def _scan_edge(
    scope: Scope, scan: ScanConfig, relation: str, certainty: DependencyCertainty = "direct"
) -> Edge:
    return Edge(
        kind="scan_config",
        id=scan.id,
        name=scan.name,
        relation=relation,
        certainty=certainty,
        url_hint=url_for(scope.slug, "scan_config", scan.id),
    )


def _override_edge(scope: Scope, override: AnomalyScopeOverride, relation: str) -> Edge:
    return Edge(
        kind="detection_override",
        id=override.id,
        name=override.scope_name or f"{override.scope_type} {override.scope_ref}",
        relation=relation,
        url_hint=url_for(scope.slug, "detection_override", override.id),
    )


async def _relation_edges(
    scope: Scope, relations: Sequence[EventTypeRelation], relation: str
) -> list[Edge]:
    """Relation edges named ``type.field → type.field``, in two bulk reads."""
    if not relations:
        return []
    type_ids = {r.source_event_type_id for r in relations} | {
        r.target_event_type_id for r in relations
    }
    field_ids = {r.source_field_id for r in relations} | {r.target_field_id for r in relations}
    type_names: dict[uuid.UUID, str] = {
        type_id: name
        for type_id, name in (
            await scope.session.execute(
                select(EventType.id, EventType.name).where(EventType.id.in_(type_ids))
            )
        ).all()
    }
    field_names: dict[uuid.UUID, str] = {
        field_id: name
        for field_id, name in (
            await scope.session.execute(
                select(FieldDefinition.id, FieldDefinition.name).where(
                    FieldDefinition.id.in_(field_ids)
                )
            )
        ).all()
    }
    edges: list[Edge] = []
    for rel in relations:
        source_type = type_names.get(rel.source_event_type_id, "?")
        target_type = type_names.get(rel.target_event_type_id, "?")
        source = f"{source_type}.{field_names.get(rel.source_field_id, '?')}"
        target = f"{target_type}.{field_names.get(rel.target_field_id, '?')}"
        edges.append(
            Edge(
                kind="relation",
                id=rel.id,
                name=f"{source} → {target}",
                relation=relation,
                url_hint=url_for(scope.slug, "relation", rel.id),
            )
        )
    return edges


# ── shared project-wide matches ─────────────────────────────────────────────


def _is_filter(flt: AlertRuleFilter, field_name: str) -> bool:
    return str(getattr(flt.field, "value", flt.field)) == field_name


async def _alerts_naming(
    scope: Scope, field_name: str, ids: Iterable[uuid.UUID], relation: str
) -> list[Edge]:
    """Alert rules whose ``field_name`` filter lists any of ``ids``, any operator.

    An exclusion (``ne`` / ``not_in``) is a dependency too: the delete path
    rewrites or retires it (``_event_reference_cleanup``), so the rule changes.
    """
    wanted = {str(value) for value in ids}
    return [
        _alert_edge(scope, rule, relation)
        for rule, flt in await scope.alert_filters()
        if _is_filter(flt, field_name) and wanted.intersection(str(v) for v in flt.values or [])
    ]


async def _overrides_on(
    scope: Scope, scope_type: str, ids: Iterable[uuid.UUID], relation: str
) -> list[Edge]:
    """Detection overrides keyed by any of ``ids`` (``scope_ref`` is the id as text).

    Deleting the entity leaves its override behind in Detection settings, and
    a renamed or re-created one no longer inherits it — worth a warning either way.
    """
    refs = {str(value) for value in ids}
    return [
        _override_edge(scope, override, relation)
        for override in await scope.detection_overrides()
        if str(getattr(override.scope_type, "value", override.scope_type)) == scope_type
        and override.scope_ref in refs
    ]


async def _metrics_using_events(scope: Scope, ids: set[uuid.UUID]) -> list[Edge]:
    return [
        _metric_edge(scope, metric, METRIC_USES_EVENT)
        for metric in await scope.metrics()
        if metric.numerator_event_id in ids or metric.denominator_event_id in ids
    ]


async def _metrics_using_types(scope: Scope, ids: set[uuid.UUID]) -> list[Edge]:
    return [
        _metric_edge(scope, metric, METRIC_USES_EVENT_TYPE)
        for metric in await scope.metrics()
        if metric.numerator_event_type_id in ids or metric.denominator_event_type_id in ids
    ]


async def _metrics_reading_table(scope: Scope, fact_table_id: uuid.UUID) -> list[Edge]:
    # The existing definition of "reads this fact table" — the one the 409
    # guard uses — rather than a second copy that could disagree with it.
    metrics = await metrics_depending_on(
        scope.session, project_id=scope.project_id, fact_table_id=fact_table_id
    )
    return [_metric_edge(scope, metric, METRIC_READS_FACT_TABLE) for metric in metrics]


async def orphan_deps(scope: Scope, kind: str, entity_id: uuid.UUID) -> Pair:
    """What still names an id that resolves to no row (deleted, or never was).

    Only the project-wide references can dangle — branch rows cascade with
    their parent — so only those are read, by the raw id.
    """
    ids = {entity_id}
    match kind:
        case "event":
            return [], [
                *await _metrics_using_events(scope, ids),
                *await _alerts_naming(scope, "event", ids, ALERT_ON_EVENT),
                *await _overrides_on(scope, "event", ids, DETECTION_OVERRIDE_EVENT),
            ]
        case "event_type":
            return [], [
                *await _metrics_using_types(scope, ids),
                *await _alerts_naming(scope, "event_type", ids, ALERT_ON_EVENT_TYPE),
                *await _overrides_on(scope, "event_type", ids, DETECTION_OVERRIDE_EVENT_TYPE),
            ]
        case "metric":
            return [], [
                *await _alerts_naming(scope, "metric", ids, ALERT_ON_METRIC),
                *await _overrides_on(scope, "metric", ids, DETECTION_OVERRIDE_METRIC),
            ]
        case "fact_table":
            return [], await _metrics_reading_table(scope, entity_id)
    return [], []


# ── events ──────────────────────────────────────────────────────────────────


async def _variables_by_token(
    scope: Scope, branch_id: uuid.UUID, tokens: set[str]
) -> list[Variable]:
    if not tokens:
        return []
    return [
        variable
        for variable in await scope.branch_variables(branch_id)
        if tokens.intersection(tokens_of(variable))
    ]


def _tokens(values: Iterable[str | None]) -> set[str]:
    return {token for value in values if value for token in variable_tokens(value)}


async def _supersession_edges(scope: Scope, event: Event) -> Pair:
    """The successor this event points at (upstream) and the events pointing at it.

    Deleting a successor clears its predecessors' pointer (``SET NULL``), so
    the predecessors are this event's downstream; the successor itself does
    not depend on the event it replaced.
    """
    upstream: list[Edge] = []
    if event.superseded_by_event_id is not None:
        # ``locate_event`` would hop a main id over to its branch copy; a
        # stored pointer names the exact row, so it is read as is.
        successor = await scope.event_by_id(event.superseded_by_event_id)
        if successor is not None:
            upstream.append(_event_edge(scope, successor, EVENT_SUPERSEDED_BY))
    predecessors = (
        (
            await scope.session.execute(
                select(Event)
                .options(*EVENT_LOAD)
                .where(
                    Event.project_id == scope.project_id,
                    Event.superseded_by_event_id == event.id,
                )
            )
        )
        .scalars()
        .all()
    )
    return upstream, [_event_edge(scope, p, EVENT_SUPERSEDES) for p in predecessors]


async def event_deps(scope: Scope, event: Event) -> Pair:
    ids = await event_identity(scope, event)
    successor, predecessors = await _supersession_edges(scope, event)
    downstream = [
        *await _metrics_using_events(scope, ids),
        *await _alerts_naming(scope, "event", ids, ALERT_ON_EVENT),
        *await _overrides_on(scope, "event", ids, DETECTION_OVERRIDE_EVENT),
        *predecessors,
    ]

    upstream: list[Edge] = [*successor]
    if event.event_type is not None:
        upstream.append(_type_edge(scope, event.event_type, EVENT_OF_TYPE))
        breakdown = {c for c in event.metric_breakdown_columns or [] if isinstance(c, str)}
        if breakdown:
            fields = (
                (
                    await scope.session.execute(
                        select(FieldDefinition).where(
                            FieldDefinition.event_type_id == event.event_type_id,
                            FieldDefinition.name.in_(breakdown),
                        )
                    )
                )
                .scalars()
                .all()
            )
            upstream.extend(
                _field_edge(scope, f, event.event_type.name, EVENT_BREAKDOWN_COLUMN) for f in fields
            )

    # Both value tables, as ``variable_retirement_service`` reads them: a
    # ``${token}`` is legal in a field value and in a meta value alike.
    field_values = (
        await scope.session.scalars(
            select(EventFieldValue.value).where(EventFieldValue.event_id == event.id)
        )
    ).all()
    meta_values = (
        await scope.session.scalars(
            select(EventMetaValue.value).where(EventMetaValue.event_id == event.id)
        )
    ).all()
    upstream.extend(
        _variable_edge(scope, v, VARIABLE_IN_EVENT_VALUE)
        for v in await _variables_by_token(scope, event.branch_id, _tokens(field_values))
    )
    upstream.extend(
        _variable_edge(scope, v, VARIABLE_IN_META_VALUE)
        for v in await _variables_by_token(scope, event.branch_id, _tokens(meta_values))
    )
    observed = (
        (
            await scope.session.execute(
                select(Variable)
                .options(*VARIABLE_LOAD)
                .join(VariableValue, VariableValue.variable_id == Variable.id)
                .where(VariableValue.event_id == event.id)
            )
        )
        .scalars()
        .all()
    )
    upstream.extend(_variable_edge(scope, v, VARIABLE_ON_EVENT) for v in observed)
    overridden = (
        (
            await scope.session.execute(
                select(Variable)
                .options(*VARIABLE_LOAD)
                .join(
                    VariableEventValueOverride,
                    VariableEventValueOverride.variable_id == Variable.id,
                )
                .where(VariableEventValueOverride.event_id == event.id)
            )
        )
        .scalars()
        .all()
    )
    upstream.extend(_variable_edge(scope, v, VARIABLE_OVERRIDE_ON_EVENT) for v in overridden)
    return upstream, downstream


# ── event types ─────────────────────────────────────────────────────────────


async def event_type_deps(scope: Scope, event_type: EventType) -> Pair:
    ids = await event_type_identity(scope, event_type)
    events = (
        (
            await scope.session.execute(
                select(Event).options(*EVENT_LOAD).where(Event.event_type_id == event_type.id)
            )
        )
        .scalars()
        .all()
    )
    relations = (
        (
            await scope.session.execute(
                select(EventTypeRelation).where(
                    EventTypeRelation.branch_id == event_type.branch_id,
                    or_(
                        EventTypeRelation.source_event_type_id == event_type.id,
                        EventTypeRelation.target_event_type_id == event_type.id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    downstream = [
        *(_event_edge(scope, event, EVENT_OF_TYPE) for event in events),
        *await _metrics_using_types(scope, ids),
        *await _alerts_naming(scope, "event_type", ids, ALERT_ON_EVENT_TYPE),
        *await _relation_edges(scope, relations, RELATION_LINKS_TYPE),
        *(
            _scan_edge(scope, scan, SCAN_BOUND_TO_TYPE)
            for scan in await scope.scan_configs()
            if scan.event_type_id in ids
        ),
        *await _overrides_on(scope, "event_type", ids, DETECTION_OVERRIDE_EVENT_TYPE),
    ]
    return [], downstream


# ── metrics, fact tables, alert rules, relations ────────────────────────────


def _present(*ids: uuid.UUID | None) -> list[uuid.UUID]:
    return list(dict.fromkeys(i for i in ids if i is not None))


async def metric_deps(scope: Scope, metric: MetricDefinition) -> Pair:
    # Function-local for the reason ``fact_table_dependents`` gives: keep the
    # metrics service out of this module's import graph.
    from tripl.services.metric_definition_service import _metric_fact_table_ids

    upstream: list[Edge] = []
    for event_id in _present(metric.numerator_event_id, metric.denominator_event_id):
        event = await locate_event(scope, event_id)
        if event is not None:
            upstream.append(_event_edge(scope, event, METRIC_USES_EVENT))
    for type_id in _present(metric.numerator_event_type_id, metric.denominator_event_type_id):
        event_type = await locate_event_type(scope, type_id)
        if event_type is not None:
            upstream.append(_type_edge(scope, event_type, METRIC_USES_EVENT_TYPE))
    for table_id in _metric_fact_table_ids(metric):
        table = await locate_fact_table(scope, table_id)
        if table is not None:
            upstream.append(_fact_table_edge(scope, table, METRIC_READS_FACT_TABLE))
    downstream = [
        *await _alerts_naming(scope, "metric", {metric.id}, ALERT_ON_METRIC),
        *await _overrides_on(scope, "metric", {metric.id}, DETECTION_OVERRIDE_METRIC),
    ]
    return upstream, downstream


async def fact_table_deps(scope: Scope, table: FactTable) -> Pair:
    return [], await _metrics_reading_table(scope, table.id)


def _uuids(values: Iterable[object]) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    for value in values:
        try:
            found.append(uuid.UUID(str(value)))
        except ValueError:
            continue
    return found


async def alert_rule_deps(scope: Scope, rule: AlertRule) -> Pair:
    filters = (
        (
            await scope.session.execute(
                select(AlertRuleFilter).where(AlertRuleFilter.rule_id == rule.id)
            )
        )
        .scalars()
        .all()
    )
    upstream: list[Edge] = []
    for flt in filters:
        values = _uuids(flt.values or [])
        if _is_filter(flt, "event"):
            for event_id in values:
                event = await locate_event(scope, event_id)
                if event is not None:
                    upstream.append(_event_edge(scope, event, ALERT_ON_EVENT))
        elif _is_filter(flt, "event_type"):
            for type_id in values:
                event_type = await locate_event_type(scope, type_id)
                if event_type is not None:
                    upstream.append(_type_edge(scope, event_type, ALERT_ON_EVENT_TYPE))
        elif _is_filter(flt, "metric"):
            for metric_id in values:
                metric = await locate_metric(scope, metric_id)
                if metric is not None:
                    upstream.append(_metric_edge(scope, metric, ALERT_ON_METRIC))
    if rule.scan_config_id is not None:
        scan = next((s for s in await scope.scan_configs() if s.id == rule.scan_config_id), None)
        if scan is not None:
            upstream.append(_scan_edge(scope, scan, ALERT_ON_SCAN))
    return upstream, []


async def relation_deps(scope: Scope, relation: EventTypeRelation) -> Pair:
    types = {
        t.id: t
        for t in (
            await scope.session.execute(
                select(EventType)
                .options(*TYPE_LOAD)
                .where(
                    EventType.id.in_({relation.source_event_type_id, relation.target_event_type_id})
                )
            )
        )
        .scalars()
        .all()
    }
    fields = (
        (
            await scope.session.execute(
                select(FieldDefinition).where(
                    FieldDefinition.id.in_({relation.source_field_id, relation.target_field_id})
                )
            )
        )
        .scalars()
        .all()
    )
    upstream: list[Edge] = [_type_edge(scope, t, RELATION_LINKS_TYPE) for t in types.values()]
    for f in fields:
        owner = types.get(f.event_type_id)
        upstream.append(_field_edge(scope, f, owner.name if owner else "?", RELATION_LINKS_FIELD))
    return upstream, []
