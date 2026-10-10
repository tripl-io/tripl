"""Deterministic shape helpers for the demo scenario.

Pure functions only — no DB, no I/O. Every value the demo seeds is derived from
``(clock, seed)`` through these helpers, so re-running the recipe with the same
clock and seed produces an identical data shape.

The hourly volume shape and the platform-drift curve live in
:mod:`tripl.core.adapters.synthetic_traffic`, beside the version and platform
mix, because the synthetic warehouse has to serve the same traffic the seeder
stores and ``core`` does not import ``services``. They are re-exported here
under the names the builders have always used.

Determinism note: per-series noise is derived with :func:`derive_seed` (SHA-256)
rather than the Python builtin ``hash()``. ``hash()`` is salted per-process for
``str``/``bytes`` (PYTHONHASHSEED), so keying noise off ``hash(str)`` would make
the seeded metrics/anomalies/drift non-reproducible across runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tripl.core.adapters import synthetic_traffic
from tripl.core.adapters.synthetic import SPIKE_MULTIPLIER
from tripl.core.analyzers.anomaly_detector import AnomalyDetectionSettings

# Wall-clock length of the seeded hourly history (23 days: three weekly cycles
# for the detector's phase baseline plus the 48h evaluation window — see
# ``synthetic_traffic.DEMO_HISTORY_DAYS``).
DEMO_HISTORY_DAYS = synthetic_traffic.DEMO_HISTORY_DAYS
DEMO_EVAL_WINDOW_HOURS = 48
# The synthetic source reproduces the spike at the same multiple, so it owns it.
DEMO_SPIKE_MULTIPLIER = SPIKE_MULTIPLIER

# Distribution-drift showcase: the platform mix drifts only over the final
# ``DEMO_DRIFT_SPAN_DAYS`` days so the real PSI climbs a stable -> minor ->
# significant ladder against the window-start baseline. The stored platform
# breakdowns drift along the same curve (``synthetic_traffic``).
DEMO_DRIFT_SPAN_DAYS = synthetic_traffic.DRIFT_SPAN_DAYS
DEMO_DRIFT_DAILY_TOTAL = 48000

# Anomaly-detector settings mirrored from the ProjectAnomalySettings row seeded by
# the monitoring builder (Wave-1 defaults). Running the real detector with these
# guarantees every seeded MetricAnomaly is exactly what the worker would produce
# over the visible EventMetric series.
DEMO_ANOMALY_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    min_expected_count=10,
)

# The volume shape the seeder, the runtime tick and the synthetic warehouse share.
derive_seed = synthetic_traffic.derive_seed
hourly_volume = synthetic_traffic.hourly_volume


def hour_buckets(now: datetime, days: int) -> list[datetime]:
    """Return UTC hour-aligned buckets covering the last ``days`` days."""
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    result: list[datetime] = []
    cursor = start
    while cursor < end:
        result.append(cursor)
        cursor += timedelta(hours=1)
    return result


def platform_shares(progress: float) -> dict[str, float]:
    """Platform mix at ``progress`` (0 = drift start, 1 = now). Web share rises
    while iOS falls, so the distribution genuinely drifts over the window."""
    return synthetic_traffic.drift_platform_shares(progress)


def shares_to_counts(shares: dict[str, float], total: int) -> dict[str, int]:
    return {value: max(0, round(share * total)) for value, share in shares.items()}


def drift_span_progress(days_before_now: float) -> float:
    """Fraction into the drift ramp for a bucket ``days_before_now`` old. The mix
    only starts drifting ``DEMO_DRIFT_SPAN_DAYS`` before now."""
    return synthetic_traffic.drift_progress(days_before_now)
