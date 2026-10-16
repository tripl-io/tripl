"""Is a demo PAUSED? — the single definition, shared by every worker path.

A demo whose last explicit activity is older than :data:`DEMO_IDLE_PAUSE_MINUTES`
is paused: it stops consuming worker time until someone opens it again
(``project_service.get_project`` touches ``Project.demo_last_accessed_at``, which
resumes it on the next beat tick).

Why the rule lives in a module of its own
-----------------------------------------
Both ``worker.tasks.metrics.schedule.check_metrics_due`` (which dispatches a
demo's scheduled collection) and ``worker.tasks.metrics.freshness_sweep`` (which
raises "scan overdue" for a collection that stopped) have to answer this
question, and they MUST answer it identically: a demo the dispatcher stopped
collecting because nobody looks at it is not overdue, and an overdue alert for
it would be noise about the pause itself. One four-line rule in a leaf module
keeps both — and the Enterprise demo pool, which keeps its waiting demos
unpaused — from each testing idleness their own way.

Resuming is safe: a demo reads the shared demo warehouse, which keeps every
hour of its retention, so the first collection after a pause catches the gap up
from real rows (clamped to what the warehouse still holds,
``metrics.demo_window``).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tripl.core.bucketing import to_utc

# A demo whose last explicit access (falling back to seed time) is older than this
# is PAUSED — the scheduler skips it so a demo nobody is looking at stops
# consuming worker time. The next access (``demo_last_accessed_at`` touched on GET
# / reset) resumes it and the next scheduled collection catches it up.
#
# Comfortably wider than ``project_service._DEMO_ACCESS_TOUCH_SECONDS`` (60 s),
# the throttle on how often an access rewrites that column, so someone actually
# looking at a demo can never let it lapse between two touches.
DEMO_IDLE_PAUSE_MINUTES = 6 * 60


def is_demo_paused(
    seeded_at: datetime | None,
    last_accessed: datetime | None,
    now: datetime,
) -> bool:
    """Whether a demo's most-recent activity is older than the idle window.

    Activity = last explicit access, falling back to seed time for a demo nobody
    has opened yet (so a fresh demo is active for one idle window before pausing).
    A demo with neither stamp has no evidence of activity at all and reads as
    paused — in production that is a demo whose seeding has not finished, which
    nothing should be advancing or collecting behind the seeder's back.

    ``to_utc`` rather than a local tz-normaliser because the two stamps arrive
    tz-aware from PostgreSQL and naive from SQLite (tests), and the comparison has
    to happen on one timeline either way.
    """
    activity = last_accessed or seeded_at
    if activity is None:
        return True
    return to_utc(activity) < now - timedelta(minutes=DEMO_IDLE_PAUSE_MINUTES)
