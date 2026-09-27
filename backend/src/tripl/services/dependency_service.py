"""What depends on a plan entity, and what a planned change would touch (GH #257).

tripl answers "what breaks if I change this?" here, for events, event types,
fields, variables, metrics, fact tables, alert rules and relations. The graph is
computed on demand from the stored references — nothing is persisted, so there
is no migration and nothing to keep in sync.

**Warn, never block.** The owner's decision on #257: breaking changes are
reported, and the one refusal stays exactly where it was —
``fact_table_dependents`` answering 409 to a fact-table edit that would strand
a metric. Delete, deprecate and rename keep their server-side behaviour; the UI
shows these answers in its confirm dialogs. A ``possible`` edge (a name found in
free SQL, see ``_dependency_sql``) is labelled as such and is only a hint.

Three entry points:

* :func:`resolve` — one entity's upstream and downstream edges, one hop by
  default and two when asked (``depth=2``, at most ``_MAX_SECOND_HOP``
  neighbours expanded per request);
* :func:`impact_response` — a caller's planned change set, each change with
  the objects it affects (always one hop) and a "2 metrics and 1 alert rule"
  summary;
* :func:`branch_impact_response` — the same shape, computed from a working
  branch's diff (``plan_branch_service.diff_branch``): every event, event
  type, field and variable the branch deletes, renames, deprecates or edits.

Branch-aware throughout: branch rows are read on the branch, and the
project-wide rows that store MAIN ids (metrics, alert rules, scans) are matched
against the branch row's main twin too (``_dependency_locate``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.models.field_definition import FieldDefinition
from tripl.models.variable import Variable
from tripl.schemas.dependency import (
    DependenciesResponse,
    DependencyEdge,
    DependencyEntity,
    DependencyEntityKind,
    ImpactChange,
    ImpactChangeKind,
    ImpactItem,
    ImpactResponse,
)
from tripl.schemas.plan_revision import PlanDiffEntry
from tripl.services._dependency_columns import field_deps, variable_deps
from tripl.services._dependency_edges import (
    alert_rule_deps,
    event_deps,
    event_type_deps,
    fact_table_deps,
    metric_deps,
    orphan_deps,
    relation_deps,
)
from tripl.services._dependency_locate import (
    Scope,
    locate_alert_rule,
    locate_event,
    locate_event_type,
    locate_fact_table,
    locate_field,
    locate_metric,
    locate_relation,
    locate_variable,
)
from tripl.services._dependency_model import (
    ENTITY_KINDS,
    Dependencies,
    Edge,
    EntityRef,
    counts_by_kind,
    dedupe_edges,
    impact_summary,
    parse_entity_ref,
    possible_counts_by_kind,
)
from tripl.services.plan_branch_service import (
    diff_branch,
    ensure_main_branch_id,
    resolve_branch_id,
)
from tripl.services.project_lookup import resolve_project_id

__all__ = [
    "Dependencies",
    "Edge",
    "EntityRef",
    "branch_impact_response",
    "dependencies_response",
    "impact_response",
    "parse_entity_ref",
    "resolve",
]

# How many first-hop neighbours one request's two-hop walk expands, upstream
# and downstream together. An event type can hold hundreds of events; the
# second hop is for "what does this reach", not an inventory, and each
# expansion costs a handful of queries.
_MAX_SECOND_HOP = 50

# Diff entity types the branch impact reads, and the kind each becomes.
_DIFF_KINDS: dict[str, DependencyEntityKind] = {
    "event": "event",
    "event_type": "event_type",
    "field_definition": "field",
    "variable": "variable",
}

# An event status change that retires it.
_RETIRING_STATUSES = frozenset({"deprecated", "archived"})


async def _scope(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    slug: str | None,
) -> Scope:
    main_branch_id = await ensure_main_branch_id(session, project_id)
    resolved = await resolve_branch_id(session, project_id, branch_id)
    return Scope(
        session=session,
        project_id=project_id,
        branch_id=resolved,
        main_branch_id=main_branch_id,
        slug=slug,
    )


async def _resolve_in(scope: Scope, entity: EntityRef) -> Dependencies:
    """One hop around ``entity``, located on ``scope``'s branch."""
    upstream: list[Edge]
    downstream: list[Edge]
    name: str | None
    match entity.kind:
        case "event":
            event = await locate_event(scope, entity.id)
            if event is None:
                return await _orphan(scope, entity)
            name = event.name
            upstream, downstream = await event_deps(scope, event)
            located = EntityRef("event", event.id)
        case "event_type":
            event_type = await locate_event_type(scope, entity.id)
            if event_type is None:
                return await _orphan(scope, entity)
            name = event_type.display_name or event_type.name
            upstream, downstream = await event_type_deps(scope, event_type)
            located = EntityRef("event_type", event_type.id)
        case "field":
            found = await locate_field(scope, entity.id)
            if found is None:
                return await _orphan(scope, entity)
            field_def, owner = found
            name = f"{owner.name}.{field_def.name}"
            upstream, downstream = await field_deps(scope, field_def, owner)
            located = EntityRef("field", field_def.id)
        case "variable":
            variable = await locate_variable(scope, entity.id)
            if variable is None:
                return await _orphan(scope, entity)
            name = variable.name
            upstream, downstream = await variable_deps(scope, variable)
            located = EntityRef("variable", variable.id)
        case "metric":
            metric = await locate_metric(scope, entity.id)
            if metric is None:
                return await _orphan(scope, entity)
            name = metric.display_name or metric.name
            upstream, downstream = await metric_deps(scope, metric)
            located = entity
        case "fact_table":
            table = await locate_fact_table(scope, entity.id)
            if table is None:
                return await _orphan(scope, entity)
            name = table.display_name or table.name
            upstream, downstream = await fact_table_deps(scope, table)
            located = entity
        case "alert_rule":
            rule = await locate_alert_rule(scope, entity.id)
            if rule is None:
                return await _orphan(scope, entity)
            name = rule.name
            upstream, downstream = await alert_rule_deps(scope, rule)
            located = entity
        case "relation":
            relation = await locate_relation(scope, entity.id)
            if relation is None:
                return await _orphan(scope, entity)
            name = relation.relation_type
            upstream, downstream = await relation_deps(scope, relation)
            located = EntityRef("relation", relation.id)
        case _:
            return Dependencies(entity=entity, exists=False)
    return Dependencies(
        entity=located,
        name=name,
        exists=True,
        upstream=dedupe_edges(upstream, exclude=located),
        downstream=dedupe_edges(downstream, exclude=located),
    )


