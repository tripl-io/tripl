"""A branch event's twin on main, and what the branch borrows from it.

A working branch deep-copies every event under a NEW id (``deep_copy_plan_to_branch``),
and everything that arrives from a warehouse — ``event_metrics`` rows,
``last_seen_at`` bumps, anomalies — lands on the MAIN row, because scans only
ever see main. So a branch copy of a live event read "never seen, no volume"
on every page, and an analyst on a branch could not tell a dead event from a
busy one.

A branch copy's twin is the main row it was copied from (``origin_id``). Only a
row without one — created on the branch — or a copy whose origin main has since
deleted pairs the old way: same event type NAME (a branch type has its own id
too), and the same scan identity — ``source_name`` where the row has one,
``name`` where it does not, on both
sides. Nothing is written back; the branch row only reads through to its twin.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from tripl.models.event import Event
from tripl.models.event_type import EventType
from tripl.services.plan_branch_service import ensure_main_branch_id


def _identity(event: Event) -> str:
    return event.source_name or event.name


async def main_counterparts(
    session: AsyncSession, *, project_id: uuid.UUID, events: Sequence[Event]
) -> dict[uuid.UUID, Event]:
    """Branch event id → its main-branch twin, for the events that have one.

    Events already on main are not in the result. Three queries at most,
    whatever the batch size; nothing when every event is on main.
    """
    main_branch_id = await ensure_main_branch_id(session, project_id)
    branch_events = [ev for ev in events if ev.branch_id != main_branch_id]
    if not branch_events:
        return {}
    # The row the copy was made from, when there is one. Keyed by type and
    # identity instead, two main rows sharing that key answered for each other:
    # the discussion, the metrics and the merge's thread move all read through
    # whichever of them the key kept.
    out: dict[uuid.UUID, Event] = {}
    origin_ids = {ev.origin_id for ev in branch_events if ev.origin_id is not None}
    if origin_ids:
        origins = {
            row.id: row
            for row in (
                await session.execute(
                    select(Event).where(
                        Event.id.in_(origin_ids),
                        Event.branch_id == main_branch_id,
                    )
                )
            )
            .scalars()
            .all()
        }
        for ev in branch_events:
            origin = origins.get(ev.origin_id) if ev.origin_id is not None else None
            if origin is not None:
                out[ev.id] = origin
    branch_events = [ev for ev in branch_events if ev.id not in out]
    if not branch_events:
        return out
    type_names: dict[uuid.UUID, str] = {
        type_id: name
        for type_id, name in (
            await session.execute(
                select(EventType.id, EventType.name).where(
                    EventType.id.in_({ev.event_type_id for ev in branch_events})
                )
            )
        ).all()
    }
    main_type_by_name: dict[str, uuid.UUID] = {
        name: type_id
        for name, type_id in (
            await session.execute(
                select(EventType.name, EventType.id).where(
                    EventType.project_id == project_id,
                    EventType.branch_id == main_branch_id,
                    EventType.name.in_(set(type_names.values())),
                )
            )
        ).all()
    }
    if not main_type_by_name:
        return out
    identities = {_identity(ev) for ev in branch_events}
    main_rows = (
        (
            await session.execute(
                select(Event)
                .where(
                    Event.branch_id == main_branch_id,
                    Event.event_type_id.in_(set(main_type_by_name.values())),
                    or_(Event.source_name.in_(identities), Event.name.in_(identities)),
                )
                # Ordered so the LOWEST id wins the key below, which is the rule
                # ``event_service._twin_reads_for_branch_rows`` picks the twin by
                # for the "Silent > N days" filter and the "Busiest first" sort.
                # Nothing stops main holding two rows under one (type, identity)
                # — nothing refuses that state — and while this query was
                # unordered the rendered Last seen came from whichever row it
                # happened to return last, so the filter and the column could
                # answer about two different main rows on the same branch row.
                .order_by(Event.id.asc())
            )
        )
        .scalars()
        .all()
    )
    main_type_name = {type_id: name for name, type_id in main_type_by_name.items()}
    # ``setdefault``, not a dict comprehension: a comprehension keeps the LAST
    # row under a repeated key, which with the ascending sort above would be the
    # highest id — the opposite of the rule.
    main_by_key: dict[tuple[str, str], Event] = {}
    for row in main_rows:
        main_by_key.setdefault((main_type_name[row.event_type_id], _identity(row)), row)
    for ev in branch_events:
        twin = main_by_key.get((type_names.get(ev.event_type_id, ""), _identity(ev)))
        if twin is not None:
            out[ev.id] = twin
    return out


async def attach_main_last_seen(
    session: AsyncSession, *, project_id: uuid.UUID, events: Sequence[Event]
) -> None:
    """Show a branch copy the ``last_seen_at`` its main twin has earned.

    Set as a COMMITTED value, not an assignment: the read paths that call this
    are also what ``update_event`` loads its row through, and a plain
    assignment would ride the next flush into the branch row as a write nobody
    made. The committed value renders in the response and evaporates on
    refresh.
    """
    twins = await main_counterparts(session, project_id=project_id, events=events)
    for ev in events:
        twin = twins.get(ev.id)
        if twin is None or twin.last_seen_at is None:
            continue
        if ev.last_seen_at is None or twin.last_seen_at > ev.last_seen_at:
            set_committed_value(ev, "last_seen_at", twin.last_seen_at)


async def metrics_row_for(session: AsyncSession, *, project_id: uuid.UUID, event: Event) -> Event:
    """The row metrics are keyed on: the main twin for a branch copy, else itself."""
    twins = await main_counterparts(session, project_id=project_id, events=[event])
    return twins.get(event.id, event)
