"""The observed half of a demo: tripl's own pipeline over the demo warehouse.

After the authored builders (:mod:`.scenario`) have written the plan, the
seed task runs, synchronously and on its own session, exactly what a team's
first week with tripl would:

* **scan** — the real catalog scan (``tasks.scan.execute_scan``) over the
  demo source: it matches the authored events by name, records field observed
  values and variable contexts, and finds the open value drift the warehouse
  carries;
* **history** — a real metrics replay (``tasks.metrics.execute_collect_metrics``)
  over :func:`~.noise.history_days` days in one-day chunks. It walks
  forward in week-long jobs that store their windows, one of which ends just
  after the newest incident's first complete hour. That job alone also runs
  detection, attribution and dispatch, once, over the last three weeks
  (:func:`scoring_window`): the dispatcher sees the incident at the head of
  its series and files the alert deliveries a live collection would have. The
  last window is the scheduler's first live collection instead, up to the
  seed hour: it runs the field-contract check a replay skips (the warehouse
  breaks the ``currency`` enum with GBP and the ``amount`` minimum with 0.00
  rows) and starts the source's
  freshness watermarks. Everything these jobs observe — anomalies,
  attributions, distribution drift, schema drift, release regressions and
  markers, shadow candidates, coverage — is the pipeline's;
* **signals** — the catalog metrics through the worker's collectors, the
  lifecycle pass, then the human reactions to what was observed (the stated
  exceptions to "observations are scanned"): the weekly promo's anomalies
  marked expected, the planned promo over a catalog metric, a note on the
  newest incident. The search index is rebuilt last.

Each stage commits as the worker does. ``demo_service`` owns the stages, the
cancel checks between them and the cleanup on failure.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.config import settings
from tripl.core.bucketing import to_utc
from tripl.core.demo_warehouse import incidents_between, promo_windows
from tripl.core.demo_warehouse.calendar import PROMO_EVENT, PROMO_TITLE
from tripl.core.demo_warehouse.engine import connection_message
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.data_source import DataSource, TestStatus
from tripl.models.domain_enums import (
    AnomalyDirection,
    ChartAnnotationScopeType,
    MetricScopeType,
    SignalExpectedReason,
    SignalTriageAction,
)
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_value import MetricValue
from tripl.models.planned_event import PlannedEvent
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.signal_triage import SignalTriage
from tripl.services.demo import noise
from tripl.services.demo.builders.catalog import REVENUE_METRIC_NAME
from tripl.services.demo.scenario import DemoContext
from tripl.services.planned_event_service import retag_planned_anomalies
from tripl.services.search_service import reindex_project_branch

logger = logging.getLogger(__name__)

STAGE_PLAN = "plan"
STAGE_SCAN = "scan"
STAGE_HISTORY = "history"
STAGE_SIGNALS = "signals"

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)

#: The planned promo over the revenue metric (F18): a promotion the team ran,
#: drawn around the best recent revenue day, so its lift is expected.
DEMO_PLANNED_EVENT_LABEL = "Spring promo (planned)"
_PLANNED_EVENT_LOOKBACK_DAYS = 7
_PLANNED_EVENT_LEAD = timedelta(days=1)
_PLANNED_EVENT_SPAN = timedelta(days=2)

#: The note on each weekly promo send's *expected* verdict; the suggestion it
#: leads to on the Annotations page quotes it.
DEMO_WEEKLY_PROMO_NOTE = PROMO_TITLE

#: The note someone left on the newest incident in the inbox.
DEMO_INCIDENT_NOTE = (
    "Looking into it: lines up with the 13:00 UTC change window. Checking the "
    "deploy log before paging anyone."
)


class DemoPipelineError(RuntimeError):
    """A pipeline job finished without completing (its error is in the message)."""


# -- scan -------------------------------------------------------------------------


#: How long a build waits for a writer run already in progress (a cold
#: warehouse's first backfill writes ~30 days; measured well under a minute).
WAREHOUSE_CATCH_UP_WAIT_SECONDS = 240


def bring_warehouse_current(now: datetime) -> None:
    """Make the PostgreSQL warehouse hold every row up to ``now`` before a build reads it.

    The beat writes every five minutes, so a build that starts between two runs
    — or on a fresh install whose first backfill is still running — would replay
    days the warehouse does not hold yet and score them as silence. This runs
    the writer inline (waiting for a run in progress), then refreshes the status
    cache so the engine choice that follows sees the result. A no-op on the
    in-memory engine, and where the warehouse is not reachable.
    """
    from tripl.core.demo_warehouse import writer
    from tripl.core.demo_warehouse.status import warehouse_status

    if settings.demo_warehouse_mode == "memory":
        return
    writer.write(now, wait_seconds=WAREHOUSE_CATCH_UP_WAIT_SECONDS)
    warehouse_status(refresh=True)


async def run_scan_stage(session: AsyncSession, ctx: DemoContext) -> None:
    """The real catalog scan, then the source's connection-status line."""
    await asyncio.to_thread(bring_warehouse_current, ctx.now)
    config_id = _require(ctx.scan_config_id, "scan config")
    job_id = await _new_job(session, config_id)

    from tripl.worker.tasks.scan import execute_scan

    await session.run_sync(execute_scan, str(config_id), str(job_id))
    await _require_completed(session, job_id, "catalog scan")
    await session.run_sync(_stamp_connection, _require(ctx.data_source_id, "data source"), ctx.now)
    await session.commit()