async def _orphan(scope: Scope, entity: EntityRef) -> Dependencies:
    upstream, downstream = await orphan_deps(scope, entity.kind, entity.id)
    return Dependencies(
        entity=entity,
        name=None,
        exists=False,
        upstream=dedupe_edges(upstream),
        downstream=dedupe_edges(downstream),
    )


async def _second_hop(scope: Scope, root: Dependencies, *, downstream: bool) -> list[Edge]:
    first = root.downstream if downstream else root.upstream
    expandable = [e for e in first if e.kind in ENTITY_KINDS]
    # The request's budget, shared by both directions (``scope.expansions_left``).
    take = expandable[: max(scope.expansions_left, 0)]
    scope.expansions_left -= len(take)
    # One bulk read of the event neighbours and their main twins, instead of
    # a locate + a twin lookup per neighbour.
    await scope.prime_events(edge.id for edge in take if edge.kind == "event")
    reached: list[Edge] = []
    for edge in take:
        neighbour = await _resolve_in(
            scope, EntityRef(cast(DependencyEntityKind, edge.kind), edge.id)
        )
        hop = neighbour.downstream if downstream else neighbour.upstream
        reached.extend(replace(item, depth=2) for item in hop)
    return reached


async def _resolve_scoped(scope: Scope, entity: EntityRef, depth: int) -> Dependencies:
    deps = await _resolve_in(scope, entity)
    if depth >= 2 and deps.exists:
        deps.upstream = dedupe_edges(
            [*deps.upstream, *await _second_hop(scope, deps, downstream=False)],
            exclude=deps.entity,
        )
        deps.downstream = dedupe_edges(
            [*deps.downstream, *await _second_hop(scope, deps, downstream=True)],
            exclude=deps.entity,
        )
    return deps


async def resolve(
    session: AsyncSession,
    project_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    entity: EntityRef,
    *,
    depth: int = 1,
    slug: str | None = None,
) -> Dependencies:
    """Upstream and downstream edges of ``entity`` on ``branch_id`` (``None`` = main).

    ``depth`` is 1 or 2; anything above 2 is read as 2. ``slug`` only feeds the
    ``url_hint`` of each edge. An id that resolves to nothing answers
    ``exists=False`` with whatever project-wide rows still name that id.
    """
    scope = await _scope(session, project_id, branch_id, slug)
    scope.expansions_left = _MAX_SECOND_HOP
    return await _resolve_scoped(scope, entity, min(max(depth, 1), 2))


# ── response builders ───────────────────────────────────────────────────────


