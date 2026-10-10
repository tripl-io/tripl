"""What a dead event is: in the plan as implemented or live, with no data lately.

One definition for every reader. Govern -> Reconciliation -> Dead events lists
them (``reconciliation_service.list_dead_events``, on an ``AsyncSession``) and
the weekly plan digest counts them as "Dead implemented events"
(``worker.tasks.alerts_messages``, on a sync ``Session``). The digest is the
number a reader is mailed and the page is where they act on it, so the two
cannot be allowed to disagree. They used to: the digest counted every
implemented event that had never been seen, so a new project's first digests
reported events as dead that the page did not list.

The clause is plain SQLAlchemy over ``Event``, so it runs on either session
kind; callers add their own project and branch scope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

from tripl.models.event import Event, EventStatus

#: Days without data before an implemented or live event counts as dead.
DEAD_EVENT_DAYS: Final = 30

_DEAD_CANDIDATE_STATUSES: Final = (EventStatus.implemented.value, EventStatus.live.value)


def dead_event_clause(cutoff: datetime) -> ColumnElement[bool]:
    """Implemented or live events with no data since ``cutoff``.

    An event that HAS been seen and then went quiet is dead regardless of when
    its plan row was written. The grace covers only the never-seen case, where
    a freshly authored event legitimately has no data yet: it counts once its
    plan row is older than the cutoff. Gating both cases on ``created_at`` would
    hide a genuinely stale event behind a young plan row.
    """
    return and_(
        Event.status.in_(_DEAD_CANDIDATE_STATUSES),
        or_(
            and_(Event.last_seen_at.is_(None), Event.created_at < cutoff),
            Event.last_seen_at < cutoff,
        ),
    )
