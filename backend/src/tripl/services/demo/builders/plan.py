"""Plan-schema builder: the design canvas of the demo.

Authors the tracking plan a team would have written before any traffic was
read: event types and their fields, meta fields, every event of the catalog
(descriptions, statuses, tags, field values, meta values, successors), one
event-type relation and — when a real creator is known — the event-type owners.

Everything comes from :mod:`tripl.core.demo_warehouse.roster`, the one place
the demo catalog is written down; the warehouse emits its rows from the same
records, so a plan event and the rows that should match it cannot drift apart.
Nothing observed is written here (no volumes, no first/last seen), with one
stated exception: the dead event's ``last_seen_at``, which lies before the
history any warehouse keeps (see :data:`DEAD_EVENT_LAST_SEEN`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.demo_warehouse import roster
from tripl.core.demo_warehouse.roster import EventDef
from tripl.models.event import Event
from tripl.models.event_field_value import EventFieldValue
from tripl.models.event_meta_value import EventMetaValue
from tripl.models.event_tag import EventTag
from tripl.models.event_type import EventType
from tripl.models.event_type_owner import EventTypeOwner
from tripl.models.event_type_relation import EventTypeRelation
from tripl.models.field_definition import FieldDefinition
from tripl.models.meta_field_definition import MetaFieldDefinition
from tripl.services.demo import noise
from tripl.services.demo.scenario import DemoContext

#: The warehouse column a demo event is named by. Every event type carries it
#: as a field because the scan's name format ``{event_name}`` reads it: a
#: column the name format uses must be a field of the type
#: (``reserved_catalog_columns``), and authoring it here gives it a real label
#: instead of the scan's "Auto-created (text)".
EVENT_NAME_FIELD = roster.FieldDef(
    "event_name",
    "Event name",
    "string",
    "The name the apps send with the event; the scan names catalog events by it.",
    is_required=True,
)
#: Every row carries the user's country; the plan documents it once per type.
COUNTRY_FIELD = roster.FieldDef(
    "country",
    "Country",
    "string",
    "ISO country code of the user's store account.",
)

# (name, display_name, field_type, link_template)
_META_FIELD_SPECS: tuple[tuple[str, str, str, str | None], ...] = (
    ("jira", "Jira ticket", "url", "https://jira.example.com/browse/${value}"),
    ("owner_team", "Owner team", "string", None),
)

#: The dead event was last seen this long before the seed: older than any
#: warehouse window, so no scan can observe it and the plan carries it.
DEAD_EVENT_LAST_SEEN = timedelta(days=45)
#: A deprecated event's announced sunset, already behind us: the lifecycle
#: watch flags it as overdue.
DEPRECATED_SUNSET_AGO = timedelta(days=3)

# "Created" stagger: the core backbone dates from the start of the history,
# the rest of the catalog was written over the following weeks, and what is
# still being designed was written recently.
_CORE_TAG = "core"
_OLDEST_AGE_DAYS = 21.0
_YOUNGEST_AGE_DAYS = 2.5
_PLANNED_STATUSES = frozenset({"draft", "in_review", "ready_for_dev"})


def event_fields(event_type_name: str) -> tuple[roster.FieldDef, ...]:
    """The fields the plan documents for one event type, in display order."""
    own = roster.event_type(event_type_name).fields
    return (EVENT_NAME_FIELD, *own, COUNTRY_FIELD)


def created_at_for(
    event: EventDef, rank: int, last_rank: int, *, now: datetime, seed: int
) -> datetime:
    """When the plan says an event was written (deterministic per event name)."""
    jitter = noise.derive_seed(seed, f"first_seen:{event.name}")
    if event.name == roster.DEAD_EVENT:
        return now - DEAD_EVENT_LAST_SEEN - timedelta(days=30)
    if _CORE_TAG in event.tags:
        return noise.history_start(now) + timedelta(hours=jitter % 12)
    if event.status in _PLANNED_STATUSES:
        # Being designed now: written within the last fortnight.
        return now - timedelta(days=1 + (jitter % 13), hours=jitter % 7)
    age = _OLDEST_AGE_DAYS - (rank / max(last_rank, 1)) * (_OLDEST_AGE_DAYS - _YOUNGEST_AGE_DAYS)
    return now - timedelta(days=age, hours=(jitter % 13) - 6)


async def build_plan(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_event_types(session, ctx)
    await _build_meta_fields(session, ctx)
    await _build_events(session, ctx)
    await _build_relation(session, ctx)
    await _build_owners(session, ctx)


async def _build_event_types(session: AsyncSession, ctx: DemoContext) -> None:
    for order, et_def in enumerate(roster.event_types()):
        et = EventType(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            name=et_def.name,
            display_name=et_def.display_name,
            description=et_def.description,
            color=et_def.color,
            order=order,
        )
        session.add(et)
        await session.flush()
        ctx.event_type_ids[et_def.name] = et.id
        for field_order, field_def in enumerate(event_fields(et_def.name)):
            fd = FieldDefinition(
                event_type_id=et.id,
                name=field_def.name,
                display_name=field_def.display_name,
                field_type=field_def.field_type,
                description=field_def.description,
                is_required=field_def.is_required,
                enum_options=list(field_def.enum_options) if field_def.enum_options else None,
                order=field_order,
            )
            session.add(fd)
            await session.flush()
            ctx.field_ids[f"{et_def.name}.{field_def.name}"] = fd.id


async def _build_meta_fields(session: AsyncSession, ctx: DemoContext) -> None:
    for order, (name, display_name, field_type, link_template) in enumerate(_META_FIELD_SPECS):
        mf = MetaFieldDefinition(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            name=name,
            display_name=display_name,
            field_type=field_type,
            link_template=link_template,
            order=order,
        )
        session.add(mf)
        await session.flush()
        ctx.meta_field_ids[name] = mf.id


async def _build_events(session: AsyncSession, ctx: DemoContext) -> None:
    catalog = roster.events()
    non_core = [event for event in catalog if _CORE_TAG not in event.tags]
    rank_of = {event.name: rank for rank, event in enumerate(non_core)}
    rows: list[tuple[EventDef, Event]] = []
    for order, event_def in enumerate(catalog):
        event = Event(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            event_type_id=ctx.event_type_ids[event_def.event_type],
            name=event_def.name,
            description=event_def.description,
            order=order,
            status=event_def.status,
            sunset_at=(ctx.now - DEPRECATED_SUNSET_AGO)
            if event_def.status == "deprecated"
            else None,
            last_seen_at=(
                ctx.now - DEAD_EVENT_LAST_SEEN if event_def.name == roster.DEAD_EVENT else None
            ),
            created_at=created_at_for(
                event_def,
                rank_of.get(event_def.name, 0),
                len(non_core) - 1,
                now=ctx.now,
                seed=ctx.seed,
            ),
            metric_breakdown_columns=list(event_def.metric_breakdown_columns),
        )
        session.add(event)
        rows.append((event_def, event))
    await session.flush()

    for event_def, event in rows:
        ctx.event_ids[event_def.name] = event.id
        values = dict(event_def.field_values)
        values.setdefault("event_name", event_def.name)
        # Every row carries a country; authored as the variable, the plan does
        # not pin the event to the busiest country the scan happened to see.
        values.setdefault("country", "${country}")
        for field_name, value in values.items():
            field_id = ctx.field_ids.get(f"{event_def.event_type}.{field_name}")
            if field_id is None:
                continue
            # Hand-authored, exactly as the event editor stamps it: a scan never
            # rewrites an authored value, so the documented ``${product_id}`` /
            # ``${platform}`` templates survive the first scan.
            session.add(
                EventFieldValue(
                    event_id=event.id, field_definition_id=field_id, value=value, is_authored=True
                )
            )
        for tag in event_def.tags:
            session.add(EventTag(event_id=event.id, name=tag))
        for meta_name, value in event_def.meta:
            session.add(
                EventMetaValue(
                    event_id=event.id,
                    meta_field_definition_id=ctx.meta_field_ids[meta_name],
                    value=value,
                )
            )
    await session.flush()

    # Successors last: both ends must exist.
    for event_def, event in rows:
        if event_def.successor is not None:
            event.superseded_by_event_id = ctx.event_ids[event_def.successor]
    await session.flush()


async def _build_relation(session: AsyncSession, ctx: DemoContext) -> None:
    # A click belongs to the screen it happened on, joined on ``screen_name``.
    session.add(
        EventTypeRelation(
            project_id=ctx.project_id,
            branch_id=ctx.branch_id,
            source_event_type_id=ctx.event_type_ids["click"],
            target_event_type_id=ctx.event_type_ids["screen_view"],
            source_field_id=ctx.field_ids["click.screen_name"],
            target_field_id=ctx.field_ids["screen_view.screen_name"],
            relation_type="belongs_to",
            description="A click belongs to the screen view it happened on.",
        )
    )
    await session.flush()


async def _build_owners(session: AsyncSession, ctx: DemoContext) -> None:
    # Owners reference a real user; the creator is the only one in the seed.
    if ctx.created_by is None:
        return
    for event_type_name in ("screen_view", "purchase"):
        session.add(
            EventTypeOwner(
                event_type_id=ctx.event_type_ids[event_type_name],
                user_id=ctx.created_by,
                granted_by=ctx.created_by,
            )
        )
    await session.flush()
