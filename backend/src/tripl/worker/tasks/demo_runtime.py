"""Demo runtime tick — keep an ACTIVE demo fresh after creation (tripl-2su6.7).

A generated demo is a snapshot: the warehouse builder seeds ~23 days of hourly
``EventMetric`` history whose newest bucket sits ~1h before creation time. Without
maintenance that snapshot ages out of the freshness horizon within hours and the
demo starts to look dead. This scheduled task advances each active demo's
synthetic clock by APPENDING new buckets/jobs/signals — it never rewrites or
mutates authored plan content.

Cadence decision — REAL cadence (catch-up)
------------------------------------------
The tick appends the hourly buckets between the demo's last populated bucket and
``now`` (the newest COMPLETE hour, ``floor(now) - 1h``), catching up if the demo
was idle. Catch-up is bounded by the retention window (``DEMO_RETENTION_DAYS``),
so a demo idle for weeks costs one bounded tick, not an unbounded backfill. We use
real cadence (not a compressed clock) so the appended series is indistinguishable
in shape from what a live warehouse scan would have produced.

Determinism
-----------
Every appended value is derived from the SAME deterministic helpers the warehouse
builder uses (:mod:`tripl.services.demo.noise`): per-event noise from
``derive_seed(DEMO_SEED, event_name)`` and volume from ``hourly_volume``. The
bucket index grid is anchored to ``demo_seeded_at`` (immutable), so an appended
bucket continues the seeded series' slow upward drift exactly. No spike is
injected on appended buckets — the seeded spike stays in history until pruned.

Idempotency & concurrency
-------------------------
Correctness comes from DB unique constraints + insert-if-absent, NOT from the
broker or the advisory lock. Each bucket's rows are written inside a SAVEPOINT and
an ``IntegrityError`` (a concurrent tick already wrote that bucket) is swallowed,
so a re-run, a partial-failure retry, or two workers at the same clock all
converge to ONE logical result. On PostgreSQL a per-project
``pg_advisory_xact_lock`` additionally serialises workers (a production
optimisation); it is a no-op off PostgreSQL (SQLite tests), where the unique
constraints carry all the weight.

Independence
------------
The tick only touches ``is_demo`` projects, so REAL scan/metric scheduling is
entirely unaffected by it. It shares exactly ONE thing with ``check_metrics_due``:
the idle-pause rule in :mod:`tripl.worker.tasks._demo_pause`, which both must apply
or a demo this tick has stopped advancing gets collected with a window that
destroys its history (tripl-0zpq.72, argued in that module). Anomaly re-detection
reuses the REAL ``detect_anomalies`` over the fresh window; the appended series is
kept fully coherent so a later real collection stays consistent.

Tests monkey-patch ``_get_sync_session`` on this module's globals (the shared
sync-session pattern) and pass an explicit ``now`` (fake clock).
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy import func as sa_func
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from tripl import cache, realtime
from tripl.config import settings
from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
    SeriesPoint,
    detect_anomalies,
)
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.schema_drift import SchemaDrift
from tripl.services.demo import noise
from tripl.services.demo.builders.plan import event_specs
from tripl.services.demo.builders.warehouse import (
    SPIKE_ANNOTATION_LABEL,
    SPIKE_EVENT_NAME,
)
from tripl.services.demo.scenario import DEMO_SEED
from tripl.services.source_freshness import (
    advance_last_event_at,
    compute_config_freshness,
    is_holding,
)
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session
from tripl.worker.tasks._demo_pause import is_demo_paused
from tripl.worker.tasks.metrics.attribution import recompute_anomaly_attributions

logger = logging.getLogger(__name__)

# Stable log event name so a per-demo tick failure is greppable/alertable in
# aggregated logs even though the sweep swallows it to keep advancing other demos.
DEMO_TICK_FAILED_EVENT = "demo.runtime.tick_failed"

# The idle-pause rule itself lives in :mod:`tripl.worker.tasks._demo_pause`
# (``DEMO_IDLE_PAUSE_MINUTES`` / ``is_demo_paused``) because the metrics dispatcher
# has to apply the SAME rule: a demo this tick has stopped advancing must not have
# its scheduled collection dispatched either, or that collection's window reaches
# back past the synthetic warehouse's full-volume hours and overwrites real history
# with sampled near-zero counts (tripl-0zpq.72).

# A per-demo advance runs in ONE transaction that can lose a Postgres deadlock race
# against a concurrent metric-collection run over the same ``metric_anomalies`` rows.
# A deadlock poisons that transaction, so the only recovery is to roll back and
# retry the whole advance — bounded, so one hot demo can't stall the sweep.
DEMO_ADVANCE_MAX_RETRIES = 3
# Rolling retention window for a demo's time-series/signal history. Matches the
# seeded history length (``DEMO_HISTORY_DAYS``) so the window stays constant-size:
# every tick prunes anything older, capping per-demo DB growth. Kept >= the
# detector's seasonal need (3 weekly cycles + 48h eval) so detection keeps working.
DEMO_RETENTION_DAYS = noise.DEMO_HISTORY_DAYS
# Bucket index grid width used by ``hourly_volume`` — the seeded series' total.
_TOTAL_BUCKETS = noise.DEMO_HISTORY_DAYS * 24
_HOUR = timedelta(hours=1)
# Coverage match rate mirrored from the governance builder so appended coverage
# reconciles with the seeded rows.
_COVERAGE_MATCH_RATE = 0.94
# Catalog metric advanced by the tick (event_composition, hourly, scan-scoped).
_CONVERSION_METRIC_NAME = "purchase_conversion"
# Direction withheld while the demo source is late (#269), as in ``metrics.detect``.
_HELD_DIRECTION = "drop"


def _aware(dt: datetime) -> datetime:
    """Normalise a datetime to tz-aware UTC.

    Postgres returns tz-aware datetimes; SQLite (tests) drops tzinfo. Re-attaching
    UTC on read keeps all internal arithmetic on a single, comparable timeline.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


