"""Demo runtime tick — keep an ACTIVE demo fresh after creation.

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
in shape from what a live warehouse scan would have produced. A bucket is due
once an hour and the beat fires every five minutes, so most ticks find nothing
due: those only stamp the demo's collection time (see ``_advance_demo``) and
neither prune, re-detect nor broadcast.

Determinism
-----------
Every appended value is derived from the SAME traffic model the warehouse builder
seeds from (:class:`~tripl.core.adapters.synthetic_traffic.DemoTraffic`): volume
from ``hourly_volume`` with per-event noise from ``derive_seed(seed,
event_name)``, and the platform and app-version split of every event from
:func:`tripl.services.demo.breakdowns.breakdown_rows`. The model is anchored to
the demo's seed clock, stored on its synthetic source (``demo_seeded_at`` and
``DEMO_SEED`` for a demo seeded before the source carried it), so an appended
bucket continues the seeded series' slow upward drift and its release train
exactly, and holds what a scheduled collection of the same hour writes back. No
spike is injected on appended buckets — the seeded spike stays in history until
pruned.

Idempotency & concurrency
-------------------------
Correctness comes from DB unique constraints + insert-if-absent, NOT from the
broker or the advisory lock. Each bucket's rows are written inside a SAVEPOINT and
an ``IntegrityError`` (a concurrent tick already wrote that bucket) is swallowed,
so a re-run, a partial-failure retry, or two workers at the same clock all
converge to ONE logical result. On PostgreSQL a per-project
``pg_advisory_xact_lock`` additionally serialises workers (a production
optimisation), and the sweep itself runs one at a time: a beat that finds the
previous sweep still holding its session lock skips instead of queueing behind
it. Both locks are no-ops off PostgreSQL (SQLite tests), where the unique
constraints carry all the weight.

Independence
------------
The tick only touches ``is_demo`` projects, so REAL scan/metric scheduling is
entirely unaffected by it. It shares exactly ONE thing with ``check_metrics_due``:
the idle-pause rule in :mod:`tripl.worker.tasks._demo_pause`, which both must apply
or a demo this tick has stopped advancing gets collected with a window that
destroys its history (argued in that module).

What it reuses rather than restates
-----------------------------------
Anomaly re-detection is the scheduled collection's own volume-scope pass
(``metrics.detect._recalculate_metric_anomalies`` without the catalog-metric
scopes), so the project's anomaly settings, its scope toggles and per-scope
overrides, the archived-event filter, outage ranges and baselines apply exactly
as in a live scan, and a visitor's change to any of them holds from one tick to
the next. The tick writes no catalog-metric value either: the event-composition
metric (Purchase conversion) re-derives from the event series, so
``check_metric_definitions_due`` finds it due once a tick appends a newer bucket
and composes it with the real collector.

Tests monkey-patch ``_get_sync_session`` on this module's globals (the shared
sync-session pattern) and pass an explicit ``now`` (fake clock).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import delete, insert, select, text
from sqlalchemy import func as sa_func
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from tripl import cache, realtime
from tripl.config import settings
from tripl.core.adapters.synthetic_traffic import DemoTraffic, stored_traffic, traffic_params
from tripl.core.bucketing import to_utc
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.data_source import DataSource, DBType
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.planned_event import PlannedEvent
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.schema_drift import SchemaDrift
from tripl.services.active_org_scope import project_in_active_org
from tripl.services.demo import noise
from tripl.services.demo.breakdowns import (
    APP_VERSION_COLUMN,
    PLATFORM_COLUMN,
    EventVolume,
    breakdown_rows,
)
from tripl.services.demo.builders.alerts import DEMO_PLANNED_EVENT_LABEL
from tripl.services.demo.builders.governance import COVERAGE_MATCH_RATE
from tripl.services.demo.builders.plan import event_specs
from tripl.services.demo.builders.warehouse import SPIKE_ANNOTATION_LABEL
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
from tripl.worker.tasks.metrics.detect import _recalculate_metric_anomalies
from tripl.worker.utils.advisory_lock import release_advisory_lock, try_acquire_advisory_lock

logger = logging.getLogger(__name__)

# Stable log event name so a per-demo tick failure is greppable/alertable in
# aggregated logs even though the sweep swallows it to keep advancing other demos.
DEMO_TICK_FAILED_EVENT = "demo.runtime.tick_failed"

# The idle-pause rule itself lives in :mod:`tripl.worker.tasks._demo_pause`
# (``DEMO_IDLE_PAUSE_MINUTES`` / ``is_demo_paused``) because the metrics dispatcher
# has to apply the SAME rule: a demo this tick has stopped advancing must not have
# its scheduled collection dispatched either, or that collection's window reaches
# back past the synthetic warehouse's full-volume hours and overwrites real history
# with sampled near-zero counts.

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
_HOUR = timedelta(hours=1)
# One sweep at a time. Distinct from the metrics dispatchers (…017 / …018), the
# digest flusher (…019) and the overdue-source sweep (…020), so none of them can
# starve another.
_ADVANCE_DEMOS_ADVISORY_LOCK_KEY = 4_021_968_021


class _Series(NamedTuple):
    """One seeded per-event series the tick advances."""

    event_id: uuid.UUID
    event_type_id: uuid.UUID
    base: int
    name: str
    # The event type's NAME, which decides whether the platform mix drifts.
    event_type: str


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

    tick_now = to_utc(now) if now is not None else datetime.now(UTC)
    session = _get_sync_session()
    lock_conn, acquired = try_acquire_advisory_lock(session, _ADVANCE_DEMOS_ADVISORY_LOCK_KEY)
    if not acquired:
        # The previous sweep is still running: an hour's re-detection over many
        # active demos can outlast the five-minute beat. Waiting behind it would
        # only hold a worker slot that scans, collections and alert deliveries
        # share; the next beat after it finishes picks up whatever it missed.
        logger.info("advance_demos: another sweep holds the lock; skipping this tick")
        session.close()
        return {"enabled": True, "running": True, "advanced": 0, "skipped": 0}
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
                project_in_active_org(),
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
        release_advisory_lock(lock_conn, _ADVANCE_DEMOS_ADVISORY_LOCK_KEY, name="advance_demos")
        session.close()


def _advance_demo(session: Session, project_id: uuid.UUID, slug: str, now: datetime) -> None:
    """Advance one demo inside a per-project advisory-locked transaction."""
    _acquire_project_xact_lock(session, project_id)

    project = session.get(Project, project_id)
    if project is None or project.demo_seeded_at is None:
        session.rollback()
        return
    org_id = project.organization_id

    scan_config = session.execute(
        select(ScanConfig).where(ScanConfig.project_id == project_id).limit(1)
    ).scalar_one_or_none()
    if scan_config is None:
        # No warehouse surface to advance; still stamp so we don't reselect hot.
        project.demo_last_tick_at = now
        session.commit()
        return

    scan_config_id = scan_config.id
    traffic = _demo_traffic(session, scan_config, to_utc(project.demo_seeded_at))
    roster = _load_series_roster(session, scan_config_id, now)

    new_buckets = _pending_buckets(session, scan_config_id, now)
    written: list[_BucketRows] = []
    if roster and new_buckets:
        written = _append_buckets(session, scan_config, roster, new_buckets, traffic)

    # Source freshness facts (#269), stamped the way a live collection stamps
    # them: the tick IS the demo's collection, so every tick that reaches the
    # scan counts as a completed collection even when nothing was due — without
    # it the seeded values age out and a live demo reads late/overdue.
    scan_config.last_collection_at = now
    project.demo_last_tick_at = now
    if not written:
        # Nothing new: no hour was due yet, or a scheduled collection already
        # wrote it (and scored and broadcast it itself). There is nothing to
        # re-score and nothing a viewer would see change, so no detection and
        # no broadcast; pruning waits for the next written hour, which removes
        # everything past the cutoff at once.
        session.commit()
        return

    # The newest appended bucket moves ``last_event_at`` forward (never back).
    scan_config.last_event_at = advance_last_event_at(scan_config.last_event_at, new_buckets[-1])
    _record_scan_job(session, scan_config_id, now, written, roster)
    # The same hold ``collect_metrics`` applies: judged on the facts just
    # stamped, so a tick that caught the demo up is fresh and holds nothing.
    # It only bites if the demo's series stops short of the clock (a
    # simulated delay), and then the tick must not write the drops a live
    # scan would have held.
    freshness = compute_config_freshness(session, scan_config, project_id, now)
    _recompute_anomalies(session, scan_config, now, hold_drops=is_holding(freshness))
    _prune_retention(session, project_id, scan_config_id, now)
    session.commit()

    _emit_status(project_id, org_id, slug)


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


def _demo_traffic(session: Session, scan_config: ScanConfig, seeded_at: datetime) -> DemoTraffic:
    """The traffic model the demo was seeded from, which its synthetic source serves.

    Read off the source, where the seeder stores it. A demo seeded before the
    source carried it gets the model it was seeded with all along — the seed
    clock and the default scenario seed — stamped on its source here, so the
    next scheduled collection serves the same platform and app-version split the
    tick appends instead of an even one.
    """
    source = session.get(DataSource, scan_config.data_source_id)
    traffic = stored_traffic(source.extra_params) if source is not None else None
    if traffic is not None:
        return traffic
    traffic = DemoTraffic(anchor=seeded_at, seed=DEMO_SEED)
    if source is not None and source.db_type == DBType.synthetic:
        stored = source.extra_params if isinstance(source.extra_params, dict) else {}
        # A new dict, so the JSON column registers the change.
        source.extra_params = {**stored, **traffic_params(traffic)}
    return traffic


def _load_series_roster(
    session: Session, scan_config_id: uuid.UUID, now: datetime
) -> list[_Series]:
    """The per-event series to advance.

    Derived from the EXISTING seeded per-event ``EventMetric`` rows joined back to
    their events, so the tick advances exactly the series that were seeded (correct
    ids, no branch-copy duplicates) and reuses each event's authored ``base``
    volume and event type from :func:`event_specs`.
    """
    specs_by_name = {spec.name: spec for spec in event_specs(now)}
    rows = session.execute(
        select(Event.id, Event.name, Event.event_type_id)
        .join(EventMetric, EventMetric.event_id == Event.id)
        .where(EventMetric.scan_config_id == scan_config_id)
        .distinct()
    ).all()
    roster: list[_Series] = []
    for event_id, name, event_type_id in rows:
        spec = specs_by_name.get(name)
        if spec is not None:
            roster.append(_Series(event_id, event_type_id, spec.base, name, spec.event_type))
    return roster


def _pending_buckets(session: Session, scan_config_id: uuid.UUID, now: datetime) -> list[datetime]:
    """Hourly buckets to append: ``(last_populated, latest_complete]``, retention-bounded."""
    last_bucket = session.execute(
        select(sa_func.max(EventMetric.bucket)).where(EventMetric.scan_config_id == scan_config_id)
    ).scalar()
    if last_bucket is None:
        return []
    last_bucket = to_utc(last_bucket)

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
    scan_config: ScanConfig,
    roster: list[_Series],
    new_buckets: list[datetime],
    traffic: DemoTraffic,
) -> list[_BucketRows]:
    """Insert-if-absent every new bucket's rows; return what each written bucket added.

    Each bucket is written inside a SAVEPOINT so a unique-constraint clash from a
    concurrent tick (or a re-run) rolls back just that bucket and is skipped,
    leaving one logical result.
    """
    scan_config_id = scan_config.id
    # Cheap pre-check: skip buckets already fully written (common on a same-clock
    # re-run) before paying for a savepoint.
    existing = {
        to_utc(b)
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
    # The splits the scan stores: a column the demo scan no longer designates (a
    # user cleared or repointed it) gets no rows, as a collection would write none.
    columns = _BreakdownColumns(
        platform=PLATFORM_COLUMN if scan_config.platform_column == PLATFORM_COLUMN else None,
        version=(
            APP_VERSION_COLUMN if scan_config.app_version_column == APP_VERSION_COLUMN else None
        ),
    )

    written: list[_BucketRows] = []
    for bucket in new_buckets:
        if bucket in existing:
            continue
        try:
            with session.begin_nested():
                rows = _build_bucket_rows(
                    session, scan_config_id, roster, bucket, traffic, columns=columns
                )
                session.flush()
            written.append(rows)
        except IntegrityError:
            # A concurrent tick already wrote this bucket — idempotent skip.
            logger.debug("advance_demos: bucket %s already present, skipping", bucket)
    return written


class _BreakdownColumns(NamedTuple):
    """The breakdown columns the tick writes, ``None`` for one it leaves out."""

    platform: str | None
    version: str | None


class _BucketRows(NamedTuple):
    """What one appended bucket stored, counted the way a collection reports it."""

    # The hour's matched warehouse volume: the sum of its per-type counts.
    volume: int
    event_metrics: int
    type_metrics: int
    breakdown_event_metrics: int
    breakdown_type_metrics: int


def _build_bucket_rows(
    session: Session,
    scan_config_id: uuid.UUID,
    roster: list[_Series],
    bucket: datetime,
    traffic: DemoTraffic,
    *,
    columns: _BreakdownColumns,
) -> _BucketRows:
    """Add one bucket's rows: per-event + per-type EventMetric, breakdown, coverage."""
    per_type_total: dict[uuid.UUID, int] = {}
    volumes: list[EventVolume] = []

    for series in roster:
        count = traffic.volume(series.base, series.name, bucket)
        session.add(
            EventMetric(
                scan_config_id=scan_config_id,
                event_id=series.event_id,
                event_type_id=None,
                bucket=bucket,
                count=count,
            )
        )
        per_type_total[series.event_type_id] = per_type_total.get(series.event_type_id, 0) + count
        volumes.append(
            EventVolume(
                event_id=series.event_id,
                event_type_id=series.event_type_id,
                name=series.name,
                event_type=series.event_type,
                base=series.base,
                count=count,
            )
        )

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

    # Every event's platform and app-version split, continuing the seeded
    # history (same model, same anchor), with the per-type rollups.
    rows = breakdown_rows(
        traffic,
        bucket,
        volumes,
        scan_config_id=scan_config_id,
        platform_column=columns.platform,
        version_column=columns.version,
    )
    if rows:
        session.execute(insert(EventMetricBreakdown), rows)
    breakdown_event_rows = sum(1 for row in rows if row["event_id"] is not None)

    matched = sum(per_type_total.values())
    if matched:
        session.add(
            CoverageMetric(
                scan_config_id=scan_config_id,
                bucket=bucket,
                total_count=max(matched, round(matched / COVERAGE_MATCH_RATE)),
                matched_count=matched,
            )
        )

    return _BucketRows(
        volume=matched,
        event_metrics=len(roster),
        type_metrics=len(per_type_total),
        breakdown_event_metrics=breakdown_event_rows,
        breakdown_type_metrics=len(rows) - breakdown_event_rows,
    )


