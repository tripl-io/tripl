"""Record source freshness facts on a scan config after collection (#269).

``collect_metrics`` calls ``record_collection_freshness`` once the warehouse
read has finished and before anomaly detection, so the detector's hold and the
alerting layer's "data is late" candidate both see this run's facts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.bucketing import to_utc
from tripl.models.event_metric import EventMetric
from tripl.models.scan_config import ScanConfig
from tripl.schemas.scan_config import SourceFreshness
from tripl.services.source_freshness import (
    advance_last_event_at,
    compute_freshness,
    load_settling_delay,
)


def newest_event_bucket(
    session: Session,
    config: ScanConfig,
    *,
    window_from: datetime,
    window_to: datetime,
) -> datetime | None:
    """Start of the newest non-empty bucket this run stored, ``None`` if none.

    Bounded to the run's window (``ix_event_metric_config_bucket`` serves it):
    ``last_event_at`` only moves forward, so nothing older can change it.
    """
    newest = session.execute(
        select(sa_func.max(EventMetric.bucket)).where(
            EventMetric.scan_config_id == config.id,
            EventMetric.bucket >= window_from,
            EventMetric.bucket < window_to,
            EventMetric.count > 0,
        )
    ).scalar()
    return to_utc(newest) if newest is not None else None


def _may_advance_watermark(
    current: datetime | None, *, window_to: datetime, is_replay: bool
) -> bool:
    """Whether this run may move ``last_event_at`` (see ``record_collection_freshness``)."""
    if not is_replay:
        return True
    if current is None:
        return False
    return to_utc(window_to) > to_utc(current)


def record_collection_freshness(
    session: Session,
    config: ScanConfig,
    *,
    window_from: datetime,
    window_to: datetime,
    collected_at: datetime,
    is_replay: bool,
) -> SourceFreshness:
    """Stamp ``last_event_at`` / ``last_collection_at`` and return freshness now.

    ``last_event_at`` advances on a live run that stored newer data, and on a
    replay only when the replay window reaches PAST the current watermark: a
    replay that backfills the delayed window up to the present is exactly how a
    late source recovers, but a backfill of old data says nothing about the
    live feed and must not be able to mask one that is still late. A replay on a
    config with no watermark yet leaves it unset for the same reason — the first
    live collection establishes it. ``last_collection_at`` moves only on a live
    collection: a replay says nothing about whether the schedule is running.
    Not committed here; it rides the run's next commit, so a run that fails
    before then records nothing.
    """
    if _may_advance_watermark(config.last_event_at, window_to=window_to, is_replay=is_replay):
        observed = newest_event_bucket(
            session, config, window_from=window_from, window_to=window_to
        )
        config.last_event_at = advance_last_event_at(config.last_event_at, observed)
    if not is_replay:
        config.last_collection_at = to_utc(collected_at)
    return compute_freshness(
        config,
        collected_at,
        settling=load_settling_delay(session, config.project_id),
    )