@celery_app.task(name="tripl.worker.tasks.demo_runtime.advance_demos")  # type: ignore[untyped-decorator]
def advance_demos(now: datetime | None = None) -> dict[str, object]:
    """Advance every active demo's synthetic clock by one real-cadence tick.

    Selector: ``is_demo AND generation_status='ready' AND NOT paused``. A no-op
    (REAL scheduling untouched) when ``demo_runtime_enabled`` is false. ``now`` is
    injectable for tests; production uses wall-clock UTC.
    """
    if not settings.demo_runtime_enabled:
        logger.info("advance_demos: demo runtime disabled; skipping tick")
        return {"enabled": False, "advanced": 0, "skipped": 0}

    tick_now = _aware(now) if now is not None else datetime.now(UTC)
    session = _get_sync_session()
    advanced = 0
    skipped = 0
    try:
        candidates = session.execute(
            select(
                Project.id,
                Project.slug,
                Project.demo_seeded_at,
                Project.demo_last_accessed_at,
            ).where(
                Project.is_demo.is_(True),
                Project.generation_status == ProjectGenerationStatus.ready.value,
            )
        ).all()
        # End the read transaction before per-demo write transactions so each demo
        # gets its own advisory-locked transaction.
        session.rollback()

        for project_id, slug, seeded_at, last_accessed in candidates:
            if is_demo_paused(seeded_at, last_accessed, tick_now):
                skipped += 1
                continue
            # Deadlock-resilient advance. A demo-project metric collection racing
            # this tick over the same ``metric_anomalies`` rows can deadlock; the
            # loser's transaction is poisoned, so we roll back and retry the whole
            # advance a bounded number of times, then skip THIS demo — never abort
            # the sweep. Deeper mitigation (out of scope for this P0): have demo
            # metric collection take the same per-project ``pg_advisory_xact_lock``
            # as ``_acquire_project_xact_lock`` so the two paths serialise instead
            # of racing the rows.
            for attempt in range(1, DEMO_ADVANCE_MAX_RETRIES + 1):
                try:
                    _advance_demo(session, project_id, slug, tick_now)
                    advanced += 1
                    break
                except OperationalError:
                    session.rollback()
                    logger.warning(
                        "advance_demos: deadlock advancing demo slug=%s (attempt %d/%d)",
                        slug,
                        attempt,
                        DEMO_ADVANCE_MAX_RETRIES,
                    )
                    if attempt == DEMO_ADVANCE_MAX_RETRIES:
                        logger.error(
                            "advance_demos: giving up on demo slug=%s after %d deadlock retries",
                            slug,
                            DEMO_ADVANCE_MAX_RETRIES,
                        )
                        skipped += 1
                except Exception:
                    # Non-retryable failure (incl. a non-deadlock DBAPIError re-raised
                    # by _recompute_anomalies): one demo's failure must not abort the
                    # whole sweep. Roll back, count it as skipped for this tick (it did
                    # not advance), and move on. Stable event name so it's observable.
                    logger.exception("%s slug=%s", DEMO_TICK_FAILED_EVENT, slug)
                    session.rollback()
                    skipped += 1
                    break

        logger.info(
            "advance_demos: %d demos advanced, %d skipped (paused, deadlocked, or failed)",
            advanced,
            skipped,
        )
        return {"enabled": True, "advanced": advanced, "skipped": skipped}
    finally:
        session.close()


