"""Shared determinism and clock helpers for the demo builders.

Recipe 5 seeds no traffic of its own: every observed number comes from tripl's
pipeline reading the demo warehouse (:mod:`tripl.core.demo_warehouse`). What is
left here is what the AUTHORED half of the recipe still needs — a stable seed
derivation for the few authored timestamps (first-seen stagger, back-dated
history) and the length of the history the provisioning replay covers.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from tripl.config import settings
from tripl.core.analyzers.anomaly_detector import AnomalyDetectionSettings

#: How much warehouse history a new demo replays by default: three full weekly
#: cycles (the hour-of-week baseline needs 21 days) plus two days to score.
#: ``settings.demo_history_days`` (``DEMO_HISTORY_DAYS``) is what is read.
DEMO_HISTORY_DAYS = 23

#: The detector settings a demo project carries (``ProjectAnomalySettings``).
#: Quieter thresholds than a real project's defaults: the demo warehouse's
#: hourly volumes are modest, and its incidents are sized for these values
#: (``core.demo_warehouse.calendar``). Settling 0: the writer completes every
#: hour before a collection reads it.
DEMO_ANOMALY_SETTINGS = AnomalyDetectionSettings(
    baseline_window_buckets=14,
    min_history_buckets=7,
    sigma_threshold=3.0,
    min_expected_count=10,
)
DEMO_INGESTION_SETTLING_MINUTES = 0


def derive_seed(seed: int, key: str) -> int:
    """A stable 32-bit seed from the scenario seed and a semantic key.

    SHA-256 rather than ``hash`` so the value does not depend on PYTHONHASHSEED;
    ``key`` is a semantic label (an event name), never a random id.
    """
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def history_days() -> int:
    """Days of history a demo provisioned now replays (``DEMO_HISTORY_DAYS``)."""
    return settings.demo_history_days


def history_start(now: datetime) -> datetime:
    """The first hour the provisioning replay covers."""
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(days=history_days())
