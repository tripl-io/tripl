"""The daily sunset watch (#258): lifecycle findings on deprecated events.

Two conditions, both on MAIN-branch events with ``status = deprecated`` (scans
only ever see main, so a branch copy has no volume of its own to judge):

* ``sunset_overdue`` — ``sunset_at`` has passed and the event still received
  volume in the last 24 hours (buckets after the sunset only);
* ``successor_silent`` — the event names a successor
  (``superseded_by_event_id``) and the successor received no volume in the
  last 7 days.

Findings live in ``lifecycle_findings``, one row per (event, kind), and this
task is their only writer: it upserts what holds today, resolves what no longer
does, and reopens a resolved row whose condition came back. The event payload,
the catalog chip, ``GET /projects/{slug}/lifecycle-findings`` and the Lifecycle
alert family all read those rows (``load_open_findings`` below is the alerting
seam), so every surface agrees with the last run.

"Window" follows ``services.lifecycle_rules``: a bucket counts when it OVERLAPS
the window, so a daily scan's midnight bucket does not fall out of "the last 24
hours" at 06:00 and flap the finding.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tripl.core.bucketing import to_utc
from tripl.models.event import Event, EventStatus
from tripl.models.event_metric import EventMetric
from tripl.models.lifecycle_finding import LifecycleFinding, LifecycleFindingKind
from tripl.models.plan_branch import BranchKind, PlanBranch
from tripl.models.scan_config import ScanConfig
from tripl.services.lifecycle_rules import (
    SUCCESSOR_SILENCE_WINDOW,
    SUNSET_VOLUME_WINDOW,
    lookback_start,
    window_volume,
)
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session

logger = logging.getLogger(__name__)

# Bind-parameter headroom for the IN lists below, as in ``metrics.collect``.
_MAX_IDS_PER_QUERY = 20000


@dataclass(frozen=True)
class _Desired:
    project_id: uuid.UUID
    related_event_id: uuid.UUID | None
    volume_24h: int | None
    successor_volume_7d: int | None


@dataclass
class LifecycleSweepStats:
    opened: int = 0
    updated: int = 0
    reopened: int = 0
    resolved: int = 0
    open_by_kind: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        out = {
            "opened": self.opened,
            "updated": self.updated,
            "reopened": self.reopened,
            "resolved": self.resolved,
        }
        for kind, count in self.open_by_kind.items():
            out[f"open_{kind}"] = count
        return out


def _chunks(ids: list[uuid.UUID]) -> list[list[uuid.UUID]]:
    return [ids[i : i + _MAX_IDS_PER_QUERY] for i in range(0, len(ids), _MAX_IDS_PER_QUERY)]


def _volume_rows(
    session: Session, event_ids: list[uuid.UUID], *, since: datetime
) -> list[tuple[uuid.UUID, datetime, int, str | None]]:
    """``(event_id, bucket, count, scan interval)`` for buckets starting at/after ``since``."""
    rows: list[tuple[uuid.UUID, datetime, int, str | None]] = []
    for chunk in _chunks(event_ids):
        result = session.execute(
            select(EventMetric.event_id, EventMetric.bucket, EventMetric.count, ScanConfig.interval)
            .join(ScanConfig, ScanConfig.id == EventMetric.scan_config_id)
            .where(
                EventMetric.event_id.in_(chunk),
                EventMetric.bucket >= since,
                EventMetric.count > 0,
            )
        ).all()
        rows.extend(
            (event_id, bucket, int(count), None if interval is None else str(interval))
            for event_id, bucket, count, interval in result
            if event_id is not None
        )
    return rows


def _desired_findings(session: Session, *, now: datetime) -> dict[tuple[uuid.UUID, str], _Desired]:
    """Every (event, kind) that holds right now, with the numbers behind it."""
    deprecated = session.execute(
        select(Event.id, Event.project_id, Event.sunset_at, Event.superseded_by_event_id)
        .join(
            PlanBranch,
            (PlanBranch.id == Event.branch_id) & (PlanBranch.project_id == Event.project_id),
        )
        .where(
            PlanBranch.kind == BranchKind.main.value,
            Event.status == EventStatus.deprecated.value,
        )
    ).all()
    desired: dict[tuple[uuid.UUID, str], _Desired] = {}
    if not deprecated:
        return desired

    past_sunset = {
        event_id: to_utc(sunset_at)
        for event_id, _project_id, sunset_at, _successor in deprecated
        if sunset_at is not None and to_utc(sunset_at) <= now
    }
    if past_sunset:
        volume_24h = window_volume(
            _volume_rows(
                session,
                list(past_sunset),
                since=lookback_start(now, SUNSET_VOLUME_WINDOW),
            ),
            now=now,
            window=SUNSET_VOLUME_WINDOW,
            since=past_sunset,
        )
        for event_id, project_id, _sunset_at, _successor in deprecated:
            volume = volume_24h.get(event_id, 0)
            if event_id in past_sunset and volume > 0:
                desired[(event_id, LifecycleFindingKind.sunset_overdue.value)] = _Desired(
                    project_id=project_id,
                    related_event_id=None,
                    volume_24h=volume,
                    successor_volume_7d=None,
                )

    successors = {
        event_id: successor
        for event_id, _project_id, _sunset_at, successor in deprecated
        if successor is not None
    }
    if successors:
        successor_volume = window_volume(
            _volume_rows(
                session,
                list(set(successors.values())),
                since=lookback_start(now, SUCCESSOR_SILENCE_WINDOW),
            ),
            now=now,
            window=SUCCESSOR_SILENCE_WINDOW,
        )
        for event_id, project_id, _sunset_at, successor in deprecated:
            if successor is None:
                continue
            volume = successor_volume.get(successor, 0)
            if volume == 0:
                desired[(event_id, LifecycleFindingKind.successor_silent.value)] = _Desired(
                    project_id=project_id,
                    related_event_id=successor,
                    volume_24h=None,
                    successor_volume_7d=0,
                )
    return desired


def compute_lifecycle_findings(session: Session, *, now: datetime) -> LifecycleSweepStats:
    """Reconcile ``lifecycle_findings`` with what holds at ``now``. Does not commit."""
    now = to_utc(now)
    desired = _desired_findings(session, now=now)
    stats = LifecycleSweepStats()

    existing = session.execute(select(LifecycleFinding)).scalars().all()
    seen_keys: set[tuple[uuid.UUID, str]] = set()
    for finding in existing:
        key = (finding.event_id, finding.kind)
        seen_keys.add(key)
        want = desired.get(key)
        if want is None:
            if finding.resolved_at is None:
                finding.resolved_at = now
                stats.resolved += 1
            continue
        if finding.resolved_at is not None:
            # The condition came back: a new episode, dated from today.
            finding.resolved_at = None
            finding.first_seen_at = now
            stats.reopened += 1
        else:
            stats.updated += 1
        finding.project_id = want.project_id
        finding.related_event_id = want.related_event_id
        finding.last_seen_at = now
        if want.volume_24h is not None:
            finding.volume_24h = want.volume_24h
        if want.successor_volume_7d is not None:
            finding.successor_volume_7d = want.successor_volume_7d

    for key, want in desired.items():
        if key in seen_keys:
            continue
        event_id, kind = key
        session.add(
            LifecycleFinding(
                id=uuid.uuid4(),
                project_id=want.project_id,
                event_id=event_id,
                kind=kind,
                related_event_id=want.related_event_id,
                first_seen_at=now,
                last_seen_at=now,
                resolved_at=None,
                volume_24h=want.volume_24h,
                successor_volume_7d=want.successor_volume_7d,
            )
        )
        stats.opened += 1

    for _event_id, kind in desired:
        stats.open_by_kind[kind] = stats.open_by_kind.get(kind, 0) + 1
    session.flush()
    return stats


def load_open_findings(
    session: Session, *, project_id: uuid.UUID | None = None
) -> list[LifecycleFinding]:
    """Open findings (``resolved_at IS NULL``), oldest first — the alerting seam.

    The Lifecycle alert family builds one ``lifecycle`` candidate per row here;
    ``(kind, event_id)`` is the natural dedup key for its cooldown state.
    """
    stmt = select(LifecycleFinding).where(LifecycleFinding.resolved_at.is_(None))
    if project_id is not None:
        stmt = stmt.where(LifecycleFinding.project_id == project_id)
    return list(
        session.execute(
            stmt.order_by(LifecycleFinding.first_seen_at.asc(), LifecycleFinding.id.asc())
        )
        .scalars()
        .all()
    )


@celery_app.task(name="tripl.worker.tasks.lifecycle.check_lifecycle_findings")  # type: ignore[untyped-decorator]
def check_lifecycle_findings() -> dict[str, int]:
    """Daily beat entry (``check-lifecycle-findings`` in celery_app.py)."""
    session = _get_sync_session()
    try:
        stats = compute_lifecycle_findings(session, now=datetime.now(UTC))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Lifecycle sweep failed")
        raise
    finally:
        session.close()
    result = stats.as_dict()
    logger.info("Lifecycle sweep: %s", result)
    return result


__all__ = [
    "LifecycleSweepStats",
    "check_lifecycle_findings",
    "compute_lifecycle_findings",
    "load_open_findings",
]