def _advance_demo(session: Session, project_id: uuid.UUID, slug: str, now: datetime) -> None:
    """Advance one demo inside a per-project advisory-locked transaction."""
    _acquire_project_xact_lock(session, project_id)

    project = session.get(Project, project_id)
    if project is None or project.demo_seeded_at is None:
        session.rollback()
        return

    scan_config = session.execute(
        select(ScanConfig).where(ScanConfig.project_id == project_id).limit(1)
    ).scalar_one_or_none()
    if scan_config is None:
        # No warehouse surface to advance; still stamp so we don't reselect hot.
        project.demo_last_tick_at = now
        session.commit()
        return

    scan_config_id = scan_config.id
    seeded_at = _aware(project.demo_seeded_at)
    grid_start = _floor_hour(seeded_at) - timedelta(days=noise.DEMO_HISTORY_DAYS)
    roster = _load_series_roster(session, scan_config_id, now)

    new_buckets = _pending_buckets(session, scan_config_id, now)
    written = 0
    if roster and new_buckets:
        written = _append_buckets(session, scan_config_id, roster, new_buckets, grid_start, now)

    # Source freshness facts (#269), stamped the way a live collection stamps
    # them: the tick IS the demo's collection. The newest appended bucket moves
    # ``last_event_at`` forward (never back), and every tick that reaches the
    # scan counts as a completed collection even when nothing was due — without
    # it the seeded values age out and a live demo reads late/overdue.
    if written and new_buckets:
        scan_config.last_event_at = advance_last_event_at(
            scan_config.last_event_at, new_buckets[-1]
        )
    scan_config.last_collection_at = now

    if written:
        _record_scan_job(session, scan_config_id, now, written, roster)
        # The same hold ``collect_metrics`` applies: judged on the facts just
        # stamped, so a tick that caught the demo up is fresh and holds nothing.
        # It only bites if the demo's series stops short of the clock (a
        # simulated delay), and then the tick must not write the drops a live
        # scan would have held.
        freshness = compute_config_freshness(session, scan_config, project_id, now)
        _recompute_anomalies(
            session, project_id, scan_config_id, now, hold_drops=is_holding(freshness)
        )

    _prune_retention(session, project_id, scan_config_id, now)

    project.demo_last_tick_at = now
    session.commit()

    _emit_status(slug)


