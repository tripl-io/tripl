"""Is a demo PAUSED? — the single definition, shared by every worker path.

A demo whose last explicit activity is older than :data:`DEMO_IDLE_PAUSE_MINUTES`
is paused: it stops consuming worker time until someone opens it again
(``project_service.get_project`` touches ``Project.demo_last_accessed_at``, which
resumes it on the next beat tick).

Why the rule lives in a module of its own
-----------------------------------------
Both ``worker.tasks.demo_runtime.advance_demos`` (which appends the demo's hourly
buckets) and ``worker.tasks.metrics.schedule.check_metrics_due`` (which dispatches
its scheduled collection) have to answer this question, and they MUST answer it
identically — see below. Importing the predicate straight from ``demo_runtime``
would be acyclic today, but it would drag ``tripl.cache``, ``tripl.realtime``, the
anomaly detector and three demo builders into the dispatcher's import graph for
one four-line rule. This subsystem already parks its shared rules in leaf modules
for exactly that reason: ``core.collection_progress`` and ``core.bucketing`` each
open by naming the several callers that must agree on them.

Why the two callers must not each roll their own idleness test
------------------------------------------------------------------------------
The tick is what keeps the newest ``EventMetric`` bucket moving. While it is
paused that bucket freezes, so ``collection_progress_to`` — the resume point the
collector derives its window start from — freezes with it, and the next scheduled
collection's window stretches back to cover the whole gap instead of the usual two
buckets. Against the synthetic demo warehouse that is actively destructive: the
adapter only materialises its newest ``SYNTHETIC_ONGOING_HOURS`` at full volume
and emits a handful of sampled rows per hour before that, while the collector
deletes each chunk window before rewriting it. A window reaching behind the
ongoing hours therefore replaces hours of real history with near-zero counts, over
and over, for as long as the demo stays paused. Two thresholds that drift apart
re-open precisely that window.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tripl.core.bucketing import to_utc

# A demo whose last explicit access (falling back to seed time) is older than this
# is PAUSED — the tick skips it so a demo nobody is looking at stops consuming
# worker time. The next access (``demo_last_accessed_at`` touched on GET / reset)
# resumes it and the next tick catches it up.
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
