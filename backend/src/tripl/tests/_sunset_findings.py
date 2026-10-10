"""Seed the row the daily sunset watch writes for an overdue deprecated event.

Not a test module. The daily sunset alert and the weekly digest's "Deprecated
events still receiving data" counter read the sunset watch's open
``sunset_overdue`` findings (``alerts_messages._sunset_overdue_scope``), so a
test that wants either message to name an event seeds the finding the sweep
(``tasks.lifecycle.compute_lifecycle_findings``) would have written for it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from tripl.models.event import Event
from tripl.models.lifecycle_finding import LifecycleFinding, LifecycleFindingKind


def open_sunset_finding(event: Event, *, at: datetime, volume_24h: int = 120) -> LifecycleFinding:
    """An open ``sunset_overdue`` finding on ``event``, as of ``at``.

    The sweep only judges MAIN-branch events; seed this for a main event only.
    """
    return LifecycleFinding(
        id=uuid.uuid4(),
        project_id=event.project_id,
        event_id=event.id,
        kind=LifecycleFindingKind.sunset_overdue.value,
        first_seen_at=at,
        last_seen_at=at,
        resolved_at=None,
        volume_24h=volume_24h,
    )