def _acquire_project_xact_lock(session: Session, project_id: uuid.UUID) -> None:
    """Take a per-project transaction advisory lock so two workers can't double-write.

    No-op off PostgreSQL (SQLite tests have no advisory locks) — there,
    idempotency rests entirely on the DB unique constraints + insert-if-absent.
    The lock auto-releases when the transaction commits/rolls back.
    """
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return
    key = int.from_bytes(project_id.bytes[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _load_series_roster(
    session: Session, scan_config_id: uuid.UUID, now: datetime
) -> list[tuple[uuid.UUID, uuid.UUID, int, str]]:
    """The per-event series to advance: ``(event_id, event_type_id, base, name)``.

    Derived from the EXISTING seeded per-event ``EventMetric`` rows joined back to
    their events, so the tick advances exactly the series that were seeded (correct
    ids, no branch-copy duplicates) and reuses each event's authored ``base``
    volume from :func:`event_specs`.
    """
    specs_by_name = {spec.name: spec for spec in event_specs(now)}
    rows = session.execute(
        select(Event.id, Event.name, Event.event_type_id)
        .join(EventMetric, EventMetric.event_id == Event.id)
        .where(EventMetric.scan_config_id == scan_config_id)
        .distinct()
    ).all()
    roster: list[tuple[uuid.UUID, uuid.UUID, int, str]] = []
    for event_id, name, event_type_id in rows:
        spec = specs_by_name.get(name)
        if spec is not None:
            roster.append((event_id, event_type_id, spec.base, name))
    return roster


def _pending_buckets(session: Session, scan_config_id: uuid.UUID, now: datetime) -> list[datetime]:
    """Hourly buckets to append: ``(last_populated, latest_complete]``, retention-bounded."""
    last_bucket = session.execute(
        select(sa_func.max(EventMetric.bucket)).where(EventMetric.scan_config_id == scan_config_id)
    ).scalar()
    if last_bucket is None:
        return []
    last_bucket = _aware(last_bucket)

    # Newest COMPLETE hour, mirroring the seeder (newest bucket ~1h before now).
    latest_complete = _floor_hour(now) - _HOUR
    # Bound catch-up to the retention window — anything older would be pruned anyway.
    retention_start = _floor_hour(now) - timedelta(days=DEMO_RETENTION_DAYS)
    cursor = max(last_bucket + _HOUR, retention_start)

    buckets: list[datetime] = []
    while cursor <= latest_complete:
        buckets.append(cursor)
        cursor += _HOUR
    return buckets


def _append_buckets(
    session: Session,
    scan_config_id: uuid.UUID,
    roster: list[tuple[uuid.UUID, uuid.UUID, int, str]],
    new_buckets: list[datetime],
    grid_start: datetime,
    now: datetime,
) -> int:
    """Insert-if-absent every new bucket's rows; return how many buckets were written.

    Each bucket is written inside a SAVEPOINT so a unique-constraint clash from a
    concurrent tick (or a re-run) rolls back just that bucket and is skipped,
    leaving one logical result.
    """
    # Cheap pre-check: skip buckets already fully written (common on a same-clock
    # re-run) before paying for a savepoint.
    existing = {
        _aware(b)
        for b in session.execute(
            select(EventMetric.bucket)
            .where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.event_type_id.isnot(None),
                EventMetric.bucket >= new_buckets[0],
            )
            .distinct()
        ).scalars()
    }
    conversion_metric_id = session.execute(
        select(MetricDefinition.id)
        .join(ScanConfig, ScanConfig.project_id == MetricDefinition.project_id)
        .where(
            ScanConfig.id == scan_config_id,
            MetricDefinition.name == _CONVERSION_METRIC_NAME,
        )
        .limit(1)
    ).scalar_one_or_none()
    spike_event_id = next(
        (eid for eid, _etid, _base, name in roster if name == SPIKE_EVENT_NAME), None
    )

    written = 0
    for bucket in new_buckets:
        if bucket in existing:
            continue
        try:
            with session.begin_nested():
                _build_bucket_rows(
                    session,
                    scan_config_id,
                    roster,
                    bucket,
                    grid_start,
                    now,
                    spike_event_id=spike_event_id,
                    conversion_metric_id=conversion_metric_id,
                )
                session.flush()
            written += 1
        except IntegrityError:
            # A concurrent tick already wrote this bucket — idempotent skip.
            logger.debug("advance_demos: bucket %s already present, skipping", bucket)
    return written


def _build_bucket_rows(
    session: Session,
    scan_config_id: uuid.UUID,
    roster: list[tuple[uuid.UUID, uuid.UUID, int, str]],
    bucket: datetime,
    grid_start: datetime,
    now: datetime,
    *,
    spike_event_id: uuid.UUID | None,
    conversion_metric_id: uuid.UUID | None,
) -> None:
    """Add one bucket's rows: per-event + per-type EventMetric, breakdown, coverage."""
    idx = round((bucket - grid_start).total_seconds() / 3600.0)
    per_type_total: dict[uuid.UUID, int] = {}
    home_count = 0

    for event_id, event_type_id, base, name in roster:
        noise_seed = noise.derive_seed(DEMO_SEED, name) % 997
        count = noise.hourly_volume(base, bucket, idx, noise_seed, _TOTAL_BUCKETS)
        session.add(
            EventMetric(
                scan_config_id=scan_config_id,
                event_id=event_id,
                event_type_id=None,
                bucket=bucket,
                count=count,
            )
        )
        per_type_total[event_type_id] = per_type_total.get(event_type_id, 0) + count
        if event_id == spike_event_id:
            home_count = count

    for event_type_id, total in per_type_total.items():
        session.add(
            EventMetric(
                scan_config_id=scan_config_id,
                event_id=None,
                event_type_id=event_type_id,
                bucket=bucket,
                count=total,
            )
        )

    if spike_event_id is not None and home_count:
        _build_breakdown_rows(session, scan_config_id, spike_event_id, bucket, home_count, now)

    matched = sum(per_type_total.values())
    if matched:
        session.add(
            CoverageMetric(
                scan_config_id=scan_config_id,
                bucket=bucket,
                total_count=max(matched, round(matched / _COVERAGE_MATCH_RATE)),
                matched_count=matched,
            )
        )

    if conversion_metric_id is not None:
        # event_composition ratio: a small live-looking fraction continuing the
        # seeded upward drift plateau (~0.08–0.11) with a daily ripple.
        value = 0.095 + 0.015 * math.sin(bucket.hour * math.pi / 12)
        session.add(
            MetricValue(
                metric_definition_id=conversion_metric_id,
                scan_config_id=scan_config_id,
                bucket=bucket,
                value=value,
            )
        )


def _build_breakdown_rows(
    session: Session,
    scan_config_id: uuid.UUID,
    spike_event_id: uuid.UUID,
    bucket: datetime,
    total_count: int,
    now: datetime,
) -> None:
    """Platform split for the spike event, matching the warehouse builder's drift."""
    days_before = (now - bucket).total_seconds() / 86400.0
    shares = noise.platform_shares(noise.drift_span_progress(days_before))
    for platform, count in noise.shares_to_counts(shares, total_count).items():
        session.add(
            EventMetricBreakdown(
                scan_config_id=scan_config_id,
                event_id=spike_event_id,
                bucket=bucket,
                breakdown_column="platform",
                breakdown_value=platform,
                is_other=False,
                count=max(1, count),
            )
        )


def _record_scan_job(
    session: Session,
    scan_config_id: uuid.UUID,
    now: datetime,
    buckets_written: int,
    roster: list[tuple[uuid.UUID, uuid.UUID, int, str]],
) -> None:
    """Record a completed ScanJob reflecting THIS tick's real execution.

    Not a timestamp rewrite of an old job — a genuinely new run row so the demo's
    scan history keeps growing (and reads healthy: the latest job is a success).
    """
    started = now - timedelta(seconds=8)
    event_types = len({etid for _eid, etid, _base, _name in roster})
    session.add(
        ScanJob(
            scan_config_id=scan_config_id,
            status=ScanJobStatus.completed.value,
            started_at=started,
            completed_at=now,
            created_at=now,
            result_summary={
                "events_created": 0,
                "events_skipped": 0,
                "events_grouped": event_types,
                "events_merged": len(roster),
                "variables_created": 0,
                "columns_analyzed": 10,
                "scan_rows_processed": buckets_written * sum(b for _e, _t, b, _n in roster),
                "buckets_appended": buckets_written,
                "demo_runtime_tick": True,
            },
        )
    )


def _recompute_anomalies(
    session: Session,
    project_id: uuid.UUID,
    scan_config_id: uuid.UUID,
    now: datetime,
    *,
    hold_drops: bool = False,
) -> None:
    """Re-run the REAL detector over the fresh window and upsert MetricAnomaly.

    Best-effort: a detection failure must not fail the tick (the appended series
    stays coherent for a later real collection). Idempotent per scope: existing
    rows in the evaluation window are cleared and re-inserted, so a re-run at the
    same clock yields the same rows. ``hold_drops`` (the demo source is late,
    #269) withholds NEW drop-direction anomalies and spares stored drop rows,
    as ``metrics.detect`` does for a live scan. The window's attributions
    (#255) are recomputed after the upserts, inside the same best-effort guard.
    """
    try:
        anomaly_settings = session.execute(
            select(ProjectAnomalySettings).where(ProjectAnomalySettings.project_id == project_id)
        ).scalar_one_or_none()
        if anomaly_settings is None or not anomaly_settings.anomaly_detection_enabled:
            return

        eval_start = _floor_hour(now) - timedelta(hours=noise.DEMO_EVAL_WINDOW_HOURS)
        eval_end = _floor_hour(now)
        window_start = eval_start - timedelta(days=DEMO_RETENTION_DAYS)

        rows = session.execute(
            select(
                EventMetric.event_id,
                EventMetric.event_type_id,
                EventMetric.bucket,
                EventMetric.count,
            ).where(
                EventMetric.scan_config_id == scan_config_id,
                EventMetric.bucket >= window_start,
            )
        ).all()

        event_series: dict[uuid.UUID, dict[datetime, int]] = {}
        type_series: dict[uuid.UUID, dict[datetime, int]] = {}
        project_total: dict[datetime, int] = {}
        for event_id, event_type_id, bucket, count in rows:
            bkt = _aware(bucket)
            if event_id is not None:
                event_series.setdefault(event_id, {})[bkt] = count
            elif event_type_id is not None:
                type_series.setdefault(event_type_id, {})[bkt] = count
                project_total[bkt] = project_total.get(bkt, 0) + count

        for event_id, series in event_series.items():
            _upsert_scope_anomalies(
                session,
                scan_config_id,
                series,
                scope_type=SCOPE_EVENT,
                scope_ref=str(event_id),
                event_id=event_id,
                event_type_id=None,
                eval_start=eval_start,
                eval_end=eval_end,
                hold_drops=hold_drops,
            )
        for event_type_id, series in type_series.items():
            _upsert_scope_anomalies(
                session,
                scan_config_id,
                series,
                scope_type=SCOPE_EVENT_TYPE,
                scope_ref=str(event_type_id),
                event_id=None,
                event_type_id=event_type_id,
                eval_start=eval_start,
                eval_end=eval_end,
                hold_drops=hold_drops,
            )
        _upsert_scope_anomalies(
            session,
            scan_config_id,
            project_total,
            scope_type=SCOPE_PROJECT_TOTAL,
            scope_ref=str(scan_config_id),
            event_id=None,
            event_type_id=None,
            eval_start=eval_start,
            eval_end=eval_end,
            hold_drops=hold_drops,
        )
        # "Why did it change?" (#255): the scope upserts above replaced the
        # anomaly rows (their attributions went with them), so re-attribute the
        # window with the worker's own pass — the demo's Why panel and alerts
        # then read the same split a real collection stores. The window reaches
        # one bucket past ``eval_end`` so the newest flagged bucket is covered.
        config = session.get(ScanConfig, scan_config_id)
        if config is not None:
            recompute_anomaly_attributions(
                session,
                config,
                evaluation_start=eval_start,
                evaluation_end=eval_end + _HOUR,
                now=now,
            )
    except OperationalError, DBAPIError:
        # A DBAPI-level failure (deadlock, lost connection) inside ``session.execute``
        # has POISONED the transaction: it can no longer be committed, and swallowing
        # it here would let ``_advance_demo`` fall through to ``_prune_retention``
        # whose next execute raises ``InFailedSqlTransaction`` and rolls back the whole
        # tick. Re-raise so ``advance_demos`` can abandon + retry this demo's
        # transaction. (``OperationalError`` is the deadlock case; it subclasses
        # ``DBAPIError``, which also covers disconnects — either way the txn is doomed.)
        raise
    except Exception:
        # A genuine detector/logic bug raises BEFORE any ``session.execute`` and leaves
        # the session clean, so we log and swallow it: a detector bug must not be able
        # to kill the whole tick.
        logger.exception("advance_demos: anomaly recompute failed for %s", scan_config_id)


def _upsert_scope_anomalies(
    session: Session,
    scan_config_id: uuid.UUID,
    series: dict[datetime, int],
    *,
    scope_type: str,
    scope_ref: str,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
    eval_start: datetime,
    eval_end: datetime,
    hold_drops: bool = False,
) -> None:
    """Delete-window-then-insert the detector's anomalies for one scope.

    Under ``hold_drops`` drop-direction anomalies are neither written nor
    cleared: a stored drop predates the delay and stands (see
    ``metrics.detect._apply_drop_hold``).
    """
    points = [SeriesPoint(bucket=bucket, count=count) for bucket, count in sorted(series.items())]
    detected = detect_anomalies(
        points,
        interval=_HOUR,
        evaluation_start=eval_start,
        evaluation_end=eval_end,
        settings=noise.DEMO_ANOMALY_SETTINGS,
    ).anomalies
    delete_filters = [
        MetricAnomaly.scan_config_id == scan_config_id,
        MetricAnomaly.scope_type == scope_type,
        MetricAnomaly.scope_ref == scope_ref,
        MetricAnomaly.bucket >= eval_start,
    ]
    if hold_drops:
        detected = [anomaly for anomaly in detected if anomaly.direction != _HELD_DIRECTION]
        delete_filters.append(MetricAnomaly.direction != _HELD_DIRECTION)
    session.execute(delete(MetricAnomaly).where(*delete_filters))
    for anomaly in detected:
        session.add(
            MetricAnomaly(
                scan_config_id=scan_config_id,
                scope_type=scope_type,
                scope_ref=scope_ref,
                event_id=event_id,
                event_type_id=event_type_id,
                bucket=anomaly.bucket,
                actual_count=anomaly.actual_count,
                expected_count=anomaly.expected_count,
                stddev=anomaly.stddev,
                z_score=anomaly.z_score,
                effective_stddev=anomaly.effective_stddev,
                detector_kind=anomaly.kind,
                direction=anomaly.direction,
            )
        )


def _prune_retention(
    session: Session, project_id: uuid.UUID, scan_config_id: uuid.UUID, now: datetime
) -> None:
    """Prune per-demo history beyond the rolling retention window (caps DB growth)."""
    cutoff = _floor_hour(now) - timedelta(days=DEMO_RETENTION_DAYS)

    session.execute(
        delete(EventMetric).where(
            EventMetric.scan_config_id == scan_config_id, EventMetric.bucket < cutoff
        )
    )
    session.execute(
        delete(EventMetricBreakdown).where(
            EventMetricBreakdown.scan_config_id == scan_config_id,
            EventMetricBreakdown.bucket < cutoff,
        )
    )
    session.execute(
        delete(CoverageMetric).where(
            CoverageMetric.scan_config_id == scan_config_id, CoverageMetric.bucket < cutoff
        )
    )
    session.execute(
        delete(MetricAnomaly).where(
            MetricAnomaly.scan_config_id == scan_config_id, MetricAnomaly.bucket < cutoff
        )
    )
    session.execute(
        delete(DistributionDrift).where(
            DistributionDrift.scan_config_id == scan_config_id,
            DistributionDrift.bucket < cutoff,
        )
    )
    session.execute(
        delete(SchemaDrift).where(
            SchemaDrift.scan_config_id == scan_config_id, SchemaDrift.detected_at < cutoff
        )
    )
    session.execute(
        delete(ScanJob).where(ScanJob.scan_config_id == scan_config_id, ScanJob.created_at < cutoff)
    )
    # The seeded "Injected demo spike" marker retires with the anomaly it
    # explains: pruned at the same cutoff, so an aging demo never keeps a label
    # pinned over a stretch of plain noise with nothing under it. The runtime
    # appends no new spike, so re-dating the marker would label nothing either
    # (tripl-0zpq.322). Matched on the seeder's label, so an annotation the user
    # wrote in the demo is left alone.
    session.execute(
        delete(ChartAnnotation).where(
            ChartAnnotation.project_id == project_id,
            ChartAnnotation.label == SPIKE_ANNOTATION_LABEL,
            ChartAnnotation.bucket < cutoff,
        )
    )
    # Catalog metric values for THIS project (both scan-scoped and NULL-scoped).
    metric_ids = (
        session.execute(
            select(MetricDefinition.id).where(MetricDefinition.project_id == project_id)
        )
        .scalars()
        .all()
    )
    if metric_ids:
        session.execute(
            delete(MetricValue).where(
                MetricValue.metric_definition_id.in_(metric_ids),
                MetricValue.bucket < cutoff,
            )
        )


def _emit_status(slug: str) -> None:
    """Emit a project-scoped 'updated' signal for the tripl-2su6.8 live stream.

    Invalidates the project-scoped cache prefixes so the next read serves the
    freshly-appended series, then publishes the realtime events so subscribed
    clients refresh without waiting on the polling fallback. Redis-off (tests) is
    a no-op for both. Runs AFTER the tick's commit.
    """
    cache.sync_delete_prefix(cache.prefix_projects())
    cache.sync_delete_prefix(f"{cache.prefix_signals()}{slug}:")
    realtime.publish_project_event(
        slug, realtime.EVENT_METRIC_COLLECTION_UPDATED, {"source": "demo_runtime"}
    )
    realtime.publish_project_event(slug, realtime.EVENT_SIGNALS_UPDATED, {"source": "demo_runtime"})