def _stamp_connection(session: Session, data_source_id: uuid.UUID, now: datetime) -> None:
    """Which engine served the scan, as a test of the source would say it."""
    from tripl.core.adapters.registry import build_adapter

    data_source = session.get(DataSource, data_source_id)
    if data_source is None:
        return
    adapter = build_adapter(data_source)
    try:
        adapter.test_connection()
        data_source.last_test_status = TestStatus.success
        data_source.last_test_message = connection_message(adapter)
    finally:
        adapter.close()
    data_source.last_test_at = now


# -- history ----------------------------------------------------------------------


#: The longest window one replay job covers. A replay samples its JSON
#: properties over its whole window in one query, and the in-memory engine reads
#: at most eight days of events per query.
REPLAY_JOB_SPAN = timedelta(days=7)


def history_windows(now: datetime) -> list[tuple[datetime, datetime]]:
    """The replay's walk-forward windows ``[start, end)``, oldest first.

    ``now`` is the seed hour. One window ends right after the earliest
    complete incident bucket that is still recent enough to alert on (the
    dispatcher alerts only on a scope's newest bucket, and only within a day of
    the wall clock), so that job ends with an incident at the head of its
    series; the rest of the history is cut into :data:`REPLAY_JOB_SPAN` jobs.
    """
    start = noise.history_start(now)
    end = now.replace(minute=0, second=0, microsecond=0)
    split = _alert_split(end)
    cuts = [start]
    # Every cut is a whole job span from the start until the split, so the
    # split's own job is the only short one (and the scoring job, below).
    while cuts[-1] + REPLAY_JOB_SPAN < (split if split is not None and split > start else end):
        cuts.append(cuts[-1] + REPLAY_JOB_SPAN)
    if split is not None and cuts[-1] < split < end:
        cuts.append(split)
    cuts.append(end)
    return list(zip(cuts, cuts[1:], strict=False))


#: How far back the build's one detection pass scores: three weekly promo
#: sends (each gets its *expected* verdict) and three weeks of incidents on the
#: Anomalies page. The fit behind each scope reads the whole stored history
#: either way, so a longer span costs next to nothing.
SCORING_SPAN = timedelta(days=21)


def scoring_window(now: datetime) -> tuple[datetime, datetime]:
    """The ``[start, end)`` the build's one detection pass scores.

    It ends where the alerting job ends (:func:`history_windows`), so the
    incident there is the newest bucket the pass sees and the dispatcher files
    its alert; the hours after it are stored, and the scheduler's next
    collection scores them.
    """
    end = now.replace(minute=0, second=0, microsecond=0)
    split = _alert_split(end)
    scored_to = split if split is not None else end
    return max(noise.history_start(now), scored_to - SCORING_SPAN), scored_to


def _alert_split(end: datetime) -> datetime | None:
    # A bucket at or after ``end - 23h`` is within 24h of any instant inside
    # the seed hour, and ``bucket + 1h <= end`` keeps it complete.
    earliest = end - timedelta(hours=23)
    candidates = [
        bucket
        for incident in incidents_between(end - 2 * _DAY, end)
        for bucket in _hours(incident.start, incident.end)
        if earliest <= bucket and bucket + _HOUR <= end
    ]
    return min(candidates) + _HOUR if candidates else None


def _hours(start: datetime, end: datetime) -> list[datetime]:
    out: list[datetime] = []
    bucket = start
    while bucket < end:
        out.append(bucket)
        bucket += _HOUR
    return out


