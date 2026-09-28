"""Find the asked-about row on the right branch, and every id that means it.

Plan rows (events, event types, fields, variables, relations and the variable
contexts) are copied per branch under NEW ids; metrics, fact tables, alert
rules and scan configurations are project-wide and store MAIN ids. So a
dependency question about a branch row has two halves: the branch-scoped
references are read on the branch itself, and the project-wide ones are matched
against the row's main twin as well as its own id.

The twin rules are the ones the rest of the branch code already uses:

* an event pairs through ``origin_id`` first and then the scan identity, via
  ``_branch_counterparts.main_counterparts``;
* a relation pairs through its own ``origin_id``;
* event types, fields and variables carry no ``origin_id`` and pair by their
  unique natural key (type name; type name + field name; variable name).

Nothing here writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload, selectinload

from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_filter import AlertRuleFilter
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.fact_table import FactTable
from tripl.models.field_definition import FieldDefinition
from tripl.models.metric_definition import MetricDefinition
from tripl.models.scan_config import ScanConfig
from tripl.models.variable import Variable
from tripl.services._branch_counterparts import main_counterparts

__all__ = [
    "EVENT_LOAD",
    "TYPE_LOAD",
    "VARIABLE_LOAD",
    "Scope",
    "event_identity",
    "event_type_identity",
    "locate_alert_rule",
    "locate_event",
    "locate_event_type",
    "locate_fact_table",
    "locate_field",
    "locate_metric",
    "locate_relation",
    "locate_variable",
]


# The plan models eager-load their children (``lazy="selectin"``): an event its
# field values, meta values and tags, an event type every field, a variable
# every value context. None of that is read here, and an event type's events
# or a project's variables can run to hundreds of rows, so the dependency reads
# switch those loads off. Only an event's own type is kept — edge names and
# links need it.
EVENT_LOAD = (
    selectinload(Event.event_type).lazyload(EventType.field_definitions),
    lazyload(Event.field_values),
    lazyload(Event.meta_values),
    lazyload(Event.tags),
)
TYPE_LOAD = (lazyload(EventType.field_definitions),)
VARIABLE_LOAD = (lazyload(Variable.value_contexts), lazyload(Variable.event_overrides))


@dataclass(slots=True)
class Scope:
    """One dependency request's session, project, branch and read cache.

    The project-wide tables are read once per request and kept here, so a
    two-hop walk (or a 200-change impact batch) does not re-read every metric
    for every neighbour. The per-branch lookups are cached too: a branch's
    variables, event types by name, events by id and each branch event's main
    twin — ``prime_events`` loads a whole neighbour list in two queries before
    the per-neighbour resolvers run.

    ``expansions_left`` is the request's second-hop budget (see
    ``dependency_service``): shared by the upstream and downstream walks, so
    one request never expands more than that many neighbours in total.
    """

    session: AsyncSession
    project_id: uuid.UUID
    branch_id: uuid.UUID
    main_branch_id: uuid.UUID
    slug: str | None = None
    # The project's organization slug; with ``slug`` it builds ``url_hint``s.
    org_slug: str | None = None
    expansions_left: int = 0
    _metrics: list[MetricDefinition] | None = None
    _fact_tables: list[FactTable] | None = None
    _scan_configs: list[ScanConfig] | None = None
    _alert_filters: list[tuple[AlertRule, AlertRuleFilter]] | None = None
    _overrides: list[AnomalyScopeOverride] | None = None
    _variables: dict[uuid.UUID, list[Variable]] = field(default_factory=dict)
    _types_by_name: dict[tuple[uuid.UUID, str], EventType | None] = field(default_factory=dict)
    _events: dict[uuid.UUID, Event] = field(default_factory=dict)
    # Branch event id → its main twin's id (None: no twin). Main rows absent.
    _twins: dict[uuid.UUID, uuid.UUID | None] = field(default_factory=dict)

    @property
    def on_main(self) -> bool:
        return self.branch_id == self.main_branch_id

    async def metrics(self) -> list[MetricDefinition]:
        if self._metrics is None:
            self._metrics = list(
                (
                    await self.session.execute(
                        select(MetricDefinition).where(
                            MetricDefinition.project_id == self.project_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return self._metrics

    async def fact_tables(self) -> list[FactTable]:
        if self._fact_tables is None:
            self._fact_tables = list(
                (
                    await self.session.execute(
                        select(FactTable).where(FactTable.project_id == self.project_id)
                    )
                )
                .scalars()
                .all()
            )
        return self._fact_tables

    async def scan_configs(self) -> list[ScanConfig]:
        if self._scan_configs is None:
            self._scan_configs = list(
                (
                    await self.session.execute(
                        select(ScanConfig).where(ScanConfig.project_id == self.project_id)
                    )
                )
                .scalars()
                .all()
            )
        return self._scan_configs

    async def alert_filters(self) -> list[tuple[AlertRule, AlertRuleFilter]]:
        """Every (rule, filter) pair in the project, scoped through the destination."""
        if self._alert_filters is None:
            rows = (
                await self.session.execute(
                    select(AlertRule, AlertRuleFilter)
                    .join(AlertRuleFilter, AlertRuleFilter.rule_id == AlertRule.id)
                    .join(AlertDestination, AlertRule.destination_id == AlertDestination.id)
                    .where(AlertDestination.project_id == self.project_id)
                )
            ).all()
            self._alert_filters = [(rule, flt) for rule, flt in rows]
        return self._alert_filters

    async def detection_overrides(self) -> list[AnomalyScopeOverride]:
        """Every per-scope detection override in the project (usually a handful)."""
        if self._overrides is None:
            self._overrides = list(
                (
                    await self.session.execute(
                        select(AnomalyScopeOverride).where(
                            AnomalyScopeOverride.project_id == self.project_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return self._overrides

    async def branch_variables(self, branch_id: uuid.UUID) -> list[Variable]:
        """Every variable on ``branch_id`` in the project, read once per request."""
        cached = self._variables.get(branch_id)
        if cached is None:
            cached = list(
                (
                    await self.session.execute(
                        select(Variable)
                        .options(*VARIABLE_LOAD)
                        .where(
                            Variable.project_id == self.project_id,
                            Variable.branch_id == branch_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            self._variables[branch_id] = cached
        return cached

    async def prime_events(self, event_ids: Iterable[uuid.UUID]) -> None:
        """Load ``event_ids`` (this project only) and their main twins in bulk.

        ``main_counterparts`` takes the whole list, so a neighbour list costs
        a fixed number of queries instead of a few per neighbour.
        """
        asked = list(dict.fromkeys(event_ids))
        wanted = {event_id for event_id in asked if event_id not in self._events}
        if wanted:
            rows = (
                (
                    await self.session.execute(
                        select(Event)
                        .options(*EVENT_LOAD)
                        .where(Event.id.in_(wanted), Event.project_id == self.project_id)
                    )
                )
                .scalars()
                .all()
            )
            for event in rows:
                self._events[event.id] = event
        await self._prime_twins([self._events[i] for i in asked if i in self._events])

    async def event_by_id(self, event_id: uuid.UUID) -> Event | None:
        """The exact event row (this project only), no branch hop — a stored pointer."""
        await self.prime_events([event_id])
        return self._events.get(event_id)

    async def _prime_twins(self, events: Iterable[Event]) -> None:
        branch_events = [
            event
            for event in events
            if event.branch_id != self.main_branch_id and event.id not in self._twins
        ]
        if not branch_events:
            return
        twins = await main_counterparts(
            self.session, project_id=self.project_id, events=branch_events
        )
        for event in branch_events:
            twin = twins.get(event.id)
            self._twins[event.id] = twin.id if twin is not None else None

    async def twin_id(self, event: Event) -> uuid.UUID | None:
        """The main twin of a branch ``event`` (None on main or when unpaired)."""
        if event.branch_id == self.main_branch_id:
            return None
        if event.id not in self._twins:
            await self._prime_twins([event])
        return self._twins.get(event.id)


# ── events ──────────────────────────────────────────────────────────────────


async def _event_in_project(scope: Scope, event_id: uuid.UUID) -> Event | None:
    cached = scope._events.get(event_id)
    if cached is not None:
        return cached
    event = await scope.session.scalar(
        select(Event)
        .options(*EVENT_LOAD)
        .where(Event.id == event_id, Event.project_id == scope.project_id)
    )
    if event is not None:
        scope._events[event.id] = event
    return event


async def locate_event(scope: Scope, event_id: uuid.UUID) -> Event | None:
    """The event on the asked branch: the row itself, or its copy there.

    A MAIN id asked about on a branch answers with the branch copy (its
    ``origin_id``), so a link from a metric page into a branch lands on the
    branch's own row. A row deleted on the branch answers with the main row it
    came from, which is what a "what did deleting this touch" question needs.
    """
    event = await _event_in_project(scope, event_id)
    if event is None:
        return None
    if event.branch_id == scope.main_branch_id and not scope.on_main:
        copy = await scope.session.scalar(
            select(Event)
            .options(*EVENT_LOAD)
            .where(Event.branch_id == scope.branch_id, Event.origin_id == event.id)
        )
        if copy is not None:
            return copy
    return event


async def event_identity(scope: Scope, event: Event) -> set[uuid.UUID]:
    """The ids a project-wide reference to ``event`` may be stored under."""
    ids = {event.id}
    if event.branch_id != scope.main_branch_id:
        if event.origin_id is not None:
            ids.add(event.origin_id)
        twin_id = await scope.twin_id(event)
        if twin_id is not None:
            ids.add(twin_id)
    return ids


# ── event types ─────────────────────────────────────────────────────────────


async def _type_named(scope: Scope, branch_id: uuid.UUID, name: str) -> EventType | None:
    key = (branch_id, name)
    if key not in scope._types_by_name:
        scope._types_by_name[key] = await scope.session.scalar(
            select(EventType)
            .options(*TYPE_LOAD)
            .where(
                EventType.project_id == scope.project_id,
                EventType.branch_id == branch_id,
                EventType.name == name,
            )
        )
    return scope._types_by_name[key]


async def locate_event_type(scope: Scope, event_type_id: uuid.UUID) -> EventType | None:
    event_type = await scope.session.scalar(
        select(EventType)
        .options(*TYPE_LOAD)
        .where(EventType.id == event_type_id, EventType.project_id == scope.project_id)
    )
    if event_type is None:
        return None
    if event_type.branch_id == scope.main_branch_id and not scope.on_main:
        copy = await _type_named(scope, scope.branch_id, event_type.name)
        if copy is not None:
            return copy
    return event_type


async def event_type_identity(scope: Scope, event_type: EventType) -> set[uuid.UUID]:
    ids = {event_type.id}
    if event_type.branch_id != scope.main_branch_id:
        twin = await _type_named(scope, scope.main_branch_id, event_type.name)
        if twin is not None:
            ids.add(twin.id)
    return ids


# ── fields ──────────────────────────────────────────────────────────────────


async def _field_named(
    scope: Scope, branch_id: uuid.UUID, type_name: str, field_name: str
) -> tuple[FieldDefinition, EventType] | None:
    row = (
        await scope.session.execute(
            select(FieldDefinition, EventType)
            .options(*TYPE_LOAD)
            .join(EventType, FieldDefinition.event_type_id == EventType.id)
            .where(
                EventType.project_id == scope.project_id,
                EventType.branch_id == branch_id,
                EventType.name == type_name,
                FieldDefinition.name == field_name,
            )
        )
    ).first()
    return None if row is None else (row[0], row[1])


async def locate_field(
    scope: Scope, field_id: uuid.UUID
) -> tuple[FieldDefinition, EventType] | None:
    """The field and its event type on the asked branch."""
    row = (
        await scope.session.execute(
            select(FieldDefinition, EventType)
            .options(*TYPE_LOAD)
            .join(EventType, FieldDefinition.event_type_id == EventType.id)
            .where(FieldDefinition.id == field_id, EventType.project_id == scope.project_id)
        )
    ).first()
    if row is None:
        return None
    field_def, event_type = row[0], row[1]
    if event_type.branch_id == scope.main_branch_id and not scope.on_main:
        copy = await _field_named(scope, scope.branch_id, event_type.name, field_def.name)
        if copy is not None:
            return copy
    return field_def, event_type


# ── variables and relations ─────────────────────────────────────────────────


async def locate_variable(scope: Scope, variable_id: uuid.UUID) -> Variable | None:
    variable = await scope.session.scalar(
        select(Variable)
        .options(*VARIABLE_LOAD)
        .where(Variable.id == variable_id, Variable.project_id == scope.project_id)
    )
    if variable is None:
        return None
    if variable.branch_id == scope.main_branch_id and not scope.on_main:
        copy = await scope.session.scalar(
            select(Variable)
            .options(*VARIABLE_LOAD)
            .where(
                Variable.project_id == scope.project_id,
                Variable.branch_id == scope.branch_id,
                Variable.name == variable.name,
            )
        )
        if copy is not None:
            return copy
    return variable


async def locate_relation(scope: Scope, relation_id: uuid.UUID) -> EventTypeRelation | None:
    relation = await scope.session.scalar(
        select(EventTypeRelation).where(
            EventTypeRelation.id == relation_id,
            EventTypeRelation.project_id == scope.project_id,
        )
    )
    if relation is None:
        return None
    if relation.branch_id == scope.main_branch_id and not scope.on_main:
        copy = await scope.session.scalar(
            select(EventTypeRelation).where(
                EventTypeRelation.branch_id == scope.branch_id,
                EventTypeRelation.origin_id == relation.id,
            )
        )
        if copy is not None:
            return copy
    return relation


# ── project-wide rows ───────────────────────────────────────────────────────


async def locate_metric(scope: Scope, metric_id: uuid.UUID) -> MetricDefinition | None:
    return next((m for m in await scope.metrics() if m.id == metric_id), None)


async def locate_fact_table(scope: Scope, fact_table_id: uuid.UUID) -> FactTable | None:
    return next((t for t in await scope.fact_tables() if t.id == fact_table_id), None)


async def locate_alert_rule(scope: Scope, rule_id: uuid.UUID) -> AlertRule | None:
    rule: AlertRule | None = await scope.session.scalar(
        select(AlertRule)
        .join(AlertDestination, AlertRule.destination_id == AlertDestination.id)
        .where(AlertRule.id == rule_id, AlertDestination.project_id == scope.project_id)
    )
    return rule
