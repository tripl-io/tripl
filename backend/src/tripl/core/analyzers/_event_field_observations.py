"""The values one scan saw for an event field, kept when they disagree.

``generate_events`` collapses every breakdown row of one scan identity onto one
event, and an event holds one value per field: the busiest row's. When the rows
disagree — a structured event fired on several screens — the other values used
to survive only as a line in the run summary. This module turns the run's
per-field tally into ``EventFieldObservation`` rows the event page can show.

The rule, per (event, field) the run observed:

* more than one distinct value — the row is replaced with this run's tally;
* exactly one — the row is deleted, because the field stopped varying;
* not observed this run (archived, past ``max_events``, merged away, or a run
  told not to record) — the row is left alone.

Counts are never added across runs: scan windows overlap, and a sum would count
the same rows twice. "The last run that observed it" is the whole contract, and
the row carries ``observed_at`` and ``scan_config_id`` so the page can say which.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.models.event import Event
from tripl.models.event_field_observation import EventFieldObservation

#: Values kept per row — the same budget as a variable's value samples.
OBSERVED_VALUE_LIMIT = 20
#: A stored value is cut to this many characters; the field values it mirrors
#: are free text and one runaway string must not bloat the JSON.
OBSERVED_VALUE_MAX_LENGTH = 255

#: ``(event identity, column)`` -> value -> summed row count, ``None`` once any
#: row carrying that value came back without a count.
ValuesSeen = dict[tuple[str, str], dict[str, int | None]]


def tally_value(
    values_seen: ValuesSeen, key: tuple[str, str], value: str, row_count: int | None
) -> None:
    """Add one breakdown row's ``value`` to the run's tally for ``key``.

    An unknown count poisons the value's sum rather than reading as zero: a
    share computed from a partial sum would be confidently wrong.
    """
    counts = values_seen.setdefault(key, {})
    if value not in counts:
        counts[value] = row_count
        return
    current = counts[value]
    counts[value] = None if current is None or row_count is None else current + row_count


def build_distribution(
    counts: Mapping[str, int | None], *, limit: int = OBSERVED_VALUE_LIMIT
) -> tuple[list[dict[str, Any]], int, int | None]:
    """``(values, distinct_count, total_count)`` for one field's tally.

    ``values`` is busiest first — unknown counts last, then by value so the
    order is stable — and holds at most ``limit`` entries. ``distinct_count``
    and ``total_count`` describe the FULL tally, so a reader can still say
    "+N more" and compute true shares; ``total_count`` is ``None`` when any
    count is unknown.
    """
    ordered = sorted(
        counts.items(),
        key=lambda item: (item[1] is None, -(item[1] or 0), item[0]),
    )
    values = [
        {"value": value[:OBSERVED_VALUE_MAX_LENGTH], "count": count}
        for value, count in ordered[:limit]
    ]
    known = [count for count in counts.values() if count is not None]
    total = sum(known) if len(known) == len(counts) else None
    return values, len(counts), total


def record_field_observations(
    session: Session,
    *,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID | None,
    observed_fields: Mapping[uuid.UUID, Mapping[uuid.UUID, tuple[str, str]]],
    values_seen: ValuesSeen,
) -> int:
    """Replace or delete the observation of every (event, field) the run observed.

    ``observed_fields`` maps event id -> field definition id -> the
    ``values_seen`` key the run tallied that field under. Only events the run
    actually wrote belong in it, so archived ones keep whatever they had.

    Called after the merge pass, which may have deleted some of those events —
    their rows went with them by ``ON DELETE CASCADE``, so they are filtered out
    here rather than written against a missing parent.

    Returns the number of rows written (inserted or replaced).
    """
    if not observed_fields:
        return 0
    event_ids = set(
        session.execute(select(Event.id).where(Event.id.in_(list(observed_fields)))).scalars()
    )
    if not event_ids:
        return 0
    existing = {
        (row.event_id, row.field_definition_id): row
        for row in session.execute(
            select(EventFieldObservation).where(EventFieldObservation.event_id.in_(event_ids))
        ).scalars()
    }
    observed_at = datetime.now(UTC)
    written = 0
    for event_id in event_ids:
        for fd_id, key in observed_fields[event_id].items():
            counts = values_seen.get(key, {})
            row = existing.get((event_id, fd_id))
            if len(counts) <= 1:
                if row is not None:
                    session.delete(row)
                continue
            values, distinct_count, total_count = build_distribution(counts)
            if row is None:
                row = EventFieldObservation(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    event_id=event_id,
                    field_definition_id=fd_id,
                )
                session.add(row)
            row.scan_config_id = scan_config_id
            row.values = values
            row.distinct_count = distinct_count
            row.total_count = total_count
            row.observed_at = observed_at
            written += 1
    return written