async def run_history_stage(
    session: AsyncSession,
    ctx: DemoContext,
    *,
    deliveries: list[str],
    between_jobs: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Collect the warehouse history; collect the alert deliveries it files.

    Every job stores its window, and one of them — the job ending at the
    alerting split — also scores :func:`scoring_window`: a single detection,
    attribution and dispatch pass over the history, where a job each would
    re-fit every scope's seasonal model once per job.

    ``between_jobs`` is awaited after each committed job (the caller's cancel
    check); the deliveries are enqueued by the caller once the demo is ready.
    """
    config_id = _require(ctx.scan_config_id, "scan config")

    from tripl.worker.tasks.metrics.tasks import execute_collect_metrics

    windows = history_windows(ctx.now)
    scored_from, scored_to = scoring_window(ctx.now)
    for index, (time_from, time_to) in enumerate(windows):
        # The last window is the scheduler's first tick rather than a replay: a
        # replay skips the field-contract check (so the contract violations the
        # warehouse carries are never filed) and leaves the freshness watermarks
        # alone. A live collection resumes where the replays stopped, which is
        # this window's start, and runs up to the current hour.
        live = index == len(windows) - 1
        scores = time_to == scored_to
        job_id = await _new_job(session, config_id)
        with _collector_paused():
            await session.run_sync(
                execute_collect_metrics,
                str(config_id),
                str(job_id),
                None if live else time_from.isoformat(),
                None if live else time_to.isoformat(),
                enqueue_delivery=deliveries.append,
                skip_detection=not scores,
                evaluation_from=scored_from if scores else None,
            )
        await _require_completed(session, job_id, "metrics collection" if live else "replay")
        if between_jobs is not None:
            await between_jobs()


@contextmanager
def _collector_paused() -> Iterator[None]:
    """The cyclic garbage collector off for one collection job, then one full pass.

    A job allocates millions of short-lived rows and keys beside long-lived
    ones (the warehouse model's cached hours, the session's identity map), and
    the collector's repeated passes over the long-lived ones were about 40 % of
    a replay's time. Paused for the job, its garbage is reclaimed in one pass at
    the end; a job holds what it allocates for its own duration anyway. The
    seed runs alone in its worker process (or inline in a test), so the pause
    meets no other work.
    """
    if not gc.isenabled():
        yield
        return
    gc.disable()
    try:
        yield
    finally:
        gc.enable()
        gc.collect()


def history_progress(summaries: Sequence[Mapping[str, object]]) -> int:
    """0-100 through the history, from the replay jobs' own progress summaries.

    Each replay job records its window and completed chunks as it goes
    (``tasks.metrics._heartbeat_replay_progress``); the share of the
    history (:func:`.noise.history_days`) they cover is the stage's progress.
    99 at most: the stage is done only when the provisioning says so.
    """
    total = timedelta(days=noise.history_days()).total_seconds()
    covered = 0.0
    for summary in summaries:
        try:
            time_from = to_utc(datetime.fromisoformat(str(summary["time_from"])))
            time_to = to_utc(datetime.fromisoformat(str(summary["time_to"])))
            done = int(str(summary.get("replay_chunks_completed") or 0))
            chunks = int(str(summary.get("replay_chunks_total") or 0))
        except KeyError, ValueError, TypeError:
            continue
        if chunks <= 0:
            continue
        covered += (time_to - time_from).total_seconds() * min(done, chunks) / chunks
    return max(0, min(99, int(100 * covered / total)))


# -- signals ----------------------------------------------------------------------


async def run_signals_stage(session: AsyncSession, ctx: DemoContext) -> None:
    await collect_catalog_metrics(session, ctx)
    from tripl.worker.tasks.lifecycle import compute_lifecycle_findings

    await session.run_sync(compute_lifecycle_findings, now=datetime.now(UTC))
    await session.commit()
    await mark_weekly_promo_expected(session, ctx)
    await plan_promo_over_revenue(session, ctx)
    await note_newest_incident(session, ctx)
    await reindex_project_branch(
        session,
        project_id=ctx.project_id,
        branch_id=ctx.branch_id,
        slug=ctx.slug,
        schedule_embeddings=False,
        commit=False,
    )
    await session.commit()


async def collect_catalog_metrics(session: AsyncSession, ctx: DemoContext) -> None:
    """Every catalog metric through the worker's collectors, over the history.

    Daily metrics, so the window is the history's whole days.
    """
    from tripl.worker.tasks.metrics.metric_collect import execute_collect_metric_definition

    day_end = ctx.now.replace(hour=0, minute=0, second=0, microsecond=0)
    window = (day_end - noise.history_days() * _DAY, day_end)
    for metric_id in ctx.metric_ids.values():
        await session.run_sync(execute_collect_metric_definition, str(metric_id), window=window)
        await session.commit()


async def mark_weekly_promo_expected(session: AsyncSession, ctx: DemoContext) -> int:
    """Mark each replay-detected weekly promo spike on the promo event as expected.

    One verdict per send — the first spike bucket inside each promo window —
    which is what the planned-window suggestions look for (#271).
    """
    event_id = ctx.event_ids.get(PROMO_EVENT)
    if event_id is None or ctx.scan_config_id is None:
        return 0
    start = noise.history_start(ctx.now)
    windows = promo_windows(start, ctx.now)
    anomalies = (
        (
            await session.execute(
                select(MetricAnomaly)
                .where(
                    MetricAnomaly.scan_config_id == ctx.scan_config_id,
                    MetricAnomaly.scope_type == MetricScopeType.event.value,
                    MetricAnomaly.scope_ref == str(event_id),
                    MetricAnomaly.direction == AnomalyDirection.spike.value,
                    MetricAnomaly.bucket >= start,
                )
                .order_by(MetricAnomaly.bucket)
            )
        )
        .scalars()
        .all()
    )
    marked = 0
    for window_start, window_end in windows:
        inside = [
            anomaly for anomaly in anomalies if window_start <= to_utc(anomaly.bucket) < window_end
        ]
        if not inside:
            continue
        session.add(
            SignalTriage(
                project_id=ctx.project_id,
                scan_config_id=ctx.scan_config_id,
                scope_type=MetricScopeType.event.value,
                scope_ref=str(event_id),
                action=SignalTriageAction.expected.value,
                bucket=inside[0].bucket,
                note=DEMO_WEEKLY_PROMO_NOTE,
                expected_reason=SignalExpectedReason.campaign.value,
                created_by_user_id=ctx.created_by,
            )
        )
        marked += 1
    await session.commit()
    return marked


async def plan_promo_over_revenue(session: AsyncSession, ctx: DemoContext) -> None:
    """The planned promo window, around the best revenue day of the last week."""
    metric_id = ctx.metric_ids.get(REVENUE_METRIC_NAME)
    if metric_id is None:
        return
    since = ctx.now - _PLANNED_EVENT_LOOKBACK_DAYS * _DAY
    best = (
        await session.execute(
            select(MetricValue.bucket)
            .where(
                MetricValue.metric_definition_id == metric_id,
                MetricValue.scan_config_id.is_(None),
                MetricValue.bucket >= since,
            )
            .order_by(MetricValue.value.desc(), MetricValue.bucket.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    anchor = to_utc(best) if best is not None else ctx.now - 2 * _DAY
    session.add(
        PlannedEvent(
            project_id=ctx.project_id,
            label=DEMO_PLANNED_EVENT_LABEL,
            description=(
                "A spring promotion planned to lift revenue. A spike inside the "
                "window is expected: it is drawn, but raises no alert."
            ),
            starts_at=anchor - _PLANNED_EVENT_LEAD,
            ends_at=anchor - _PLANNED_EVENT_LEAD + _PLANNED_EVENT_SPAN,
            direction=AnomalyDirection.spike.value,
            scope_type=ChartAnnotationScopeType.metric.value,
            scope_ref=str(metric_id),
            created_by_user_id=ctx.created_by,
        )
    )
    await session.flush()
    await session.run_sync(retag_planned_anomalies, ctx.project_id)
    await session.commit()


async def note_newest_incident(session: AsyncSession, ctx: DemoContext) -> None:
    """Leave a note on the newest open incident in the inbox (a comment on it)."""
    state = (
        await session.execute(
            select(AlertCorrelationState)
            .where(AlertCorrelationState.project_id == ctx.project_id)
            .order_by(
                AlertCorrelationState.last_seen_at.desc().nullslast(),
                AlertCorrelationState.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if state is None:
        return
    state.note = DEMO_INCIDENT_NOTE
    await session.commit()


# -- helpers ----------------------------------------------------------------------


def _require[T](value: T | None, what: str) -> T:
    if value is None:
        msg = f"The demo recipe did not author its {what}"
        raise DemoPipelineError(msg)
    return value


async def _new_job(session: AsyncSession, config_id: uuid.UUID) -> uuid.UUID:
    job = ScanJob(scan_config_id=config_id, status=ScanJobStatus.pending.value)
    session.add(job)
    await session.commit()
    return job.id


async def _require_completed(session: AsyncSession, job_id: uuid.UUID, what: str) -> None:
    status, error = (
        await session.execute(
            select(ScanJob.status, ScanJob.error_message).where(ScanJob.id == job_id)
        )
    ).one()
    if status != ScanJobStatus.completed.value:
        msg = f"The demo {what} ended {status}: {error or 'no error recorded'}"
        raise DemoPipelineError(msg)


async def replay_summaries(
    session: AsyncSession, scan_config_id: uuid.UUID
) -> list[Mapping[str, object]]:
    """The progress summaries of the config's replay jobs (for the UI's percentage)."""
    rows = (
        await session.execute(
            select(ScanJob.result_summary).where(
                ScanJob.scan_config_id == scan_config_id,
                ScanJob.result_summary.is_not(None),
            )
        )
    ).scalars()
    return [
        summary
        for summary in rows
        if isinstance(summary, dict) and summary.get("replay_chunks_total") is not None
    ]


async def demo_scan_config_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await session.execute(
            select(ScanConfig.id)
            .where(ScanConfig.project_id == project_id)
            .order_by(ScanConfig.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
