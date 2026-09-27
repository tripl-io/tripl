"""Event health: batched fact loading and aggregation (F15, #268).

The scored population is the MAIN plan's events whose status is not
``archived``: scans write metrics, drifts and lifecycle findings only for main
rows, so a branch copy has nothing of its own to score.

``load_facts`` builds :class:`~tripl.services.health_score.EventHealthFacts`
for any number of events in a fixed number of flat queries (ids chunked per
``IN`` list), and ``score_event`` turns each into a score. Everything else here
is lookup and aggregation for the routes, the catalog sort and the daily
snapshot.

"Covering scan configs" of an event, shared by freshness, drifts and signals:
the configs that wrote a metric for it in the last ``COVERAGE_LOOKBACK_DAYS``
days, plus the configs bound to its event type.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl import cache
from tripl.core.bucketing import to_utc
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import DistributionDriftBand
from tripl.models.event import Event, EventStatus
from tripl.models.event_metric import EventMetric
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.field_definition import FieldDefinition
from tripl.models.lifecycle_finding import LifecycleFinding
from tripl.models.project_health_snapshot import ProjectHealthSnapshot
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.models.variable_value_drift import VariableValueDrift
from tripl.schemas.health import (
    ComponentAverage,
    EventHealth,
    EventHealthBrief,
    EventHealthListResponse,
    EventTypeHealth,
    ProjectHealthResponse,
    ProjectHealthTrendPoint,
)
from tripl.services import health_weights as hw
from tripl.services import schema_drift_service, variable_value_drift_service
from tripl.services._id_chunks import chunked
from tripl.services._open_event_signals import open_unverdicted_event_signals
from tripl.services.health_score import EventHealthFacts, grade_for, score_event
from tripl.services.health_weights import HealthComponentKey
from tripl.services.plan_branch_service import ensure_main_branch_id
from tripl.services.project_lookup import resolve_project_id
from tripl.services.scan_config_service import project_settling_delay
from tripl.services.source_freshness import compute_freshness

EVENT_NOT_FOUND = "Event not found"
PROJECT_HEALTH_CACHE_TTL_SECONDS = 120

# Drift types counted by the "drifts" component. The contract kinds are scored
# by the "contract" component and deliberately not counted twice.
STRUCTURAL_DRIFT_TYPES: frozenset[str] = frozenset({"new_field", "missing_field", "type_changed"})


@dataclass(frozen=True)
class EventHealthRow:
    """The event columns the score reads — no ORM entity, no relationships."""

    id: uuid.UUID
    name: str
    event_type_id: uuid.UUID
    status: str
    description: str | None
    owner_id: uuid.UUID | None
    last_seen_at: datetime | None


_ROW_COLUMNS = (
    Event.id,
    Event.name,
    Event.event_type_id,
    Event.status,
    Event.description,
    Event.owner_id,
    Event.last_seen_at,
)


@dataclass(frozen=True)
class _Config:
    id: uuid.UUID
    name: str
    event_type_id: uuid.UUID | None
    interval: str | None
    time_column: str | None
    last_event_at: datetime | None
    last_collection_at: datetime | None
    anomaly_detection_enabled: bool


def _row(values: Sequence[Any]) -> EventHealthRow:
    event_id, name, event_type_id, status, description, owner_id, last_seen_at = values
    return EventHealthRow(
        id=event_id,
        name=name,
        event_type_id=event_type_id,
        status=str(status),
        description=description,
        owner_id=owner_id,
        last_seen_at=last_seen_at,
    )


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


# --------------------------------------------------------------------------- #
# Fact loading
# --------------------------------------------------------------------------- #


def _contract_expectations(
    rows: Iterable[Sequence[Any]],
) -> dict[uuid.UUID, list[tuple[str, str]]]:
    """``(field_name, drift_type)`` per event type, the set the scanner checks.

    Mirrors ``worker.tasks.metrics.schema_drift._field_contract_expectations``
    minus its "column observed in this run" filter, which only a scan knows.
    """
    out: dict[uuid.UUID, list[tuple[str, str]]] = defaultdict(list)
    for (
        event_type_id,
        name,
        field_type,
        is_required,
        enum_options,
        contract_regex,
        contract_min,
        contract_max,
    ) in rows:
        expectations = out[event_type_id]
        if is_required:
            expectations.append((name, "required_null_violation"))
        if _enum_value(field_type) == "enum" and enum_options:
            expectations.append((name, "enum_violation"))
        if contract_regex:
            expectations.append((name, "regex_violation"))
        if contract_min is not None or contract_max is not None:
            expectations.append((name, "range_violation"))
    return out


async def _load_configs(session: AsyncSession, project_id: uuid.UUID) -> list[_Config]:
    result = await session.execute(
        select(
            ScanConfig.id,
            ScanConfig.name,
            ScanConfig.event_type_id,
            ScanConfig.interval,
            ScanConfig.time_column,
            ScanConfig.last_event_at,
            ScanConfig.last_collection_at,
            ScanConfig.anomaly_detection_enabled,
        ).where(ScanConfig.project_id == project_id)
    )
    return [
        _Config(
            id=row[0],
            name=row[1],
            event_type_id=row[2],
            interval=None if row[3] is None else _enum_value(row[3]),
            time_column=row[4],
            last_event_at=row[5],
            last_collection_at=row[6],
            anomaly_detection_enabled=bool(row[7]),
        )
        for row in result.all()
    ]


async def load_facts(
    session: AsyncSession,
    project_id: uuid.UUID,
    events: Sequence[EventHealthRow],
    now: datetime,
) -> dict[uuid.UUID, EventHealthFacts]:
    """Facts for every event in ``events``, in a fixed number of batched queries."""
    if not events:
        return {}
    now = to_utc(now)
    event_ids = [event.id for event in events]
    type_ids = sorted({event.event_type_id for event in events}, key=str)

    configs = await _load_configs(session, project_id)
    config_by_id = {config.id: config for config in configs}
    settling = await project_settling_delay(session, project_id)

    # Covering configs (a): the configs that wrote a metric for the event lately.
    metric_cover: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    coverage_from = now - timedelta(days=hw.COVERAGE_LOOKBACK_DAYS)
    for chunk in chunked(event_ids):
        result = await session.execute(
            select(EventMetric.event_id, EventMetric.scan_config_id)
            .where(
                EventMetric.event_id.in_(list(chunk)),
                EventMetric.bucket >= coverage_from,
            )
            .group_by(EventMetric.event_id, EventMetric.scan_config_id)
        )
        for event_id, scan_config_id in result.all():
            if event_id is not None and scan_config_id in config_by_id:
                metric_cover[event_id].add(scan_config_id)
    # Covering configs (b): the configs bound to the event's type.
    type_cover: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for config in configs:
        if config.event_type_id is not None:
            type_cover[config.event_type_id].add(config.id)

    lifecycle: dict[uuid.UUID, set[str]] = defaultdict(set)
    for chunk in chunked(event_ids):
        result = await session.execute(
            select(LifecycleFinding.event_id, LifecycleFinding.kind).where(
                LifecycleFinding.event_id.in_(list(chunk)),
                LifecycleFinding.resolved_at.is_(None),
            )
        )
        for event_id, kind in result.all():
            lifecycle[event_id].add(_enum_value(kind))

    expectations = _contract_expectations(
        (
            await session.execute(
                select(
                    FieldDefinition.event_type_id,
                    FieldDefinition.name,
                    FieldDefinition.field_type,
                    FieldDefinition.is_required,
                    FieldDefinition.enum_options,
                    FieldDefinition.contract_regex,
                    FieldDefinition.contract_min_value,
                    FieldDefinition.contract_max_value,
                ).where(FieldDefinition.event_type_id.in_(type_ids))
            )
        ).all()
    )

    active_schema: dict[uuid.UUID, set[tuple[str, str]]] = defaultdict(set)
    schema_rows = await session.execute(
        select(SchemaDrift.event_type_id, SchemaDrift.field_name, SchemaDrift.drift_type)
        .where(
            SchemaDrift.event_type_id.in_(type_ids),
            SchemaDrift.detected_at >= schema_drift_service.retention_cutoff(now),
            *schema_drift_service.active_drift_predicates(now),
        )
        .group_by(SchemaDrift.event_type_id, SchemaDrift.field_name, SchemaDrift.drift_type)
    )
    for event_type_id, field_name, drift_type in schema_rows.all():
        active_schema[event_type_id].add((field_name, _enum_value(drift_type)))

    value_drifts: dict[uuid.UUID, int] = {}
    for chunk in chunked(event_ids):
        result = await session.execute(
            select(VariableValueDrift.event_id, func.count(VariableValueDrift.id))
            .where(
                VariableValueDrift.project_id == project_id,
                VariableValueDrift.event_id.in_(list(chunk)),
                VariableValueDrift.detected_at
                >= variable_value_drift_service.retention_cutoff(now),
                *variable_value_drift_service.active_drift_predicates(now),
            )
            .group_by(VariableValueDrift.event_id)
        )
        value_drifts.update({event_id: int(count) for event_id, count in result.all()})

    distribution_rows = await session.execute(
        select(
            DistributionDrift.event_type_id, func.count(func.distinct(DistributionDrift.field_name))
        )
        .where(
            DistributionDrift.event_type_id.in_(type_ids),
            DistributionDrift.band == DistributionDriftBand.significant.value,
            DistributionDrift.bucket >= now - timedelta(days=hw.DISTRIBUTION_WINDOW_DAYS),
        )
        .group_by(DistributionDrift.event_type_id)
    )
    distribution: dict[uuid.UUID, int] = {
        event_type_id: int(count)
        for event_type_id, count in distribution_rows.all()
        if event_type_id is not None
    }

    owned_types = set(
        (
            await session.execute(
                select(EventTypeOwner.event_type_id)
                .where(EventTypeOwner.event_type_id.in_(type_ids))
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    freshness_by_config = {
        config.id: compute_freshness(config, now, settling=settling) for config in configs
    }

    covering: dict[uuid.UUID, list[_Config]] = {}
    for event in events:
        ids = metric_cover.get(event.id, set()) | type_cover.get(event.event_type_id, set())
        covering[event.id] = sorted(
            (config_by_id[config_id] for config_id in ids), key=lambda c: (c.name, str(c.id))
        )

    signal_candidates = [
        event.id
        for event in events
        if event.status in hw.SHIPPED_STATUSES
        and any(config.anomaly_detection_enabled for config in covering[event.id])
    ]
    open_signals = await open_unverdicted_event_signals(
        session, project_id, signal_candidates, now=now
    )

    facts: dict[uuid.UUID, EventHealthFacts] = {}
    for event in events:
        type_expectations = expectations.get(event.event_type_id, [])
        drifts_on_type = active_schema.get(event.event_type_id, set())
        violations = tuple(sorted(exp for exp in type_expectations if exp in drifts_on_type))
        event_configs = covering[event.id]
        facts[event.id] = EventHealthFacts(
            event_id=event.id,
            event_type_id=event.event_type_id,
            name=event.name,
            status=event.status,
            last_seen_at=event.last_seen_at,
            lifecycle_kinds=frozenset(lifecycle.get(event.id, set())),
            has_description=bool((event.description or "").strip()),
            has_owner=event.owner_id is not None or event.event_type_id in owned_types,
            contract_total=len(type_expectations),
            contract_violated=len(violations),
            contract_violations=violations,
            schema_drifts=sum(
                1 for _field, kind in drifts_on_type if kind in STRUCTURAL_DRIFT_TYPES
            ),
            value_drifts=value_drifts.get(event.id, 0),
            distribution_drifts=distribution.get(event.event_type_id, 0),
            covered=bool(event_configs),
            detection_enabled=any(config.anomaly_detection_enabled for config in event_configs),
            open_signals=open_signals.get(event.id, 0),
            freshness=tuple(
                (
                    config.name,
                    str(freshness_by_config[config.id].status),
                    freshness_by_config[config.id].lag_seconds,
                )
                for config in event_configs
            ),
        )
    return facts


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _scored_population(project_id: uuid.UUID, main_branch_id: uuid.UUID) -> Select[Any]:
    return select(*_ROW_COLUMNS).where(
        Event.project_id == project_id,
        Event.branch_id == main_branch_id,
        Event.status != EventStatus.archived,
    )


async def _load_rows(
    session: AsyncSession, project_id: uuid.UUID, event_ids: Sequence[uuid.UUID] | None
) -> list[EventHealthRow]:
    main_branch_id = await ensure_main_branch_id(session, project_id)
    base = _scored_population(project_id, main_branch_id)
    if event_ids is None:
        return [_row(values) for values in (await session.execute(base)).all()]
    rows: list[EventHealthRow] = []
    unique_ids = list(dict.fromkeys(event_ids))
    for chunk in chunked(unique_ids):
        result = await session.execute(base.where(Event.id.in_(list(chunk))))
        rows.extend(_row(values) for values in result.all())
    return rows


async def score_events(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_ids: Sequence[uuid.UUID] | None,
    now: datetime | None = None,
) -> dict[uuid.UUID, EventHealth]:
    """Health of the given events (``None``: every scored event of the project).

    Ids outside the scored population (another project, a branch, archived,
    missing) are absent from the result.
    """
    now = to_utc(now) if now is not None else datetime.now(UTC)
    rows = await _load_rows(session, project_id, event_ids)
    facts = await load_facts(session, project_id, rows, now)
    return {event_id: score_event(event_facts, now) for event_id, event_facts in facts.items()}


async def get_event_health(session: AsyncSession, slug: str, event_id: uuid.UUID) -> EventHealth:
    project_id = await resolve_project_id(session, slug)
    health = (await score_events(session, project_id, [event_id])).get(event_id)
    if health is None:
        raise HTTPException(status_code=404, detail=EVENT_NOT_FOUND)
    return health


async def list_events_health(
    session: AsyncSession, slug: str, ids: Sequence[uuid.UUID]
) -> EventHealthListResponse:
    project_id = await resolve_project_id(session, slug)
    now = datetime.now(UTC)
    scores = await score_events(session, project_id, ids, now)
    ordered = [scores[event_id] for event_id in dict.fromkeys(ids) if event_id in scores]
    return EventHealthListResponse(items=ordered, computed_at=now)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HealthAggregate:
    score: int | None
    scored_events: int
    healthy_count: int
    warning_count: int
    unhealthy_count: int
    component_averages: list[ComponentAverage]
    worst: list[EventHealthBrief]

    @property
    def grade(self) -> hw.HealthGrade | None:
        return None if self.score is None else grade_for(self.score)


def _worst_key(health: EventHealth) -> tuple[int, str, str]:
    return (health.score, health.name, str(health.event_id))


def _brief(health: EventHealth) -> EventHealthBrief:
    return EventHealthBrief(
        event_id=health.event_id,
        name=health.name,
        score=health.score,
        grade=health.grade,
        top_issue=health.top_issue,
    )


def aggregate(healths: Sequence[EventHealth]) -> HealthAggregate:
    """Mean score, grade counts, component averages and the worst events."""
    values: dict[HealthComponentKey, list[float]] = {key: [] for key in hw.COMPONENT_ORDER}
    for health in healths:
        for component in health.components:
            if component.applies and component.value is not None:
                values[component.key].append(component.value)
    averages = [
        ComponentAverage(
            key=key,
            value=round(sum(values[key]) / len(values[key]), 4) if values[key] else None,
            applies_count=len(values[key]),
        )
        for key in hw.COMPONENT_ORDER
    ]
    score = int(sum(health.score for health in healths) / len(healths) + 0.5) if healths else None
    return HealthAggregate(
        score=score,
        scored_events=len(healths),
        healthy_count=sum(1 for health in healths if health.grade == "healthy"),
        warning_count=sum(1 for health in healths if health.grade == "warning"),
        unhealthy_count=sum(1 for health in healths if health.grade == "unhealthy"),
        component_averages=averages,
        worst=[_brief(health) for health in sorted(healths, key=_worst_key)][
            : hw.WORST_EVENTS_LIMIT
        ],
    )


async def event_type_health(session: AsyncSession, slug: str) -> list[EventTypeHealth]:
    """One entry per main-branch event type of the project, scored or not."""
    project_id = await resolve_project_id(session, slug)
    main_branch_id = await ensure_main_branch_id(session, project_id)
    type_ids = (
        (
            await session.execute(
                select(EventType.id)
                .where(EventType.project_id == project_id, EventType.branch_id == main_branch_id)
                .order_by(EventType.order.asc(), EventType.name.asc())
            )
        )
        .scalars()
        .all()
    )
    scores = await score_events(session, project_id, None)
    by_type: dict[uuid.UUID, list[EventHealth]] = defaultdict(list)
    for health in scores.values():
        by_type[health.event_type_id].append(health)
    items: list[EventTypeHealth] = []
    for type_id in type_ids:
        agg = aggregate(by_type.get(type_id, []))
        items.append(
            EventTypeHealth(
                event_type_id=type_id,
                score=agg.score,
                grade=agg.grade,
                scored_events=agg.scored_events,
                healthy_count=agg.healthy_count,
                warning_count=agg.warning_count,
                unhealthy_count=agg.unhealthy_count,
                component_averages=agg.component_averages,
                worst=agg.worst,
            )
        )
    return items


async def project_summary_for_snapshot(
    session: AsyncSession, project_id: uuid.UUID, now: datetime | None = None
) -> HealthAggregate:
    """The project-wide aggregate the daily snapshot stores."""
    return aggregate(list((await score_events(session, project_id, None, now)).values()))


def snapshot_payload(summary: HealthAggregate) -> dict[str, Any]:
    """Column values of a ``ProjectHealthSnapshot`` row for ``summary``."""
    return {
        "score": summary.score,
        "scored_events": summary.scored_events,
        "healthy_count": summary.healthy_count,
        "warning_count": summary.warning_count,
        "unhealthy_count": summary.unhealthy_count,
        "component_averages": {
            average.key: {"value": average.value, "applies_count": average.applies_count}
            for average in summary.component_averages
        },
        "worst_events": [
            {
                "event_id": str(brief.event_id),
                "name": brief.name,
                "score": brief.score,
                "top_issue": brief.top_issue,
            }
            for brief in summary.worst
        ],
    }


async def _trend(
    session: AsyncSession, project_id: uuid.UUID, today: date, trend_days: int
) -> tuple[list[ProjectHealthTrendPoint], int | None]:
    since = today - timedelta(days=max(trend_days, hw.TREND_DELTA_DAYS))
    rows = (
        await session.execute(
            select(
                ProjectHealthSnapshot.day,
                ProjectHealthSnapshot.score,
                ProjectHealthSnapshot.scored_events,
            )
            .where(
                ProjectHealthSnapshot.project_id == project_id,
                ProjectHealthSnapshot.day >= since,
            )
            .order_by(ProjectHealthSnapshot.day.asc())
        )
    ).all()
    trend_from = today - timedelta(days=trend_days - 1)
    trend = [
        ProjectHealthTrendPoint(day=day, score=score, scored_events=scored_events)
        for day, score, scored_events in rows
        if day >= trend_from
    ]
    previous_day = today - timedelta(days=hw.TREND_DELTA_DAYS)
    previous = next((score for day, score, _n in rows if day == previous_day), None)
    return trend, previous


async def project_health(
    session: AsyncSession, slug: str, trend_days: int = 30
) -> ProjectHealthResponse:
    project_id = await resolve_project_id(session, slug)
    key = cache.key_project_health(project_id, trend_days)
    cached = await cache.get_json(key)
    if cached is not None:
        try:
            return ProjectHealthResponse.model_validate(cached)
        except ValueError:
            await cache.delete(key)
    now = datetime.now(UTC)
    summary = await project_summary_for_snapshot(session, project_id, now)
    trend, previous = await _trend(session, project_id, now.date(), trend_days)
    response = ProjectHealthResponse(
        score=summary.score,
        grade=summary.grade,
        scored_events=summary.scored_events,
        healthy_count=summary.healthy_count,
        warning_count=summary.warning_count,
        unhealthy_count=summary.unhealthy_count,
        component_averages=summary.component_averages,
        worst=summary.worst,
        trend=trend,
        previous_score=previous,
        computed_at=now,
    )
    await cache.set_json(key, response.model_dump(mode="json"), PROJECT_HEALTH_CACHE_TTL_SECONDS)
    return response


async def health_sorted_ids(
    session: AsyncSession, project_id: uuid.UUID, id_query: Select[Any]
) -> list[uuid.UUID]:
    """Ids of ``id_query`` (selecting ``Event.id, Event.name``) least healthy first.

    Order: score ascending, then name, then id. Rows outside the scored
    population (an explicitly requested archived event) have no score and sort
    after every scored one, by name then id.
    """
    rows = [(event_id, name) for event_id, name in (await session.execute(id_query)).all()]
    scores = await score_events(session, project_id, [event_id for event_id, _name in rows])
    unscored = 101  # past any real score (0..100)

    def _key(row: tuple[uuid.UUID, str]) -> tuple[int, str, str]:
        event_id, name = row
        health = scores.get(event_id)
        return (health.score if health is not None else unscored, name, str(event_id))

    return [event_id for event_id, _name in sorted(rows, key=_key)]
