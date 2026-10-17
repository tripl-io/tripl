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
#: cycles (the hour-of-week baseline needs 21 days) plus two days to score,
#: then back to midnight (:func:`history_start`).
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


#: A history longer than this many days (any history the first build does not
#: replay whole) starts at midnight UTC.
WHOLE_DAYS_AFTER = 2


def history_start(now: datetime) -> datetime:
    """The first hour the provisioning replay covers.

    :func:`history_days` back from the seed hour, and then, for a history of
    more than :data:`WHOLE_DAYS_AFTER` days, back to that day's midnight UTC: a
    full history holds 23 to 24 days. The detector scores no bucket of a series'
    first ``min_history_buckets`` hours (seven), and for seven build hours of
    every week a history starting at the hour had a weekly promo send among
    them: that send got no anomaly and no *expected* verdict, and the three
    weekly verdicts the planned-window suggestion needs were two. From midnight, the first seven
    hours are always a night, and every send of the first three weeks is
    scored.

    Never earlier than the warehouse's retention reaches back, where the
    collection would clamp it anyway (``DEMO_HISTORY_DAYS`` equal to
    ``DEMO_WAREHOUSE_RETENTION_DAYS``).
    """
    hour = now.replace(minute=0, second=0, microsecond=0)
    days = history_days()
    start = hour - timedelta(days=days)
    if days <= WHOLE_DAYS_AFTER:
        return start
    horizon = hour - timedelta(days=settings.demo_warehouse_retention_days)
    return max(start.replace(hour=0), horizon)
