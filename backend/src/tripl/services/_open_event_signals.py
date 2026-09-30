"""Open, significant, unverdicted EVENT-scope signals per event (F15, #268).

The health score's "signals" component counts what the sidebar badge counts,
restricted to ``scope_type = event`` and the requested events. Both read the
same implementation, ``_open_signals.open_counted_scan_signals``; this module
only narrows it to the event scope and counts the rows per event.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT
from tripl.services._open_signals import open_counted_scan_signals


async def open_unverdicted_event_signals(
    session: AsyncSession,
    project_id: uuid.UUID,
    event_ids: Sequence[uuid.UUID],
    *,
    now: datetime | None = None,
) -> dict[uuid.UUID, int]:
    """Per event, how many of its event-scope signals the badge would count.

    Events with none are absent from the mapping.
    """
    if not event_ids:
        return {}
    wanted = {str(event_id): event_id for event_id in event_ids}
    rows = await open_counted_scan_signals(
        session, [project_id], (SCOPE_EVENT,), event_ids=list(wanted.values()), now=now
    )
    counts: dict[uuid.UUID, int] = {}
    for row in rows:
        event_id = wanted[row.anomaly.scope_ref]
        counts[event_id] = counts.get(event_id, 0) + 1
    return counts
