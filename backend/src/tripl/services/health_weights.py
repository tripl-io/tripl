"""Fixed weights and thresholds of the event health score (F15, #268).

v1 decision: the weights are NOT configurable per project. Every number the
score depends on lives here as a named constant, so changing one is a one-line
diff reviewed like code, and the docs table (website/docs/use/feature-reference.md,
"Health score") can be checked against a single file.
"""

from __future__ import annotations

from typing import Final, Literal

HealthComponentKey = Literal[
    "implemented_seen", "contract", "drifts", "signals", "freshness", "documentation"
]
HealthGrade = Literal["healthy", "warning", "unhealthy"]

KEY_IMPLEMENTED_SEEN: Final = "implemented_seen"
KEY_CONTRACT: Final = "contract"
KEY_DRIFTS: Final = "drifts"
KEY_SIGNALS: Final = "signals"
KEY_FRESHNESS: Final = "freshness"
KEY_DOCUMENTATION: Final = "documentation"

# Component weights. They sum to 100; when a component does not apply to an
# event the remaining ones are renormalized over their own sum.
WEIGHT_IMPLEMENTED_SEEN: Final = 25
WEIGHT_CONTRACT: Final = 20
WEIGHT_DRIFTS: Final = 15
WEIGHT_SIGNALS: Final = 15
WEIGHT_FRESHNESS: Final = 10
WEIGHT_DOCUMENTATION: Final = 15

# Each open drift costs this share of the drifts component.
DRIFT_PENALTY: Final = 0.25
# Each open, unverdicted signal costs this share of the signals component.
SIGNAL_PENALTY: Final = 0.5

# "Seen recently" and "stale" windows for implemented/live events. The stale
# window equals the weekly digest's DEAD_EVENT_DAYS.
SEEN_RECENT_DAYS: Final = 7
SEEN_STALE_DAYS: Final = 30
SEEN_STALE_VALUE: Final = 0.5
# Distribution drifts count when their bucket is inside this window.
DISTRIBUTION_WINDOW_DAYS: Final = 7
# A scan covers an event when it wrote a metric for it inside this window.
COVERAGE_LOOKBACK_DAYS: Final = 30

# Source freshness status -> component value. ``unknown`` is not listed: a
# config with unknown freshness is ignored, and no known config means excluded.
FRESHNESS_VALUE: Final[dict[str, float]] = {"fresh": 1.0, "late": 0.5, "overdue": 0.0}

# Lifecycle findings on a deprecated event -> implemented_seen value.
LIFECYCLE_SUNSET_OVERDUE_VALUE: Final = 0.0
LIFECYCLE_SUCCESSOR_SILENT_VALUE: Final = 0.5

GRADE_HEALTHY_MIN: Final = 80
GRADE_WARNING_MIN: Final = 50

WORST_EVENTS_LIMIT: Final = 5
SNAPSHOT_RETENTION_DAYS: Final = 400
# The digest reads a snapshot only when it is at most this many days old.
SNAPSHOT_FRESH_DAYS: Final = 2
# ``previous_score`` and the digest delta compare against this many days back.
TREND_DELTA_DAYS: Final = 7

COMPONENT_ORDER: Final[tuple[HealthComponentKey, ...]] = (
    "implemented_seen",
    "contract",
    "drifts",
    "signals",
    "freshness",
    "documentation",
)

COMPONENT_WEIGHTS: Final[dict[HealthComponentKey, int]] = {
    "implemented_seen": WEIGHT_IMPLEMENTED_SEEN,
    "contract": WEIGHT_CONTRACT,
    "drifts": WEIGHT_DRIFTS,
    "signals": WEIGHT_SIGNALS,
    "freshness": WEIGHT_FRESHNESS,
    "documentation": WEIGHT_DOCUMENTATION,
}

COMPONENT_LABELS: Final[dict[HealthComponentKey, str]] = {
    "implemented_seen": "Implemented & seen",
    "contract": "Contract",
    "drifts": "Drifts",
    "signals": "Signals",
    "freshness": "Freshness",
    "documentation": "Documentation",
}

# Status groups. Archived events are outside the scored population entirely.
PLANNED_STATUSES: Final = frozenset({"draft", "in_review", "ready_for_dev"})
SHIPPED_STATUSES: Final = frozenset({"implemented", "live", "deprecated"})