def _record_scan_job(
    session: Session,
    scan_config_id: uuid.UUID,
    now: datetime,
    written: list[_BucketRows],
    roster: list[_Series],
) -> None:
    """Record a completed ScanJob reflecting THIS tick's real execution.

    Not a timestamp rewrite of an old job — a genuinely new run row so the demo's
    scan history keeps growing (and reads healthy: the latest job is a success).
    """
    started = now - timedelta(seconds=8)
    event_types = len({series.event_type_id for series in roster})
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
                # Warehouse rows: a catalog run's ``catalog_rows_scanned``, not
                # its ``scan_rows_processed`` (column combinations).
                "catalog_rows_scanned": sum(rows.volume for rows in written),
                # The metric points this tick stored, under the counters a
                # collection reports them in, so the scan page counts the run.
                # Deliberately no ``mode``: the dispatcher takes its watermark
                # and the demo's collection cooldown from its own
                # ``metrics_collection`` runs only.
                "event_metrics": sum(rows.event_metrics for rows in written),
                "type_metrics": sum(rows.type_metrics for rows in written),
                "breakdown_event_metrics": sum(rows.breakdown_event_metrics for rows in written),
                "breakdown_type_metrics": sum(rows.breakdown_type_metrics for rows in written),
                "buckets_appended": len(written),
                "demo_runtime_tick": True,
            },
        )
    )