def _edge_out(edge: Edge) -> DependencyEdge:
    return DependencyEdge(
        kind=edge.kind,
        id=edge.id,
        name=edge.name,
        relation=edge.relation,
        certainty=edge.certainty,
        url_hint=edge.url_hint,
        depth=edge.depth,
    )


def _entity_out(deps: Dependencies) -> DependencyEntity:
    return DependencyEntity(
        kind=deps.entity.kind, id=deps.entity.id, name=deps.name, exists=deps.exists
    )


async def dependencies_response(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    slug: str,
    branch_id: uuid.UUID | None,
    entity: EntityRef,
    depth: int = 1,
) -> DependenciesResponse:
    deps = await resolve(session, project_id, branch_id, entity, depth=depth, slug=slug)
    return DependenciesResponse(
        entity=_entity_out(deps),
        upstream=[_edge_out(e) for e in deps.upstream],
        downstream=[_edge_out(e) for e in deps.downstream],
        counts_by_kind=counts_by_kind(deps.downstream),
        possible_counts_by_kind=possible_counts_by_kind(deps.downstream),
    )


def _impact_item(change: ImpactChange, deps: Dependencies) -> ImpactItem:
    return ImpactItem(
        change=change,
        entity=_entity_out(deps),
        name=deps.name,
        affected=[_edge_out(e) for e in deps.downstream],
        summary=impact_summary(deps.downstream),
    )


async def impact_response(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    slug: str,
    branch_id: uuid.UUID | None,
    changes: Sequence[ImpactChange],
) -> ImpactResponse:
    """What each planned change would affect, one hop. Reads only; refuses nothing.

    Always depth 1: a confirm dialog lists what the change touches directly,
    and two hops over a 200-change batch was the expensive path. The
    request's ``depth`` is accepted and ignored (``ImpactRequest``). One
    ``Scope`` serves the whole batch, so the project-wide tables, the branch
    variables and the event rows (primed up front) are each read once.
    """
    scope = await _scope(session, project_id, branch_id, slug)
    await scope.prime_events(change.id for change in changes if change.kind == "event")
    items = []
    for change in changes:
        deps = await _resolve_scoped(scope, EntityRef(change.kind, change.id), 1)
        items.append(_impact_item(change, deps))
    return ImpactResponse(items=items)


# ── branch impact ───────────────────────────────────────────────────────────


