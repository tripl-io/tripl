"""Governance builder: scan history, coverage, and reconciliation outputs.

Seeds the observable governance surface that a real scan/metrics pipeline would
leave behind, coherent with the warehouse volume seeded by the warehouse builder:

- ``ScanJob`` history — a run cadence of completed jobs with realistic
  ``result_summary`` and timing, plus one older, safely-explained transient
  failure. The latest job is successful, so the project summary reads healthy.
- ``CoverageMetric`` — per-bucket plan coverage of scanned volume. ``matched``
  is the volume attributed to plan events (the seeded per-type series); ``total``
  adds a small unmatched tail, so coverage reconciles with scanned volume and
  the unmatched share equals what the shadow candidates represent.
- ``ShadowEventCandidate`` — warehouse identities with no matching plan event.
  Names are deliberately OUTSIDE the authored plan so accepting one never
  collides with an existing source identity.
- One age-valid dead-event example (a plan event whose warehouse volume dried up
  long enough ago to surface in the dead-events review).

All rows are synthetic and deterministic for a given ``(clock, seed)``. No real
scan runs here — this is the "faithfully orchestrated" initial history; the live
Run now / Preview / Replay / scheduled paths run the real pipeline over the
synthetic source on demand.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.bucketing import to_utc
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.event import Event
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.scan_job import ScanJob, ScanJobStatus
from tripl.models.shadow_event_candidate import (
    SHADOW_STATUS_DISMISSED,
    SHADOW_STATUS_NEW,
    ShadowEventCandidate,
)
from tripl.services.demo import noise
from tripl.services.demo.builders.warehouse import (
    DEAD_EVENT_AGE_DAYS,
    DEAD_EVENT_NAME,
    SCAN_COLUMNS,
)
from tripl.services.demo.scenario import DemoContext

# Coverage rate: plan events account for ~94% of scanned volume; the ~6% tail is
# unmatched (the shadow candidates below). Public because the demo runtime tick
# appends coverage at the same rate, so appended rows reconcile with these.
COVERAGE_MATCH_RATE = 0.94
# Only the recent slice gets coverage rows (the reconciliation view is windowed);
# keeps the seed bounded while still reconciling with the volume chart.
_COVERAGE_DAYS = 14

# How long ago each backfilled completed run started (one per scan interval).
_COMPLETED_RUN_OFFSETS = (timedelta(hours=3), timedelta(hours=2), timedelta(hours=1))
# Backfilled run duration: a floor plus time proportional to the rows scanned,
# with a small deterministic jitter, so three consecutive runs do not all report
# an identical wall time.
_RUN_DURATION_FLOOR = timedelta(seconds=2.4)
_RUN_SECONDS_PER_1K_ROWS = 0.09
_RUN_JITTER_SECONDS = 2.5


def _run_duration(ctx: DemoContext, started: datetime, rows: int) -> timedelta:
    """Deterministic per-run wall time in the same band a real Run now lands in."""
    jitter = (noise.derive_seed(ctx.seed, f"scan_run:{started.isoformat()}") % 1000) / 1000.0
    seconds = (
        _RUN_DURATION_FLOOR.total_seconds()
        + rows / 1000.0 * _RUN_SECONDS_PER_1K_ROWS
        + jitter * _RUN_JITTER_SECONDS
    )
    return timedelta(seconds=round(seconds, 1))


async def build_governance(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_scan_history(session, ctx)
    await _build_coverage(session, ctx)
    await _build_shadow_candidates(session, ctx)
    await _build_dead_event(session, ctx)


def _hourly_scanned_rows(ctx: DemoContext, window_from: datetime) -> int:
    """Rows the seeded warehouse actually holds for one hourly scan window.

    The per-type series IS the seeded volume, so summing the window's buckets
    makes a backfilled run report the same order of magnitude a real Run now
    reports against the same synthetic table.
    """
    bucket = window_from.replace(minute=0, second=0, microsecond=0)
    return sum(
        count for (_et_id, stored), count in ctx.type_bucket_counts.items() if stored == bucket
    )


async def _stored_points_by_hour(
    session: AsyncSession, ctx: DemoContext, buckets: list[datetime]
) -> dict[datetime, dict[str, int]]:
    """The metric points the warehouse builder stored for each hour.

    Counted from the stored rows, under the four counters a metrics collection
    reports its points in (``metrics.tasks``' result summary): per-event and
    per-type volume rows, and their breakdown rows.
    """
    points = {
        to_utc(bucket): {
            "event_metrics": 0,
            "type_metrics": 0,
            "breakdown_event_metrics": 0,
            "breakdown_type_metrics": 0,
        }
        for bucket in buckets
    }
    for model, event_key, type_key in (
        (EventMetric, "event_metrics", "type_metrics"),
        (EventMetricBreakdown, "breakdown_event_metrics", "breakdown_type_metrics"),
    ):
        rows = await session.execute(
            # ``count(event_id)`` counts the per-event rows; the rest of the
            # hour's rows are the per-type rollups, which carry no event.
            select(model.bucket, func.count(model.event_id), func.count())
            .where(model.scan_config_id == ctx.scan_config_id, model.bucket.in_(buckets))
            .group_by(model.bucket)
        )
        for bucket, event_rows, all_rows in rows:
            counters = points[to_utc(bucket)]
            counters[event_key] = event_rows
            counters[type_key] = all_rows - event_rows
    return points


async def _build_scan_history(session: AsyncSession, ctx: DemoContext) -> None:
    """A realistic run cadence: older completed runs, one transient failure that
    recovered, and a fresh successful run (the latest job)."""
    matched_events = len(ctx.event_ids)

    def _summary(
        window_from: datetime, window_to: datetime, rows: int, points: dict[str, int]
    ) -> dict[str, object]:
        return {
            "events_created": 0,
            "events_skipped": 0,
            "events_grouped": len(ctx.event_type_ids),
            "events_merged": matched_events,
            "variables_created": 0,
            "columns_analyzed": len(SCAN_COLUMNS),
            # Warehouse rows, so the counter a real catalog run puts them in.
            # ``scan_rows_processed`` is the distinct column combinations its
            # GROUP BY returned, which the scan page prints as "combos": these
            # runs read "31,402 combos" beside a Run now's "28,160 rows".
            "catalog_rows_scanned": rows,
            "scan_window_from": window_from.isoformat(),
            "scan_window_to": window_to.isoformat(),
            # The hour's stored metric points, so a monitoring scan's history
            # shows the series it backs. Deliberately no ``mode``: the
            # dispatcher takes its watermark and the demo's collection cooldown
            # from its own ``metrics_collection`` runs only, and a seeded run
            # stamped as one would move both.
            **points,
            "details": [],
        }

    # Completed runs at the scan interval, newest last so the latest job is fresh.
    # ``created_at`` is set explicitly to the run time: the "latest job" rollups
    # rank by created_at, so without this every job would share the one
    # provisioning timestamp and the failed run could win the tiebreak.
    #
    # Each run reports ITS OWN hour: window, row count and duration are all
    # derived from the run's own clock and the volume the seeded warehouse holds
    # for that hour. They used to be constants, so three
    # consecutive runs claimed the same future window, byte-identical millions of
    # rows and an identical 42.0s — next to a real Run now reporting ~30K rows.
    runs: list[tuple[datetime, datetime, datetime]] = []
    for offset in _COMPLETED_RUN_OFFSETS:
        started = ctx.now - offset
        window_to = started.replace(minute=0, second=0, microsecond=0)
        runs.append((started, window_to - timedelta(hours=1), window_to))
    points_by_hour = await _stored_points_by_hour(
        session, ctx, [window_from for _started, window_from, _window_to in runs]
    )
    for started, window_from, window_to in runs:
        rows = _hourly_scanned_rows(ctx, window_from)
        session.add(
            ScanJob(
                scan_config_id=ctx.scan_config_id,
                status=ScanJobStatus.completed.value,
                started_at=started,
                completed_at=started + _run_duration(ctx, started, rows),
                result_summary=_summary(
                    window_from, window_to, rows, points_by_hour[to_utc(window_from)]
                ),
                created_at=started,
            )
        )

    # One older transient failure that later recovered — safe, non-leaky message.
    # Its old created_at keeps it behind the recent completed runs in the ranking,
    # so the config's LATEST job is the successful one and the project reads healthy.
    failed_started = ctx.now - timedelta(days=2, hours=4)
    session.add(
        ScanJob(
            scan_config_id=ctx.scan_config_id,
            status=ScanJobStatus.failed.value,
            started_at=failed_started,
            completed_at=failed_started + timedelta(seconds=61),
            error_message="Synthetic warehouse read timed out; the next scheduled run recovered.",
            created_at=failed_started,
        )
    )
    await session.flush()


async def _build_coverage(session: AsyncSession, ctx: DemoContext) -> None:
    """Per-bucket coverage that reconciles with the seeded per-type volume."""
    cutoff = ctx.now - timedelta(days=_COVERAGE_DAYS)
    # Aggregate the per-type series to a per-bucket matched total.
    matched_by_bucket: dict[datetime, int] = {}
    for (_et_id, bucket), count in ctx.type_bucket_counts.items():
        if bucket < cutoff:
            continue
        matched_by_bucket[bucket] = matched_by_bucket.get(bucket, 0) + count

    for bucket, matched in matched_by_bucket.items():
        total = max(matched, round(matched / COVERAGE_MATCH_RATE))
        session.add(
            CoverageMetric(
                scan_config_id=ctx.scan_config_id,
                bucket=bucket,
                total_count=total,
                matched_count=matched,
            )
        )
    await session.flush()


# Sample rows for the open shadow candidate, in the warehouse's own columns
# (``SCAN_COLUMNS`` minus the time column the collector leaves out), with the
# synthetic adapter's app versions.
_HEARTBEAT_SAMPLES: list[dict[str, str]] = [
    {
        "event_type": "click",
        "event_name": "app_heartbeat_v1",
        "platform": "ios",
        "app_version": "1.4.0",
    },
    {
        "event_type": "click",
        "event_name": "app_heartbeat_v1",
        "platform": "android",
        "app_version": "1.4.0",
    },
    {
        "event_type": "click",
        "event_name": "app_heartbeat_v1",
        "platform": "android",
        "app_version": "1.3.0",
    },
]


async def _build_shadow_candidates(session: AsyncSession, ctx: DemoContext) -> None:
    """Warehouse identities with no plan event. Names are OUTSIDE the authored
    plan so accepting one cannot collide with an existing source identity."""
    click_type_id = ctx.event_type_ids.get("click")
    recent = ctx.now - timedelta(hours=2)
    week_ago = ctx.now - timedelta(days=7)

    session.add(
        ShadowEventCandidate(
            project_id=ctx.project_id,
            scan_config_id=ctx.scan_config_id,
            # Keep the coached one-click Accept path valid: unlike screen_view,
            # the click type has no required field whose value the warehouse
            # candidate cannot supply.
            event_type_id=click_type_id,
            event_name="app_heartbeat_v1",
            observed_count=1840,
            # What the collector would have kept, so the demo's inbox
            # shows its "Show N samples" rather than an empty toggle.
            sample_properties=_HEARTBEAT_SAMPLES,
            first_seen_at=week_ago,
            last_seen_at=recent,
            status=SHADOW_STATUS_NEW,
        )
    )
    session.add(
        ShadowEventCandidate(
            project_id=ctx.project_id,
            scan_config_id=ctx.scan_config_id,
            event_type_id=None,
            event_name="legacy_deeplink_open",
            observed_count=420,
            first_seen_at=week_ago,
            last_seen_at=week_ago + timedelta(days=1),
            status=SHADOW_STATUS_DISMISSED,
            # A dismissal recording no resolver is a state the API cannot
            # produce: reconciliation_service sets status, resolved_by and
            # resolved_at in one step. It also gives the audit builder the
            # instant to file its ``shadow_event.dismiss`` row at, so the row and
            # the candidate name one moment rather than two.
            # Dated after the identity was last seen — you cannot wave away
            # traffic before it arrives.
            resolved_by=ctx.created_by,
            resolved_at=week_ago + timedelta(days=2),
        )
    )
    await session.flush()


async def _build_dead_event(session: AsyncSession, ctx: DemoContext) -> None:
    """Age out one authored event's warehouse volume so it surfaces as dead."""
    event_id = ctx.event_ids.get(DEAD_EVENT_NAME)
    if event_id is None:
        return
    event = await session.get(Event, event_id)
    if event is not None:
        last_seen = ctx.now - timedelta(days=DEAD_EVENT_AGE_DAYS)
        event.last_seen_at = last_seen
        # ``list_dead_events`` gates only NEVER-seen events on ``created_at``, so
        # no grace-period backdating is needed to surface this one. The row must
        # still be self-consistent — an event cannot be seen before it was
        # written down — so first-seen moves back only as far as the last
        # sighting, not a further 30 days ahead of every other event.
        # ``created_at`` may come back from the database, and
        # SQLite drops the offset, so compare through ``to_utc`` — a naive value
        # cannot be compared with the aware ``last_seen`` at all.
        if to_utc(event.created_at) > last_seen:
            event.created_at = last_seen
    await session.flush()
