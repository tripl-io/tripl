"""Attach the values a scan saw for each event field (``EventFieldObservation``).

Scans write observations for MAIN-branch events only. A branch copy borrows its
main twin's — found the way ``last_seen_at`` and metrics are, through
``_branch_counterparts.main_counterparts`` — and matches fields by NAME,
because the branch deep-copied its field definitions under new ids. Nothing is
written to the branch, so its page always shows main's current distribution,
dated, rather than a copy frozen at branch creation.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.event import Event
from tripl.models.event_field_observation import EventFieldObservation
from tripl.models.field_definition import FieldDefinition
from tripl.services._branch_counterparts import main_counterparts


def observed_values_payload(row: EventFieldObservation) -> dict[str, Any]:
    """The response shape of one row, with shares and the remainder computed here.

    ``share`` divides by ``total_count`` — the FULL set, values past the cap
    included — so the shares of the kept values may add up to less than one,
    and ``other_count`` says by how much.
    """
    total = row.total_count
    values = [
        {
            "value": item["value"],
            "count": item.get("count"),
            "share": (item["count"] / total if total and item.get("count") is not None else None),
        }
        for item in row.values or []
    ]
    other_count: int | None = None
    if total is not None:
        other_count = max(total - sum(item["count"] or 0 for item in values), 0)
    return {
        "distinct_count": row.distinct_count,
        "total_count": total,
        "values": values,
        "other_count": other_count,
        "observed_at": row.observed_at,
        "scan_config_id": row.scan_config_id,
    }


async def attach_event_field_observations(
    session: AsyncSession, *, project_id: uuid.UUID, events: Sequence[Event]
) -> None:
    """Set ``field_value.observed_values`` on every field value of ``events``.

    One query for the rows, one for the twins (branch events only) and, when a
    branch event is involved, one for the field names. A field with no row
    reads ``None``.
    """
    if not events:
        return
    twins = await main_counterparts(session, project_id=project_id, events=events)
    source_of = {ev.id: twins[ev.id].id if ev.id in twins else ev.id for ev in events}
    rows = (
        (
            await session.execute(
                select(EventFieldObservation).where(
                    EventFieldObservation.event_id.in_(set(source_of.values()))
                )
            )
        )
        .scalars()
        .all()
    )
    by_field = {(row.event_id, row.field_definition_id): row for row in rows}

    # Branch copies match by field name; only they need the names.
    names: dict[uuid.UUID, str] = {}
    if rows and twins:
        field_ids = {row.field_definition_id for row in rows}
        for ev in events:
            if ev.id in twins:
                field_ids |= {fv.field_definition_id for fv in ev.field_values}
        names = {
            fd_id: name
            for fd_id, name in await session.execute(
                select(FieldDefinition.id, FieldDefinition.name).where(
                    FieldDefinition.id.in_(field_ids)
                )
            )
        }
    by_name = {
        (row.event_id, names[row.field_definition_id]): row
        for row in rows
        if row.field_definition_id in names
    }

    for ev in events:
        source_id = source_of[ev.id]
        for field_value in ev.field_values:
            if ev.id in twins:
                row = by_name.get((source_id, names.get(field_value.field_definition_id, "")))
            else:
                row = by_field.get((source_id, field_value.field_definition_id))
            field_value.observed_values = (  # type: ignore[attr-defined]
                observed_values_payload(row) if row is not None else None
            )
