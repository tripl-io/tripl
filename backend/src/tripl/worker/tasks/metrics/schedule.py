"""Scheduling task for metrics collection.

``check_metrics_due`` scans all configured scan configs and dispatches
``collect_metrics`` for every config with a newly completed interval bucket.
The Celery task name is kept identical via the explicit ``name=`` string.

Tests monkey-patch the session/helper globals of THIS module.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func as sa_func
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from tripl.config import settings
from tripl.core.adapters.synthetic import SYNTHETIC_ONGOING_HOURS
from tripl.core.bucketing import floor_to_bucket
from tripl.core.collection_progress import collection_progress_to
from tripl.core.intervals import get_interval
from tripl.models.domain_enums import MetricKind, MetricStatus
from tripl.models.event_metric import EventMetric
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.services.active_org_scope import in_active_org, project_in_active_org
from tripl.worker.celery_app import celery_app
from tripl.worker.tasks._demo_pause import is_demo_paused
from tripl.worker.tasks.metrics._helpers import (
    STALE_ACTIVE_SCAN_JOB_TIMEOUT,
    _fail_stale_active_scan_job,
    _floor_to_interval,
    _get_active_scan_jobs,
    _get_sync_session,
    _normalize_job_timestamp,
    _parse_task_datetime,
)
from tripl.worker.tasks.metrics.metric_collect import (
    COLLECTION_STATUS_ERROR,
    COLLECTION_STATUS_RUNNING,
    collect_fact_metrics_batch,
    collect_metric_definitions,
    event_composition_binding_error,
    mark_collection_error,
)
from tripl.worker.tasks.metrics.tasks import (
    _RECENT_JOB_SCAN_LIMIT,
    METRICS_COLLECTION_MODE,
    SCHEDULED_RESUME_OVERLAP_BUCKETS,
    _is_dispatcher_collection_job,
    _last_collected_window_to,
    collect_metrics,
)

logger = logging.getLogger(__name__)

# How rarely a DEMO project's scheduled collection may run (tripl-jfm3.73).
#
# A demo pays 67-141 s per collection against an in-memory dataset, and every
# demo on a deployment paid it every hour. Almost all of that is per-scope
# MSTL(24,168) — 21 volume scopes plus 81-131 breakdown scopes at ~1.2 s each —
# and none of the existing fast paths apply, because demo volumes sit far above
# ``min_expected_count`` and every demo series is unique so the decomposition
# cache added for tripl-jfm3.1 has nothing to reuse.
#
# The demo does NOT go dark between runs: ``advance_demos`` already appends
# hourly buckets and re-runs the real detector for volume anomalies every hour,
# so the series and the headline signals stay live. What this cooldown defers is
# the part only the scheduled collection produces — breakdown anomalies and
# distribution drift — from 24x a day to 4x, which for a demo is invisible.
#
# Deliberately NOT implemented by lengthening ``ScanConfig.interval``: that is
# the BUCKET size, and the demo's whole seasonality story (periods 24 and 168 in
# hourly buckets, 23 days of history) is built on it being 1h.
DEMO_COLLECTION_COOLDOWN_HOURS = 6


def _hours_since_last_scheduled_collection(
    session: Session, scan_config_id: uuid.UUID, *, now: datetime
) -> float | None:
    """Age of the newest job THIS dispatcher created for metrics collection.

    ``None`` when the recent history holds none. Manual catalog scans, metrics
    replays, event-group applies and the demo's hourly runtime tick all share
    ``scan_config_id`` with a scheduled collection and must not restart the
    cooldown — only the ``mode`` stamp, written at job creation, identifies one.
    Identified by that stamp rather than by excluding the tick: exclusion counted
    every foreign job, so exercising Run scan or Replay on a demo deferred the
    scheduled collection — the only producer of breakdown anomalies and
    distribution drift — for up to six hours.

    Status is deliberately not filtered: a dispatcher job that failed still
    consumed the slot this cooldown rations, and repeated failure is the failure
    backoff's job, not this one's.

    ``_RECENT_JOB_SCAN_LIMIT`` bounds ROWS, and the stricter filter skips more of
    them, so a demo with a long run of manual scans can bury its dispatcher job
    and read as "no recent collection". That errs toward collecting, which is the
    safe direction and the same one the CLI's ``scan_history_window_full``
    finding reports.
    """
    rows = session.execute(
        select(ScanJob.created_at, ScanJob.result_summary)
        .where(ScanJob.scan_config_id == scan_config_id)
        .order_by(ScanJob.created_at.desc())
        .limit(_RECENT_JOB_SCAN_LIMIT)
    ).all()
    for created_at, result_summary in rows:
        if not _is_dispatcher_collection_job(result_summary):
            continue
        created = _normalize_job_timestamp(created_at)
        if created is None:
            continue
        return (now - created).total_seconds() / 3600.0
    return None


# Consecutive-failure backoff for a config that can never succeed (tripl-n9ee).
#
# "Due" is derived from how far collection has actually got — the written metrics
# (``max(EventMetric.bucket)``) or the window a COMPLETED job recorded — and a
# collection that dies before writing its first row advances NEITHER, so the
# config is due again on the very next beat tick — every 300 s, forever, ignoring
# its own interval (the zero-row twin of this is tripl-wopq). Prod ran
# 200 consecutive failed jobs in 17 h for one 1h-interval config, each a ~30 s
# warehouse query, and the junk rows pushed real scan history out of the API's
# 200-row window.
#
# Backoff engages only after a RUN of failures, so a single blip — including a
# job the stale-job reaper above has just marked failed in this very tick —
# still retries immediately, exactly as before. A config that has never
# collected has no failures either, so its first run is untouched.
FAILURE_BACKOFF_AFTER = 3

# The delay doubles per failure past the threshold, in units of the config's OWN
# interval: 1x, 2x, 4x, ... So a failing config is never retried more often than
# a healthy one would run, which is the actual pathology here.
#
# Two ceilings, because one is not enough across a 15m..1w interval range:
#   * ``MAX_INTERVALS`` keeps the delay a small bounded multiple, so a 15m config
#     tops out at 2h rather than drifting into days;
#   * ``CEILING`` caps the absolute wait, so a 1w config does not end up on an
#     8-week retry (a fixed config would look dead for two months).
# The final delay is floored at one interval, which is what makes 1d/1w configs
# simply retry at their own cadence instead of being slowed below it.
FAILURE_BACKOFF_MAX_INTERVALS = 8
FAILURE_BACKOFF_CEILING = timedelta(hours=24)

# How far back to walk the job history when measuring the streak. The delay stops
# growing at ``FAILURE_BACKOFF_AFTER + 3`` failures, so this is comfortably past
# saturation; truncating an even longer streak can only SHORTEN the delay, which
# is the safe direction. Keeping it small also keeps this a cheap indexed read
# (mirrors ``_RECENT_JOB_SCAN_LIMIT`` above).
# Sized like ``_RECENT_JOB_SCAN_LIMIT`` rather than to the streak that saturates
# the delay: the LIMIT counts ROWS, and a demo's hourly runtime tick plus manual
# scans sit between two dispatcher jobs, so a tight bound would fill with rows
# the streak then skips and never reach the failures underneath.
_FAILURE_STREAK_SCAN_LIMIT = _RECENT_JOB_SCAN_LIMIT


def _consecutive_failure_streak(
    session: Session, scan_config_id: uuid.UUID
) -> tuple[int, datetime | None]:
    """Length of the run of failed jobs at the head of a config's history.

    Returns ``(streak, last_failure_at)``, or ``(0, None)`` when the newest job
    did not fail — the healthy case, and the only one that matters for cost since
    this is only ever called for a config that is already about to dispatch.

    Only this dispatcher's own collection jobs are considered. A manual scan, a
    replay or a demo tick on the same config is neither counted as a failure nor
    treated as the success that ends a streak, so a user pressing "Run now" can
    neither trigger the backoff nor clear it. Pending/running rows cannot appear
    here — the caller has already ``continue``d on a live job and reaped the
    stale ones into ``failed`` — so any non-failed head row is a genuine success.
    """
    rows = session.execute(
        select(ScanJob.status, ScanJob.completed_at, ScanJob.created_at, ScanJob.result_summary)
        .where(ScanJob.scan_config_id == scan_config_id)
        .order_by(ScanJob.created_at.desc())
        .limit(_FAILURE_STREAK_SCAN_LIMIT)
    ).all()

    streak = 0
    last_failure_at: datetime | None = None
    for status, completed_at, created_at, result_summary in rows:
        if not _is_dispatcher_collection_job(result_summary):
            continue
        if status != ScanJobStatus.failed.value:
            break
        streak += 1
        if last_failure_at is None:
            # ``completed_at`` is when the failure was recorded; fall back to
            # ``created_at`` for a row that somehow never got stamped, so a NULL
            # can't be read as "failed infinitely long ago" and defeat the wait.
            stamped = completed_at if completed_at is not None else created_at
            last_failure_at = _normalize_job_timestamp(stamped)
    return streak, last_failure_at


def _failure_backoff_delay(streak: int, interval_delta: timedelta) -> timedelta | None:
    """How long to wait after ``streak`` consecutive failures, or None for no wait."""
    if streak < FAILURE_BACKOFF_AFTER:
        return None
    ceiling = max(
        interval_delta,
        min(interval_delta * FAILURE_BACKOFF_MAX_INTERVALS, FAILURE_BACKOFF_CEILING),
    )
    # Pinned to int because ``int.__pow__`` is typed as returning Any (it yields a
    # float for a negative exponent); this exponent is >= 0 by the guard above.
    multiplier: int = 2 ** (streak - FAILURE_BACKOFF_AFTER)
    return min(interval_delta * multiplier, ceiling)


def scan_config_collection_schedule(
    *,
    last_bucket: datetime | None,
    watermark: datetime | None,
    delta: timedelta,
    now: datetime,
) -> tuple[datetime, bool]:
    """``(next_collection_at, due)`` for a scan config — the bucket half of the due check.

    Pure, so the async read path (``metrics_service``'s drilldown responses) can
    report the same answer the dispatcher acts on without porting the rule. When
    the collected grid lags the current interval boundary the config is due now
    and ``next_collection_at`` is ``now``; otherwise the next dispatch becomes
    due at the next boundary. Celery beat ticks every 300 s, so the value is the
    earliest due moment, not a second-exact promise.

    Deliberately only the bucket half: the dispatcher can still hold a due config
    back (a live job, the failure backoff, a demo's pause or cooldown). Those read
    job history and project state, so they stay in ``check_metrics_due``.
    """
    current_boundary = _floor_to_interval(now, delta)
    progress_to = collection_progress_to(last_bucket=last_bucket, watermark=watermark, delta=delta)
    if progress_to is None or progress_to < current_boundary:
        return now, True
    return current_boundary + delta, False


def scan_config_collection_progress(
    jobs: Iterable[tuple[object, datetime | None]],
) -> tuple[datetime | None, datetime | None]:
    """``(last_collected_at, watermark)`` from completed jobs, newest first.

    ``jobs`` are ``(result_summary, completed_at)`` pairs of a config's COMPLETED
    ``scan_jobs`` ordered by ``created_at`` descending — the rows
    ``_last_collected_window_to`` reads. Only dispatcher collection jobs count
    (``_is_dispatcher_collection_job``); ``last_collected_at`` is the newest such
    job's completion and ``watermark`` the newest parseable ``time_to``, so the
    read path and the worker agree on what "collected up to" means.
    """
    last_collected_at: datetime | None = None
    watermark: datetime | None = None
    for summary, completed_at in jobs:
        if not isinstance(summary, dict) or not _is_dispatcher_collection_job(summary):
            continue
        if last_collected_at is None and completed_at is not None:
            last_collected_at = _normalize_job_timestamp(completed_at)
        raw_to = summary.get("time_to")
        if watermark is None and isinstance(raw_to, str):
            with contextlib.suppress(ValueError):
                watermark = _parse_task_datetime(raw_to)
        if last_collected_at is not None and watermark is not None:
            break
    return last_collected_at, watermark


def _scan_config_collection_due(
    session: Session,
    scan_config_id: uuid.UUID,
    *,
    last_bucket: datetime | None,
    delta: timedelta,
    now: datetime,
) -> bool:
    """Whether a config has a complete interval bucket nobody has collected yet.

    Answers it through ``collection_progress_to``, the one definition the
    catalog-metric scheduler, the collection worker and the API's
    ``next_collection_at`` already share: progress is the LATER of the newest
    stored bucket's exclusive end and the last completed collection's window end,
    never the stored buckets alone.
    """

    def _due(watermark: datetime | None) -> bool:
        _next_at, due = scan_config_collection_schedule(
            last_bucket=last_bucket, watermark=watermark, delta=delta, now=now
        )
        return due

    # Short-circuited on purpose: the job watermark can only ever push progress
    # FORWARD, so a config that is already not due by its stored buckets cannot
    # become due by reading jobs — and skipping that read keeps a healthy config
    # at one job query per interval instead of one per 300 s beat tick.
    return _due(None) and _due(_last_collected_window_to(session, scan_config_id))


def _demo_resume_window_exceeds_full_volume(
    session: Session,
    scan_config_id: uuid.UUID,
    *,
    last_bucket: datetime | None,
    delta: timedelta,
    now: datetime,
) -> bool:
    """Whether a demo collection dispatched NOW would reach behind the synthetic
    warehouse's full-volume hours (tripl-0zpq.342).

    The collector resumes ``SCHEDULED_RESUME_OVERLAP_BUCKETS`` behind
    ``collection_progress_to`` and runs to the current boundary, deleting each
    window before rewriting it. The synthetic adapter serves only its newest
    ``SYNTHETIC_ONGOING_HOURS`` at full volume and a sampled trickle before that,
    so any window reaching further back rewrites real history at sampled volume.

    That happens right after a pause ends: the pause froze both progress
    signals, and until ``advance_demos`` backfills the missed buckets the resume
    point still sits at pause start. Which beat tick runs first is a race, so
    the window is checked here instead of relying on the backfill winning it.
    Computed conservatively (to the current boundary, not the settled end), so a
    true answer never lets a too-wide window through. ``None`` progress — never
    collected — is the default backfill's business and is not declined here.

    The gate only makes sense while the tick that closes the gap can run, and
    while the gap CAN close. With ``DEMO_RUNTIME_ENABLED=false`` nothing
    backfills, and only collections move progress, so the next dispatch — at
    least ``DEMO_COLLECTION_COOLDOWN_HOURS`` later — always reaches further back
    than the full-volume hours; likewise an interval whose resume overlap alone
    spans them. Declining there would stop scheduled collection for good, so
    both fall back to collecting as before.
    """
    if not settings.demo_runtime_enabled:
        return False
    overlap = delta * SCHEDULED_RESUME_OVERLAP_BUCKETS
    if overlap >= timedelta(hours=SYNTHETIC_ONGOING_HOURS):
        return False
    progress_to = collection_progress_to(
        last_bucket=last_bucket,
        watermark=_last_collected_window_to(session, scan_config_id),
        delta=delta,
    )
    if progress_to is None:
        return False
    window_from = progress_to - overlap
    reach = _floor_to_interval(now, delta) - window_from
    return reach > timedelta(hours=SYNTHETIC_ONGOING_HOURS)


# A single Postgres session-level advisory lock serialises the whole dispatcher
# across worker processes. With concurrency=N, a backlog of redelivered
# ``check_metrics_due`` messages (e.g. accumulated while the worker was busy or
# after a restart) used to be drained in parallel; the per-config "one active
# job" guard is not atomic, so overlapping runs each created a job for the same
# config in the same instant — duplicate pending/running rows plus duplicate-key
# crashes downstream in anomaly writes. Holding this lock for the duration of a
# run makes any overlapping run a no-op instead. No-op on non-Postgres backends
# (e.g. SQLite in tests).
_DISPATCH_ADVISORY_LOCK_KEY = 4_021_968_017

# A SECOND, distinct advisory lock for the catalog-metric dispatcher so it
# serialises across workers WITHOUT contending with ``check_metrics_due``. Using
# a separate key means catalog-metric dispatch never starves (nor is starved by)
# event-metric collection — the two dispatchers run independently.
_METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY = 4_021_968_018


@celery_app.task(name="tripl.worker.tasks.metrics.check_metrics_due")  # type: ignore[untyped-decorator]
def check_metrics_due() -> dict[str, int]:
    """Check which scan configs are due for metrics collection and dispatch tasks."""
    session = _get_sync_session()
    lock_conn = None
    try:
        bind = session.bind
        if bind is not None and bind.dialect.name == "postgresql":
            engine = bind if isinstance(bind, Engine) else bind.engine
            lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            acquired = bool(
                lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _DISPATCH_ADVISORY_LOCK_KEY},
                ).scalar()
            )
            if not acquired:
                logger.info(
                    "check_metrics_due: another dispatch run holds the advisory "
                    "lock; skipping this tick"
                )
                lock_conn.close()
                lock_conn = None
                return {"checked": 0, "dispatched": 0}

        config_rows = session.execute(
            select(
                ScanConfig,
                Project.is_demo,
                Project.demo_seeded_at,
                Project.demo_last_accessed_at,
            )
            .join(Project, Project.id == ScanConfig.project_id)
            .where(
                ScanConfig.interval.isnot(None),
                ScanConfig.time_column.isnot(None),
                # A suspended or deleting organization gets no new work (F20 PR14).
                project_in_active_org(),
            )
        ).all()
        configs = [row[0] for row in config_rows]
        # Demo-only project state, keyed by config: PRESENCE in this map is
        # ``is_demo`` and the value is the activity pair the pause gate reads.
        # Deliberately one map rather than an ``is_demo`` map beside a parallel
        # activity map — the two demo gates below would then have two things to
        # keep in agreement. Carried on the join the dispatcher already does
        # rather than re-read per config: this loop runs over every scan config
        # on every 300 s beat tick, and two more columns on one query is free
        # where a per-config Project load is not.
        demo_activity_by_config: dict[uuid.UUID, tuple[datetime | None, datetime | None]] = {
            row[0].id: (row[2], row[3]) for row in config_rows if row[1]
        }

        # One grouped aggregate instead of one max() per config per beat tick.
        # Safe to snapshot before the loop: collection is dispatched asynchronously
        # and each config's buckets are independent, so no config's watermark can
        # be advanced by an earlier iteration of this same loop.
        last_bucket_by_config: dict[uuid.UUID, datetime] = {}
        if configs:
            last_bucket_by_config = {
                config_id: bucket
                for config_id, bucket in session.execute(
                    select(EventMetric.scan_config_id, sa_func.max(EventMetric.bucket))
                    .where(EventMetric.scan_config_id.in_([config.id for config in configs]))
                    .group_by(EventMetric.scan_config_id)
                ).all()
            }

        dispatched = 0
        for config in configs:
            now = datetime.now(UTC)
            # Examine EVERY active job, not just the newest: reap all stale ones
            # so a stuck old job can't be permanently shadowed by a fresher
            # pending row, and skip dispatch only when a genuinely live
            # (non-stale) job remains.
            has_live_job = False
            for active_job in _get_active_scan_jobs(session, config.id):
                if not _fail_stale_active_scan_job(
                    session,
                    active_job,
                    now=now,
                    scan_name=config.name,
                ):
                    has_live_job = True
            if has_live_job:
                logger.info(
                    f"Skipping collect_metrics for {config.name!r}: "
                    "an active job is still in progress"
                )
                continue

            assert config.interval is not None
            interval_spec = get_interval(config.interval)
            delta = interval_spec.delta

            # Last metric bucket for this config, from the grouped read above.
            last_bucket = last_bucket_by_config.get(config.id)

            should_run = _scan_config_collection_due(
                session,
                config.id,
                last_bucket=last_bucket,
                delta=delta,
                now=now,
            )

            if should_run:
                # Only measured once the bucket check says "due", so a healthy
                # config pays this read once per interval instead of once per
                # 300 s beat tick — and, its newest job being a success, gets
                # streak 0 and dispatches unchanged (tripl-n9ee).
                streak, last_failure_at = _consecutive_failure_streak(session, config.id)
                backoff = _failure_backoff_delay(streak, delta)
                if backoff is not None and last_failure_at is not None:
                    waited = now - last_failure_at
                    if waited < backoff:
                        logger.info(
                            f"Skipping collect_metrics for {config.name!r}: "
                            f"{streak} consecutive failures, backing off "
                            f"{backoff} (last failure {waited} ago)"
                        )
                        should_run = False

            demo_activity = demo_activity_by_config.get(config.id)

            if should_run and demo_activity is not None:
                # A PAUSED demo must not be collected either (tripl-0zpq.72).
                #
                # ``advance_demos`` stops appending this demo's hourly buckets
                # while it is paused, so ``max(EventMetric.bucket)`` freezes and
                # with it ``collection_progress_to`` — the point the collector
                # resumes from. Every run from then on opens a window stretching
                # back to the previous run instead of the resume overlap alone,
                # far enough to reach behind the synthetic warehouse's
                # full-volume hours, and the collector DELETES a window before
                # rewriting it. ``_demo_pause`` carries the full argument and the
                # reason both callers must ask it rather than each testing
                # idleness their own way.
                #
                # Nothing is lost by skipping: an access unpauses the demo, the
                # next tick (300 s) backfills the missed buckets from the same
                # deterministic helpers the seeder used, and by the time
                # collection is dispatched again the window is back to the
                # overlap. A paused demo therefore ends up with neither the dips
                # nor a hole.
                #
                # Placed ahead of the cooldown so a demo that will be skipped
                # anyway does not pay the cooldown's job-history read, matching
                # how the failure backoff above is only measured once the cheap
                # bucket check has said "due".
                seeded_at, last_accessed = demo_activity
                if is_demo_paused(seeded_at, last_accessed, now):
                    logger.info(
                        f"Skipping collect_metrics for paused demo {config.name!r}: "
                        "no activity within the demo idle window"
                    )
                    should_run = False

            if (
                should_run
                and demo_activity is not None
                and _demo_resume_window_exceeds_full_volume(
                    session, config.id, last_bucket=last_bucket, delta=delta, now=now
                )
            ):
                # Just resumed, and ``advance_demos`` has not backfilled the
                # paused hours yet: the window would reach behind the synthetic
                # adapter's full-volume hours and rewrite them at sampled volume
                # (tripl-0zpq.342). The backfill tick (60 s) closes the gap, and
                # the next dispatcher tick then collects the normal overlap.
                logger.info(
                    f"Skipping collect_metrics for demo {config.name!r}: waiting for "
                    "the demo tick to backfill the paused hours"
                )
                should_run = False

            if should_run and demo_activity is not None:
                age_hours = _hours_since_last_scheduled_collection(session, config.id, now=now)
                if age_hours is not None and age_hours < DEMO_COLLECTION_COOLDOWN_HOURS:
                    logger.info(
                        f"Skipping collect_metrics for demo {config.name!r}: "
                        f"last scheduled collection was {age_hours:.1f}h ago "
                        f"(cooldown {DEMO_COLLECTION_COOLDOWN_HOURS}h)"
                    )
                    should_run = False

            if should_run:
                job = ScanJob(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    status=ScanJobStatus.pending.value,
                    # Stamp the mode at CREATION, not on completion: a run that
                    # dies before writing its summary leaves result_summary
                    # empty, and an unlabelled failed row is indistinguishable
                    # from a failed manual scan on the same scan_config_id. The
                    # failure streak below would then both count other people's
                    # failures and let one successful manual scan reset it.
                    result_summary={"mode": METRICS_COLLECTION_MODE},
                )
                session.add(job)
                session.commit()

                logger.info(
                    f"Dispatching collect_metrics for {config.name!r} (interval={config.interval})"
                )
                try:
                    collect_metrics.delay(str(config.id), str(job.id))
                except Exception as exc:
                    job.status = ScanJobStatus.failed.value
                    job.completed_at = datetime.now(UTC)
                    job.error_message = f"Failed to dispatch collect_metrics: {exc}"
                    session.commit()
                    raise
                dispatched += 1

        logger.info(f"check_metrics_due: {len(configs)} configs checked, {dispatched} dispatched")
        return {"checked": len(configs), "dispatched": dispatched}

    except Exception:
        logger.exception("check_metrics_due failed")
        raise
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _DISPATCH_ADVISORY_LOCK_KEY},
                )
            except Exception:  # pragma: no cover - best-effort lock release
                logger.exception("Failed to release check_metrics_due advisory lock")
            finally:
                lock_conn.close()
        session.close()


def _try_acquire_advisory_lock(session: object, key: int) -> tuple[object | None, bool]:
    """Try to grab a Postgres session advisory lock; no-op (acquired) elsewhere.

    Returns ``(lock_conn, acquired)``. On non-Postgres backends (SQLite tests)
    there is no lock, so it always reports acquired with ``lock_conn=None``.
    """
    bind = getattr(session, "bind", None)
    if bind is None or bind.dialect.name != "postgresql":
        return None, True
    engine = bind if isinstance(bind, Engine) else bind.engine
    lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    acquired = bool(
        lock_conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
    )
    if not acquired:
        lock_conn.close()
        return None, False
    return lock_conn, True


def _release_advisory_lock(lock_conn: object | None, key: int) -> None:
    """Best-effort release of an advisory lock acquired above."""
    if lock_conn is None:
        return
    try:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - best-effort lock release
        logger.exception("Failed to release metric-definition dispatch advisory lock")
    finally:
        lock_conn.close()  # type: ignore[attr-defined]


def _metric_collection_in_progress(definition: MetricDefinition, *, now: datetime) -> bool:
    """Whether a non-stale collection is already running for this metric.

    The single ``last_collection_status`` column doubles as the one-active-job
    guard (there is no separate job table for catalog metrics). A ``running``
    marker older than the staleness window is treated as dead, so a crashed run
    can be re-dispatched instead of wedging the metric forever.
    """
    if definition.last_collection_status != COLLECTION_STATUS_RUNNING:
        return False
    activity_at = _normalize_job_timestamp(definition.updated_at)
    return now - activity_at < STALE_ACTIVE_SCAN_JOB_TIMEOUT


def _max_event_metric_bucket(
    session: Session,
    *,
    event_id: uuid.UUID | None,
    event_type_id: uuid.UUID | None,
) -> datetime | None:
    if event_id is not None:
        condition = EventMetric.event_id == event_id
    elif event_type_id is not None:
        condition = EventMetric.event_type_id == event_type_id
    else:
        return None
    return session.execute(select(sa_func.max(EventMetric.bucket)).where(condition)).scalar()


def _event_composition_due(session: Session, definition: MetricDefinition) -> bool:
    """An event_composition metric is due when its source has newer buckets.

    It has no interval of its own; it re-derives from already-collected
    event_metrics, so "due" means the newest numerator event-metric bucket is
    ahead of the newest value we have composed.
    """
    # A structurally unconfigured metric is ALWAYS due, so the collector gets to
    # say so. Without this the guard would be inert: both refs NULL means no
    # source bucket, ``source_max`` is None, and this returns False forever —
    # which is exactly the half of tripl-jtnv that made the flatline permanent.
    # The hourly post-error backoff keeps it from becoming a storm.
    if event_composition_binding_error(definition) is not None:
        return True

    source_max = _max_event_metric_bucket(
        session,
        event_id=definition.numerator_event_id,
        event_type_id=definition.numerator_event_type_id,
    )
    if source_max is None:
        return False
    composed_max = session.execute(
        select(sa_func.max(MetricValue.bucket)).where(
            MetricValue.metric_definition_id == definition.id
        )
    ).scalar()
    return composed_max is None or composed_max < source_max


def _metric_definition_due(
    session: Session, definition: MetricDefinition, *, now: datetime
) -> bool:
    """Whether a metric has a newly complete bucket (or source data) to collect."""
    kind = (
        definition.kind if isinstance(definition.kind, MetricKind) else MetricKind(definition.kind)
    )
    if kind is MetricKind.event_composition:
        return _event_composition_due(session, definition)
    # ``sql`` carries its own ``data_source_id``; ``fact`` takes its data source
    # from the referenced FactTable (so ``data_source_id`` is NULL). Both gate on
    # the metric's own collection ``interval``.
    if definition.interval is None:
        return False
    delta = get_interval(definition.interval).delta
    last_bucket = session.execute(
        select(sa_func.max(MetricValue.bucket)).where(
            MetricValue.metric_definition_id == definition.id,
            MetricValue.scan_config_id.is_(None),
        )
    ).scalar()
    current_boundary = floor_to_bucket(now, str(definition.interval))
    progress_to = collection_progress_to(
        last_bucket=last_bucket,
        watermark=definition.last_collection_window_to,
        delta=delta,
    )
    return progress_to is None or progress_to < current_boundary


# Retry floor for a metric that has no interval of ITS OWN (tripl-wopq).
#
# ``event_composition`` re-derives from already-collected event_metrics on the
# source scan grid, which this dispatcher never loads, so the metric's natural
# cadence is not available here. An hour is 12x the 300 s beat that produced the
# storm, and deferring costs nothing: ``_collect_event_composition`` ignores its
# window and recomputes the FULL stored series on every run, so a skipped attempt
# loses no bucket — it only delays recovery by up to an hour after a fix.
NO_INTERVAL_ERROR_BACKOFF = timedelta(hours=1)


def _metric_definition_error_backoff(
    definition: MetricDefinition, *, now: datetime
) -> tuple[timedelta, timedelta] | None:
    """``(waited, delay)`` while a metric sits out its post-error cooldown, else None.

    Same pathology and same remedy as the scan-config backoff above: due-ness is
    derived from how far the WRITTEN values have advanced, and a collection that
    dies writes neither a value nor the ``last_collection_window_to`` watermark —
    so a metric that can never succeed is re-dispatched on every 300 s tick,
    ignoring its own interval, exactly as a broken scan config was before
    tripl-n9ee.

    It cannot climb the exponential curve, though: the catalog path has no job
    table — a single ``last_collection_status`` column is the entire history — so
    the RUN of consecutive failures the curve is indexed on cannot be measured
    without a schema change. What it does take from ``_failure_backoff_delay`` is
    that curve's first step, so the one fact both dispatchers must agree on ("a
    failing collection is never retried more often than a healthy one would run")
    keeps a single definition, and giving it a real streak later is a change of
    argument rather than a second curve.
    """
    if definition.last_collection_status != COLLECTION_STATUS_ERROR:
        return None
    delta = (
        get_interval(str(definition.interval)).delta
        if definition.interval is not None
        else NO_INTERVAL_ERROR_BACKOFF
    )
    delay = _failure_backoff_delay(FAILURE_BACKOFF_AFTER, delta)
    if delay is None:  # pragma: no cover - the curve is None only below the threshold
        return None
    # ``last_collection_failed_at`` is written ONLY by the collection cycle, so
    # it means exactly what this reads it as. ``updated_at`` did not: it carries
    # ``onupdate=func.now()``, so any write moved it, and an operator editing a
    # broken metric in order to fix it restarted the cooldown they were waiting
    # out (tripl-os3v).
    #
    # The fallback is for rows that failed BEFORE the column existed and carry
    # NULL. Treating those as "never failed" would release every one of them on
    # the first tick after deploy — the retry storm this backoff exists to stop,
    # produced by the fix for it. They keep the old reading until their next
    # real failure stamps the column.
    failed_at = definition.last_collection_failed_at or definition.updated_at
    waited = now - _normalize_job_timestamp(failed_at)
    if waited >= delay:
        return None
    return waited, delay


def metric_definition_cooldown_until(
    definition: MetricDefinition, *, now: datetime
) -> datetime | None:
    """When a metric sitting out a post-error cooldown becomes dispatchable, else None.

    Exists so the metric drilldown can answer "when does this collect next" with
    the SAME rule the dispatcher applies. Without it the API computed due-ness
    from the watermark alone and told the operator a collection was due now
    while this scheduler would skip it for up to a full interval — two answers
    to one question, which is the divergence this repository keeps paying for
    (tripl-os3v).

    Derived from ``_metric_definition_error_backoff`` rather than recomputing
    the curve, so the two cannot drift even if the curve changes.
    """
    backoff = _metric_definition_error_backoff(definition, now=now)
    if backoff is None:
        return None
    waited, delay = backoff
    return now + (delay - waited)


@celery_app.task(name="tripl.worker.tasks.metrics.check_metric_definitions_due")  # type: ignore[untyped-decorator]
def check_metric_definitions_due() -> dict[str, int]:
    """Dispatch ``collect_metric_definitions`` for every active metric that is due.

    Mirrors ``check_metrics_due``: a single advisory lock (distinct key)
    serialises the dispatcher across workers, and the per-metric
    ``last_collection_status`` marker prevents double-dispatching a metric whose
    collection is already running (stale ``running`` markers are reclaimed).
    """
    session = _get_sync_session()
    lock_conn = None
    try:
        lock_conn, acquired = _try_acquire_advisory_lock(
            session, _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY
        )
        if not acquired:
            logger.info(
                "check_metric_definitions_due: another dispatch run holds the "
                "advisory lock; skipping this tick"
            )
            return {"checked": 0, "dispatched": 0}

        definitions = (
            session.execute(
                select(MetricDefinition).where(
                    MetricDefinition.status == MetricStatus.active,
                    in_active_org(MetricDefinition.project_id),
                )
            )
            .scalars()
            .all()
        )

        now = datetime.now(UTC)
        dispatched = 0
        # Fact metrics are collected in shared warehouse scans, so they are
        # grouped by interval (one bucket grid per group) and dispatched as a
        # single batch task per group. ``sql`` / ``event_composition`` metrics
        # have no shared scan to exploit and stay per-metric.
        fact_groups: dict[str, list[MetricDefinition]] = {}
        for definition in definitions:
            if _metric_collection_in_progress(definition, now=now):
                logger.info(
                    "Skipping collect_metric_definitions for %r: collection already running",
                    definition.name,
                )
                continue
            if not _metric_definition_due(session, definition, now=now):
                continue
            # Measured only once the due check has passed, mirroring the
            # scan-config backoff: a healthy metric never reaches this branch on
            # the ticks between its own boundaries.
            error_backoff = _metric_definition_error_backoff(definition, now=now)
            if error_backoff is not None:
                waited, delay = error_backoff
                logger.info(
                    "Skipping collect_metric_definitions for %r: last collection "
                    "errored %s ago, backing off %s",
                    definition.name,
                    waited,
                    delay,
                )
                continue

            kind = (
                definition.kind
                if isinstance(definition.kind, MetricKind)
                else MetricKind(definition.kind)
            )
            if kind is MetricKind.fact and definition.interval is not None:
                fact_groups.setdefault(str(definition.interval), []).append(definition)
                continue

            # Mark running before dispatch (the one-active guard, analogous to
            # check_metrics_due creating a pending ScanJob first).
            definition.last_collection_status = COLLECTION_STATUS_RUNNING
            session.commit()

            logger.info(
                "Dispatching collect_metric_definitions for %r (kind=%s)",
                definition.name,
                definition.kind,
            )
            try:
                collect_metric_definitions.delay(str(definition.id))
            except Exception as exc:
                mark_collection_error(
                    definition, f"Failed to dispatch collect_metric_definitions: {exc}"
                )
                session.commit()
                raise
            dispatched += 1

        for interval_code, group in fact_groups.items():
            # Mark ALL members running before dispatch so the one-active guard
            # holds for the whole batch (mirrors the per-metric path above).
            for definition in group:
                definition.last_collection_status = COLLECTION_STATUS_RUNNING
            session.commit()

            metric_ids = [str(definition.id) for definition in group]
            logger.info(
                "Dispatching collect_fact_metrics_batch for %s fact metrics (interval=%s)",
                len(group),
                interval_code,
            )
            try:
                collect_fact_metrics_batch.delay(metric_ids)
            except Exception as exc:
                for definition in group:
                    mark_collection_error(
                        definition, f"Failed to dispatch collect_fact_metrics_batch: {exc}"
                    )
                session.commit()
                raise
            dispatched += len(group)

        logger.info(
            "check_metric_definitions_due: %s metrics checked, %s dispatched",
            len(definitions),
            dispatched,
        )
        return {"checked": len(definitions), "dispatched": dispatched}

    except Exception:
        logger.exception("check_metric_definitions_due failed")
        raise
    finally:
        _release_advisory_lock(lock_conn, _METRIC_DEFINITION_DISPATCH_ADVISORY_LOCK_KEY)
        session.close()