def _recompute_anomalies(
    session: Session,
    config: ScanConfig,
    now: datetime,
    *,
    hold_drops: bool = False,
) -> None:
    """Re-score the demo's volume scopes over the fresh window, as a collection does.

    Runs the scheduled collection's own pass
    (``metrics.detect._recalculate_metric_anomalies``) over the project total,
    the event types and the events, without the catalog-metric scopes: the
    project's settings, scope toggles and per-scope overrides, the
    archived-event filter, outage ranges and baselines all apply as in a live
    scan. ``hold_drops`` (the demo source is late, #269) withholds NEW
    drop-direction anomalies and spares stored drop rows, as for a live scan.

    Best-effort: a detection failure must not fail the tick (the appended series
    stays coherent for a later real collection). Idempotent: the pass replaces
    each scope's rows in the window, so a re-run at the same clock yields the
    same rows. The window's attributions (#255) are recomputed after it, inside
    the same guard, because the replaced rows took theirs with them.
    """
    eval_end = _floor_hour(now)
    eval_start = eval_end - timedelta(hours=noise.DEMO_EVAL_WINDOW_HOURS)
    try:
        _recalculate_metric_anomalies(
            session,
            config,
            evaluation_start=eval_start,
            evaluation_end=eval_end,
            hold_drops=hold_drops,
            catalog_metrics=False,
        )
        # The demo's Why panel and alerts then read the same split a real
        # collection stores. The window reaches one bucket past ``eval_end`` so
        # the newest flagged bucket is covered.
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
        # Anything else is a detector or logic bug, not a broken transaction: every
        # statement before it succeeded, so the session can still commit the
        # appended hour. Scopes already replaced stand (each replace is whole on its
        # own) and the next written hour re-scores the window. A detector bug must
        # not be able to kill the whole tick.
        logger.exception("advance_demos: anomaly recompute failed for %s", config.id)


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
    # The chart band the hourly re-detection writes for every scored bucket.
    session.execute(
        delete(MetricBaseline).where(
            MetricBaseline.scan_config_id == scan_config_id, MetricBaseline.bucket < cutoff
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
    # appends no new spike, so re-dating the marker would label nothing either.
    # Matched on the seeder's label, so an annotation the user
    # wrote in the demo is left alone.
    session.execute(
        delete(ChartAnnotation).where(
            ChartAnnotation.project_id == project_id,
            ChartAnnotation.label == SPIKE_ANNOTATION_LABEL,
            ChartAnnotation.bucket < cutoff,
        )
    )
    # The seeded planned promo (F18) retires the same way, once its window ends
    # behind the cutoff; a planned event the user added is left alone.
    session.execute(
        delete(PlannedEvent).where(
            PlannedEvent.project_id == project_id,
            PlannedEvent.label == DEMO_PLANNED_EVENT_LABEL,
            PlannedEvent.ends_at < cutoff,
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


def _emit_status(project_id: uuid.UUID, org_id: uuid.UUID, slug: str) -> None:
    """Emit a project-scoped 'updated' signal for the live stream.

    Invalidates the organization's project list and the project's signals so the
    next read serves the freshly-appended series, then publishes the realtime
    events so subscribed clients refresh without waiting on the polling fallback.
    Redis-off (tests) is a no-op for both. Runs AFTER the tick's commit.
    """
    cache.sync_delete(cache.key_projects_list(org_id))
    cache.sync_delete_prefix(cache.prefix_signals(project_id))
    realtime.publish_project_event(
        project_id, slug, realtime.EVENT_METRIC_COLLECTION_UPDATED, {"source": "demo_runtime"}
    )
    realtime.publish_project_event(
        project_id, slug, realtime.EVENT_SIGNALS_UPDATED, {"source": "demo_runtime"}
    )