def _as_uuid(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def _scalar_id(
    session: AsyncSession, statement: Select[tuple[uuid.UUID]]
) -> uuid.UUID | None:
    found: uuid.UUID | None = await session.scalar(statement)
    return found


async def _by_name(
    scope: Scope, kind: DependencyEntityKind, name: str, parent: str | None
) -> uuid.UUID | None:
    """A diff row's entity on ``scope``'s branch by natural key, when its id is gone."""
    session = scope.session
    branch_type = select(EventType.id).where(
        EventType.project_id == scope.project_id,
        EventType.branch_id == scope.branch_id,
        EventType.name == (parent or ""),
    )
    match kind:
        case "event_type":
            return await _scalar_id(
                session,
                select(EventType.id).where(
                    EventType.project_id == scope.project_id,
                    EventType.branch_id == scope.branch_id,
                    EventType.name == name,
                ),
            )
        case "field":
            return await _scalar_id(
                session,
                select(FieldDefinition.id).where(
                    FieldDefinition.event_type_id.in_(branch_type.scalar_subquery()),
                    FieldDefinition.name == name,
                ),
            )
        case "event":
            return await _scalar_id(
                session,
                select(Event.id)
                .where(
                    Event.project_id == scope.project_id,
                    Event.branch_id == scope.branch_id,
                    Event.event_type_id.in_(branch_type.scalar_subquery()),
                    Event.name == name,
                )
                .order_by(Event.id)
                .limit(1),
            )
        case "variable":
            return await _scalar_id(
                session,
                select(Variable.id).where(
                    Variable.project_id == scope.project_id,
                    Variable.branch_id == scope.branch_id,
                    Variable.name == name,
                ),
            )
    return None


def _change_of(entry: PlanDiffEntry) -> ImpactChangeKind:
    for field_change in entry.field_changes:
        if field_change.field == "status" and field_change.after in _RETIRING_STATUSES:
            return "deprecate"
    return "change"


async def _entry_entity(
    scope: Scope, kind: DependencyEntityKind, entry: PlanDiffEntry
) -> uuid.UUID | None:
    entity_id = _as_uuid(entry.entity_id)
    if entity_id is not None and await _exists(scope, EntityRef(kind, entity_id)):
        return entity_id
    return await _by_name(scope, kind, entry.name, entry.parent)


async def _exists(scope: Scope, entity: EntityRef) -> bool:
    match entity.kind:
        case "event":
            return await locate_event(scope, entity.id) is not None
        case "event_type":
            return await locate_event_type(scope, entity.id) is not None
        case "field":
            return await locate_field(scope, entity.id) is not None
        case "variable":
            return await locate_variable(scope, entity.id) is not None
    return False


async def branch_impact_response(
    session: AsyncSession, *, slug: str, branch_id: uuid.UUID
) -> ImpactResponse:
    """The branch diff's deletions, renames, deprecations and edits, with what each touches.

    Built on ``plan_branch_service.diff_branch`` — the diff the branch page
    already shows, including its rename pairing — so the panel and the diff can
    never disagree about what changed. Per entry:

    * a REMOVED row (not half of a rename) is a ``delete``; it no longer exists
      on the branch, so it is read on main, where its dependents still point;
    * a rename pair is one ``rename``, read on main through the removed half —
      the id everything project-wide stores. Besides the diff's own pairing, a
      removed event whose main id is the ``origin_id`` of an ADDED branch event
      is that event under a new name, so it is a ``rename`` too;
    * a removed event type does not list, among what it affects, the events
      and fields the same diff removes: those are items of their own, and
      counting them twice would inflate the type's summary;
    * a CHANGED row is a ``deprecate`` when its status moved to deprecated or
      archived, otherwise a ``change``; it is read on the branch;
    * additions are skipped (nothing can depend on a row that does not exist
      yet), and so are housekeeping rows, which are the machine's doing.
    """
    project_id = await resolve_project_id(session, slug)
    diff = await diff_branch(session, slug, branch_id)
    main_scope = await _scope(session, project_id, None, slug)
    branch_scope = Scope(
        session=session,
        project_id=project_id,
        branch_id=branch_id,
        main_branch_id=main_scope.main_branch_id,
        slug=slug,
    )
    renamed_from = {
        (rename.entity_type, rename.parent, rename.removed_name) for rename in diff.renames
    }
    renamed_origins = await _added_event_origins(branch_scope, diff.entries)

    # First pass: which entity each entry is, and how it changed.
    planned: list[tuple[DependencyEntityKind, uuid.UUID, ImpactChangeKind, Scope]] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    removed: set[tuple[str, uuid.UUID]] = set()
    for entry in diff.entries:
        kind = _DIFF_KINDS.get(entry.entity_type)
        if kind is None or entry.housekeeping is not None or entry.kind == "added":
            continue
        key = (entry.entity_type, entry.parent, entry.name)
        change: ImpactChangeKind
        if entry.kind == "removed":
            change = "delete"
            scope = main_scope
        else:
            change = _change_of(entry)
            scope = branch_scope
        entity_id = await _entry_entity(scope, kind, entry)
        if entity_id is None or (kind, entity_id) in seen:
            continue
        seen.add((kind, entity_id))
        if entry.kind == "removed":
            removed.add((kind, entity_id))
            if key in renamed_from or (kind == "event" and entity_id in renamed_origins):
                change = "rename"
        planned.append((kind, entity_id, change, scope))

    items: list[ImpactItem] = []
    for kind, entity_id, change, scope in planned:
        deps = await _resolve_scoped(scope, EntityRef(kind, entity_id), 1)
        if kind == "event_type" and (kind, entity_id) in removed:
            deps.downstream = [
                edge
                for edge in deps.downstream
                if not (edge.kind in {"event", "field"} and (edge.kind, edge.id) in removed)
            ]
        items.append(_impact_item(ImpactChange(kind=kind, id=deps.entity.id, change=change), deps))
    return ImpactResponse(items=items)


async def _added_event_origins(scope: Scope, entries: Sequence[PlanDiffEntry]) -> set[uuid.UUID]:
    """The main ids that an ADDED branch event was copied from (``origin_id``).

    The diff keys events by name, so renaming a branch copy reads as its old
    name removed and a new name added; the added row still carries the main
    row it came from, which pairs the two even where ``diff.renames`` does not.
    """
    added_ids = [
        entity_id
        for entry in entries
        if entry.entity_type == "event" and entry.kind == "added"
        if (entity_id := _as_uuid(entry.entity_id)) is not None
    ]
    if not added_ids:
        return set()
    await scope.prime_events(added_ids)
    origins: set[uuid.UUID] = set()
    for event_id in added_ids:
        event = await scope.event_by_id(event_id)
        if event is not None and event.branch_id == scope.branch_id and event.origin_id is not None:
            origins.add(event.origin_id)
    return origins
