"""Deterministic shape helpers for the demo scenario.

Pure functions only — no DB, no I/O. Every value the demo seeds is derived from
``(clock, seed)`` through these helpers, so re-running the recipe with the same
clock and seed produces an identical data shape.

Determinism note: per-series noise is derived with :func:`derive_seed` (SHA-256)
rather than the Python builtin ``hash()``. ``hash()`` is salted per-process for
``str``/``bytes`` (PYTHONHASHSEED), so keying noise off ``hash(str)`` would make
the seeded metrics/anomalies/drift non-reproducible across runs.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta

from tripl.core.adapters.synthetic import SPIKE_MULTIPLIER
from tripl.core.analyzers.anomaly_detector import AnomalyDetectionSettings

# Wall-clock length of the seeded hourly history. The seasonal (hour-of-week)
# phase baseline in the anomaly detector needs 3 full weekly cycles = 504 hourly
# buckets BEFORE the evaluation window, or ``detect_anomalies`` silently degrades
# to the seasonality-blind rolling fallback. 23 days = 552 buckets gives 504 of
# history plus a 48h evaluation window, and the newest bucket sits one hour before
# ``now`` so the signal stays inside the Wave-1 freshness horizon
# (``LATEST_SCAN_STALE_INTERVALS`` = 3 intervals).
DEMO_HISTORY_DAYS = 23
DEMO_EVAL_WINDOW_HOURS = 48
# The synthetic source reproduces the spike at the same multiple, so it owns it.
DEMO_SPIKE_MULTIPLIER = SPIKE_MULTIPLIER

# Distribution-drift showcase: the platform mix drifts only over the final
# ``DEMO_DRIFT_SPAN_DAYS`` days so the real PSI climbs a stable -> minor ->
# significant ladder against the window-start baseline.
DEMO_DRIFT_SPAN_DAYS = 8
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


def derive_seed(seed: int, key: str) -> int:
    """Derive a stable 32-bit noise seed from the scenario seed and a semantic key.

    Uses SHA-256 (not builtin ``hash``) so the value is reproducible across
    processes regardless of PYTHONHASHSEED. ``key`` is a stable semantic label
    (e.g. an event name), never a random uuid, so the same event always maps to
    the same noise across reseeds.
    """
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


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


def sinusoidal_count(base: int, bucket: datetime, noise_seed: int) -> int:
    """Daily sinusoid with a small deterministic noise term."""
    hour_of_day = bucket.hour
    # peak around 14:00 UTC, trough around 02:00
    sinusoid = math.sin((hour_of_day - 2) * math.pi / 12)
    # cheap deterministic noise: seed+hour to ±10 %
    noise = ((noise_seed * 31 + bucket.day * 7 + hour_of_day * 13) % 21 - 10) / 100.0
    raw = base * (1 + 0.4 * sinusoid + noise)
    return max(1, round(raw))


def hourly_volume(
    base: int, bucket: datetime, idx: int, noise_seed: int, total_buckets: int
) -> int:
    """Deterministic hourly volume: daily + gentle weekly shape, a phase-consistent
    texture, and a slow upward drift.

    The texture depends only on ``(noise_seed, weekday, hour)`` — never on the week
    — so the same hour-of-week repeats identically across cycles. That keeps the
    detector's seasonal phase baseline tight (near-zero robust scale), so the
    injected spike is the only deviation that clears the sigma gate and the demo
    yields a small, reproducible set of anomalies instead of noise-driven false
    positives. The drift stays well under the detector's 15% trend-shift gate.
    """
    hour = bucket.hour
    weekday = bucket.weekday()
    daily = math.sin((hour - 2) * math.pi / 12)
    weekly = 0.08 * math.sin(weekday * math.pi / 3.5)
    texture = ((noise_seed * 31 + weekday * 7 + hour * 13) % 15 - 7) / 100.0
    drift = 0.04 * (idx / max(total_buckets - 1, 1))
    raw = base * (1 + 0.35 * daily + weekly + texture + drift)
    return max(1, round(raw))


def platform_shares(progress: float) -> dict[str, float]:
    """Platform mix at ``progress`` (0 = drift start, 1 = now). Web share rises
    while iOS falls, so the distribution genuinely drifts over the window."""
    clamped = min(max(progress, 0.0), 1.0)
    web = 0.12 + 0.24 * clamped
    ios = 0.55 - 0.18 * clamped
    android = max(0.01, 1.0 - web - ios)
    return {"ios": ios, "android": android, "web": web}


def shares_to_counts(shares: dict[str, float], total: int) -> dict[str, int]:
    return {value: max(0, round(share * total)) for value, share in shares.items()}


def drift_span_progress(days_before_now: float) -> float:
    """Fraction into the drift ramp for a bucket ``days_before_now`` old. The mix
    only starts drifting ``DEMO_DRIFT_SPAN_DAYS`` before now."""
    return min(max((DEMO_DRIFT_SPAN_DAYS - days_before_now) / DEMO_DRIFT_SPAN_DAYS, 0.0), 1.0)
