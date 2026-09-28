"""Audit builder: the change trail behind everything the recipe just authored.

The demo seeder writes plan objects directly rather than through the audited
service paths, so a fresh demo landed on "No audit entries yet" — a dead end on
a surface the product calls a headline governance record and the Concepts
glossary describes as "a chronological record of who changed what in the plan"
(tripl-jfm3.60).

This builder backfills that record from what the recipe REALLY created: it reads
the ids the earlier builders published on the context (and the rows they
inserted) and emits one entry per authored object, attributed to the demo's
creator and back-dated so the log reads as a plausible build-up rather than a
single timestamp. "Per authored object" was aspirational until tripl-0zpq.246 —
the fact table, the four catalog metrics, the relation, the event-type owner
grant, the variable override, the feature branch and its one branch-side edit
all existed with nothing in the trail saying who made them. They are covered
now, and anything a future builder authors belongs here too. Every action string
a PROJECT-scoped row uses is one the Audit
tab's filter offers, so the seeded trail is filterable out of the box; the one
instance-scoped row (``data_source.create``) carries no project, which is how its
real route records it, and is therefore no more and no less visible than a real
one (tripl-wkwv.15).

EVENTS were missing from that list until tripl-wkwv.14, and not by choice: when
this builder was written the events router recorded nothing at all, so there was
no vocabulary to seed. tripl-wkwv.10 gave events one and made the Events group
the FIRST thing the filter offers — and on a demo holding eighteen events it
matched nothing, implying nobody had ever created an event on a project whose
whole point is to look lived-in.

THREE RULES THE EVENT ROWS FOLLOW, each of them a way of not lying:

1. Every row is DERIVED from a row some earlier builder really wrote. Creations
   are dated from each event's own ``created_at`` — the same column the detail
   page reads "first seen" from — and edits are read back from the ``EventChange``
   history the activity builder seeded. Nothing here invents a subject.
2. One audit row per REQUEST, not per changed field. A PATCH writes one audit row
   and one ``EventChange`` per TRACKED field it changed (six of them since
   9fbc5811 — ``event_service._TRACKED_FIELDS``; this line said four until
   tripl-0zpq.244), so the edits are grouped by (event, instant) before becoming
   rows; two fields changed together stay one row, as they would in production.
   The ``created`` row every event now carries is skipped here: it is the same
   act ``event.create`` already records, not an edit.
3. No bulk and no delete rows. The recipe never bulk-edited or deleted anything,
   so ``event.bulk_*`` and ``event.delete`` match nothing — truthfully. A reader
   who wants to see one can perform the action in the demo and watch its row
   appear, which is exactly what the demo docs promise about their own actions.

Runs after every builder that creates an object it records, which is tenth of
eleven — the search reindex follows it. It only records what already exists, and
never invents an object.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.bucketing import to_utc
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.audit_log import AuditLog
from tripl.models.data_source import DataSource
from tripl.models.event import Event
from tripl.models.event_change import EventChange
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.fact_table import FactTable
from tripl.models.metric_definition import MetricDefinition
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.shadow_event_candidate import SHADOW_STATUS_DISMISSED, ShadowEventCandidate
from tripl.models.user import User
from tripl.models.variable_event_value_override import VariableEventValueOverride
from tripl.services.demo.builders import branches
from tripl.services.demo.scenario import DemoContext

# Entries with no instant of their own are spread backwards from an anchor so the
# log has a real chronology (schema first, then sources, then alerting) instead of
# N rows sharing one timestamp.
_ENTRY_SPACING = timedelta(minutes=7)
_SOURCE_NOTE = "Seeded by the demo recipe — no real change was made to a warehouse."


@dataclass(frozen=True)
class _Entry:
    action: str
    target_type: str
    target_id: uuid.UUID | None
    target_name: str
    # Merged UNDER the demo marker, so a payload key can never displace the
    # ``demo_seed`` flag the demo docs promise every seeded row carries. Values
    # must already be JSON primitives: writing ``AuditLog`` directly bypasses
    # ``audit_service._jsonable``, so a stray UUID or datetime raises at flush.
    payload: dict[str, object] | None = None
    # Stamped only on the rows the recipe really authored ON the feature branch.
    # ``audit_service.record`` reads the branch off a request-scoped contextvar,
    # so a branch row carries both halves or neither; main is spelled as their
    # ABSENCE, which is why every other entry leaves these unset.
    branch_id: uuid.UUID | None = None
    branch_name: str = ""
    # False for the one object the recipe authors that is NOT a project resource.
    # A data source is workspace-level — ``DataSource.project_id`` is non-NULL
    # only for a demo's synthetic warehouse, so it can be cleaned up with the
    # project — and ``api/v1/data_sources.py`` records the action with no project
    # at all. Seeding it WITH one made the demo the only place that shape exists,
    # and put a row in the Audit tab that its filter deliberately cannot offer
    # (tripl-wkwv.15).
    project_scoped: bool = True


def _plan_entries(ctx: DemoContext) -> list[_Entry]:
    """One creation entry per authored schema object, in authoring order."""
    entries = [
        _Entry("event_type.create", "event_type", type_id, name)
        for name, type_id in ctx.event_type_ids.items()
    ]
    entries += [
        # ``target_type="field_definition"`` and the BARE field name, which is
        # the shape ``api/v1/fields.py`` writes. ``ctx.field_ids`` is keyed
        # "<event type>.<field>" for the builders' own lookups, and seeding that
        # qualified key as the target name — under a ``target_type`` no route has
        # ever used — put a row in the Audit tab that the product cannot produce
        # (tripl-0zpq.246).
        _Entry("field.create", "field_definition", field_id, key.split(".", 1)[-1])
        for key, field_id in ctx.field_ids.items()
    ]
    entries += [
        _Entry("meta_field.create", "meta_field", meta_id, name)
        for name, meta_id in ctx.meta_field_ids.items()
    ]
    entries += [
        _Entry("variable.create", "variable", variable_id, name)
        for name, variable_id in ctx.variable_ids.items()
    ]
    return entries


async def _authored_plan_entries(session: AsyncSession, ctx: DemoContext) -> list[_Entry]:
    """The plan objects ``_plan_entries`` cannot name from the context alone.

    The relation, the event-type owner grant and the authored variable override
    are all things the recipe really writes, and all three were missing from the
    trail while the module docstring above promised "one entry per authored
    object" (tripl-0zpq.246). Each carries the ``target_type`` and the
    ``target_name`` its own route records — including the two routes that
    deliberately record an EMPTY name (a relation has none; an owner grant names
    the event type in the payload, not the title).
    """
    entries: list[_Entry] = []
    relation = (
        await session.execute(
            select(EventTypeRelation)
            .where(
                EventTypeRelation.project_id == ctx.project_id,
                EventTypeRelation.branch_id == ctx.branch_id,
            )
            .order_by(EventTypeRelation.id)
        )
    ).scalars()
    for row in relation:
        entries.append(
            _Entry(
                "relation.create",
                "relation",
                row.id,
                "",
                payload={
                    "source_event_type_id": str(row.source_event_type_id),
                    "target_event_type_id": str(row.target_event_type_id),
                    "source_field_id": str(row.source_field_id),
                    "target_field_id": str(row.target_field_id),
                },
            )
        )
    owners = (
        await session.execute(
            # Scoped to the ids the plan builder published, NOT to the project:
            # the branches builder deep-copies the plan, and a project-wide
            # select would file a second grant naming a branch copy nobody
            # granted anything on — the trap ``_authored_events`` documents.
            select(EventTypeOwner)
            .where(EventTypeOwner.event_type_id.in_(list(ctx.event_type_ids.values())))
            .order_by(EventTypeOwner.event_type_id, EventTypeOwner.user_id)
        )
    ).scalars()
    entries += [
        _Entry(
            "event_type.add_owner",
            "event_type",
            owner.event_type_id,
            "",
            payload={"user_id": str(owner.user_id)},
        )
        for owner in owners
    ]
    overrides = (
        await session.execute(
            select(VariableEventValueOverride)
            .where(
                VariableEventValueOverride.project_id == ctx.project_id,
                VariableEventValueOverride.branch_id == ctx.branch_id,
            )
            .order_by(VariableEventValueOverride.variable_id, VariableEventValueOverride.event_id)
        )
    ).scalars()
    variable_names = {value: key for key, value in ctx.variable_ids.items()}
    event_names = {value: key for key, value in ctx.event_ids.items()}
    entries += [
        _Entry(
            "variable.override_set",
            "variable",
            override.variable_id,
            # The VARIABLE's name, which is what the route files: the target IS
            # the variable, and the event belongs in the payload beside it.
            variable_names.get(override.variable_id, ""),
            payload={
                "event_id": str(override.event_id),
                "event_name": event_names.get(override.event_id, ""),
                "values": list(override.values),
            },
        )
        for override in overrides
    ]
    return entries


async def _catalog_entries(session: AsyncSession, ctx: DemoContext) -> list[_Entry]:
    """The fact table and the catalog metrics the recipe authors.

    Ordered by name so two seeds assign the same timestamps; the fact table
    first, because a metric that reads it could not have been defined before it
    existed.
    """
    entries: list[_Entry] = []
    if ctx.fact_table_id is not None:
        fact_table = await session.get(FactTable, ctx.fact_table_id)
        if fact_table is not None:
            entries.append(
                _Entry("fact_table.create", "fact_table", fact_table.id, fact_table.name)
            )
    metrics = (
        await session.execute(
            select(MetricDefinition)
            .where(MetricDefinition.project_id == ctx.project_id)
            .order_by(MetricDefinition.name, MetricDefinition.id)
        )
    ).scalars()
    entries += [
        _Entry("metric_definition.create", "metric_definition", metric.id, metric.name)
        for metric in metrics
    ]
    return entries


async def _branch_entries(session: AsyncSession, ctx: DemoContext) -> list[_Entry]:
    """The feature branch's own trail: its creation, and the one edit made on it.

    The EDIT is the only row in this builder that carries a branch, and the only
    reason ``_Entry`` grew branch fields. Creating the branch is an action ON
    MAIN: ``POST /projects/{slug}/branches`` takes no ``?branch=``
    (``api/v1/plan_branches.py``), so ``audit_service.record`` reads nothing off
    the contextvar and the real ``plan_branch.create`` row carries no branch
    either. Everything else here was authored on main too, which that same
    function spells as the ABSENCE of a branch.
    """
    branch = (
        (
            await session.execute(
                select(PlanBranch)
                .where(
                    PlanBranch.project_id == ctx.project_id,
                    PlanBranch.kind == BranchKind.working.value,
                )
                .order_by(PlanBranch.created_at, PlanBranch.id)
            )
        )
        .scalars()
        .first()
    )
    if branch is None:
        return []
    entries = [_Entry("plan_branch.create", "plan_branch", branch.id, branch.name)]
    edited = (
        (
            await session.execute(
                select(Event).where(
                    Event.project_id == ctx.project_id,
                    Event.branch_id == branch.id,
                    Event.name == branches.CHANGED_EVENT_NAME,
                )
            )
        )
        .scalars()
        .first()
    )
    if edited is not None:
        entries.append(
            _Entry(
                "event.update",
                "event",
                edited.id,
                edited.name,
                payload={
                    "description": edited.description,
                    "field_values_replaced": False,
                    "meta_values_replaced": False,
                },
                branch_id=branch.id,
                branch_name=branch.name,
            )
        )
    return entries


async def _alerting_entries(session: AsyncSession, ctx: DemoContext) -> list[_Entry]:
    destinations = (
        (
            await session.execute(
                # Ordered by NAME so the spread below assigns the same timestamps
                # on every seed. It ordered by ``created_at`` first, which reads
                # like chronology and is not: every row in one seed shares a
                # single server-side ``now()``, so the whole ordering fell
                # through to the uuid4 tie-break and the trail came out in a
                # different order every time (tripl-0zpq.246). ``id`` still
                # breaks a name tie, so the sort stays total.
                select(AlertDestination)
                .where(AlertDestination.project_id == ctx.project_id)
                .order_by(AlertDestination.name, AlertDestination.id)
            )
        )
        .scalars()
        .all()
    )
    # Rules hang off a destination, not off the project, so scope them through
    # the destinations just read.
    destination_ids = [row.id for row in destinations]
    rules = (
        (
            (
                await session.execute(
                    # By name, for the reason the destination order gives above.
                    select(AlertRule)
                    .where(AlertRule.destination_id.in_(destination_ids))
                    .order_by(AlertRule.name, AlertRule.id)
                )
            )
            .scalars()
            .all()
        )
        if destination_ids
        else []
    )
    return [
        _Entry("alert_destination.create", "alert_destination", row.id, row.name)
        for row in destinations
    ] + [_Entry("alert_rule.create", "alert_rule", row.id, row.name) for row in rules]


async def _authored_events(session: AsyncSession, ctx: DemoContext) -> list[Event]:
    """The events the recipe authored, oldest first.

    Selected by the ids ``plan`` published on the context, NOT by project: the
    branches builder runs before this one and deep-copies the whole plan onto a
    feature branch, so a project-wide select would file a second creation row for
    every event — naming ids on a branch nobody authored them on, and stamping no
    branch chip to say so.
    """
    if not ctx.event_ids:
        return []
    rows = (
        (await session.execute(select(Event).where(Event.id.in_(list(ctx.event_ids.values())))))
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda event: (to_utc(event.created_at), event.id))


def _event_creation_entries(events: list[Event]) -> list[tuple[_Entry, datetime]]:
    """One ``event.create`` per authored event, dated from the event itself.

    The event's own ``created_at`` is what keeps this row from contradicting the
    product around it: the detail page reads "first seen" off that column, and it
    is staggered across ~3 weeks, so a row dated from the seed instant would claim
    an event was created hours ago that the catalog shows as three weeks old — and
    would land AFTER the edits below, which are dated days back.

    ``to_utc`` on every instant read back from the database: SQLite drops the
    offset on a timezone-aware column, and a naive value cannot be compared with
    the aware ones the rest of the trail is built from.
    """
    return [
        (_Entry("event.create", "event", event.id, event.name), to_utc(event.created_at))
        for event in events
    ]


async def _event_edit_entries(
    session: AsyncSession,
    events: list[Event],
) -> list[tuple[_Entry, datetime]]:
    """One ``event.update`` per seeded edit, at the instant the edit happened.

    Derived rather than restated: the activity builder owns the story of which
    fields were edited and when, so reading it back means the audit log and the
    event's own history can never disagree about an edit they both describe —
    which is precisely the contrast the docs draw between the two surfaces (who
    and when, versus before and after).

    Grouped by (event, instant) because that is how production writes them: one
    PATCH files ONE audit row and one ``EventChange`` per field it touched, so a
    two-field edit must not become two audit rows here.

    The payload carries each new value as the history stores it — as text — so a
    JSON-valued field shows its stored form rather than a re-parsed list.
    """
    names = {event.id: event.name for event in events}
    if not names:
        return []
    changes = (
        (
            await session.execute(
                select(EventChange)
                .where(EventChange.event_id.in_(list(names)))
                .order_by(EventChange.created_at, EventChange.id)
            )
        )
        .scalars()
        .all()
    )

    grouped: dict[tuple[uuid.UUID, datetime], dict[str, object]] = defaultdict(dict)
    for change in changes:
        # ``created`` is not an edit. The activity builder files one per event,
        # because that is what ``create_event`` does, and ``event.create`` above
        # already records the same act — without this every event would carry
        # both an ``event.create`` and an ``event.update`` at the same instant,
        # for one creation (tripl-0zpq.244).
        if change.event_id in names and change.field != "created":
            grouped[(change.event_id, to_utc(change.created_at))][change.field] = change.new_value

    return [
        (
            _Entry(
                "event.update",
                "event",
                event_id,
                names[event_id],
                payload={
                    **fields,
                    # The two booleans a real PATCH always files. False here: the
                    # seeded edits change scalar fields, never a value list.
                    "field_values_replaced": False,
                    "meta_values_replaced": False,
                },
            ),
            at,
        )
        for (event_id, at), fields in grouped.items()
    ]


async def _shadow_dismissal_entries(
    session: AsyncSession,
    ctx: DemoContext,
) -> list[tuple[_Entry, datetime]]:
    """A ``shadow_event.dismiss`` behind each candidate the recipe left dismissed.

    Only for candidates whose resolution the governance builder actually recorded:
    the instant comes off ``resolved_at``, so the audit row and the candidate row
    name the same moment instead of two. A dismissal is audited at all because the
    candidate row is CASCADE-deleted with its scan and takes ``resolved_by`` with
    it (tripl-wkwv.13) — a demo showing a dismissed candidate over an empty filter
    would teach the opposite.
    """
    candidates = (
        (
            await session.execute(
                select(ShadowEventCandidate)
                .where(
                    ShadowEventCandidate.project_id == ctx.project_id,
                    ShadowEventCandidate.status == SHADOW_STATUS_DISMISSED,
                    ShadowEventCandidate.resolved_at.is_not(None),
                )
                .order_by(ShadowEventCandidate.resolved_at, ShadowEventCandidate.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        (
            _Entry(
                "shadow_event.dismiss",
                "shadow_event_candidate",
                candidate.id,
                candidate.event_name,
                payload={
                    "observed_count": candidate.observed_count,
                    # ``str(to_utc(...))``, not ``isoformat()``: the real route
                    # hands raw datetimes to ``audit_service.record``, whose
                    # ``_jsonable`` coerces them with ``json.dumps(default=str)``
                    # — a space separator, not a "T". ``to_utc`` first because
                    # sqlite returns these naive, and a payload that renders one
                    # way in the suite and another in production is a demo that
                    # teaches the wrong shape somewhere.
                    "first_seen_at": str(to_utc(candidate.first_seen_at)),
                    "last_seen_at": str(to_utc(candidate.last_seen_at)),
                    "scan_config_id": str(candidate.scan_config_id),
                    "event_type_id": (
                        str(candidate.event_type_id) if candidate.event_type_id else None
                    ),
                },
            ),
            # Narrowed to non-NULL above, so this is a real instant. ``to_utc``
            # because it comes back naive from SQLite.
            to_utc(candidate.resolved_at),
        )
        for candidate in candidates
        if candidate.resolved_at is not None
    ]


def _spread_backwards(entries: list[_Entry], *, until: datetime) -> list[tuple[_Entry, datetime]]:
    """Space entries backwards from ``until``, oldest first, newest last."""
    total = len(entries)
    return [
        (entry, until - _ENTRY_SPACING * (total - index)) for index, entry in enumerate(entries)
    ]


async def build_audit(session: AsyncSession, ctx: DemoContext) -> None:
    project = await session.get(Project, ctx.project_id)
    if project is None:
        return
    # The creator is the only real user in the seed; without one there is nobody
    # to attribute the trail to, so record nothing rather than invent an actor.
    user = await session.get(User, ctx.created_by) if ctx.created_by else None
    if user is None:
        return

    events = await _authored_events(session, ctx)

    operations: list[_Entry] = []
    if ctx.data_source_id is not None:
        data_source = await session.get(DataSource, ctx.data_source_id)
        if data_source is not None:
            operations.append(
                _Entry(
                    "data_source.create",
                    "data_source",
                    data_source.id,
                    # READ off the seeded row, never restated. The real route
                    # records ``ds.name``, and the literal here said "Demo
                    # warehouse" while the warehouse builder names the source
                    # "Demo warehouse <slug>" — one audit row naming an object
                    # that does not exist under that name (tripl-0zpq.246).
                    data_source.name,
                    project_scoped=False,
                )
            )
    if ctx.scan_config_id is not None:
        scan_config = await session.get(ScanConfig, ctx.scan_config_id)
        if scan_config is not None:
            operations.append(
                _Entry("scan_config.create", "scan_config", scan_config.id, scan_config.name)
            )
    operations += await _catalog_entries(session, ctx)
    operations += await _alerting_entries(session, ctx)
    operations += await _branch_entries(session, ctx)

    # Two anchors, because the schema had to exist before the events that use it.
    # The schema trail ends where the OLDEST event begins — derived from the data
    # rather than copied from plan.py's stagger window, so the ordering survives
    # whatever that window becomes. Connecting a warehouse and setting up alerting
    # are dated to the generation instant, which is when they really happened.
    schema_until = to_utc(events[0].created_at) if events else ctx.now
    plan_entries = _plan_entries(ctx) + await _authored_plan_entries(session, ctx)
    dated = _spread_backwards(plan_entries, until=schema_until)
    dated += _spread_backwards(operations, until=ctx.now)
    dated += _event_creation_entries(events)
    dated += await _event_edit_entries(session, events)
    dated += await _shadow_dismissal_entries(session, ctx)

    for entry, created_at in dated:
        session.add(
            AuditLog(
                user_id=user.id,
                user_email=user.email,
                # Both halves together: the id is what a purge finds the row by,
                # the slug is what the Audit tab filters on. An instance-scoped
                # row carries neither, exactly as its real route records it.
                project_id=project.id if entry.project_scoped else None,
                project_slug=project.slug if entry.project_scoped else "",
                # Every row, instance-scoped ones included, belongs to the
                # demo's organization: left to the column default it would land
                # in the default organization's feed (F20 PR5).
                organization_id=project.organization_id,
                # A branch on the ONE row the recipe really authored on the
                # feature branch — the event edit — and on nothing else. Creating
                # the branch is itself an action on main
                # (``POST /projects/{slug}/branches`` takes no ``?branch=``), so
                # ``plan_branch.create`` carries none either here or in
                # ``api/v1/plan_branches.py``. Main is spelled as the ABSENCE of
                # a branch (see ``audit_service.record``) — a chip reading "main"
                # would be the one thing the audit UI promises never to show.
                branch_id=entry.branch_id,
                branch_name=entry.branch_name,
                action=entry.action,
                target_type=entry.target_type,
                target_id=entry.target_id,
                target_name=entry.target_name,
                created_at=created_at,
                payload={**(entry.payload or {}), "demo_seed": True, "note": _SOURCE_NOTE},
            )
        )
    await session.flush()
