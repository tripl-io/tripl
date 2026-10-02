"""Monitoring builder: signals derived from the seeded series.

Seeds the project anomaly gate, the ``MetricAnomaly`` rows produced by RUNNING the
real ``detect_anomalies`` over the stored series (not hand-written z-scores), the
``DistributionDrift`` ladder computed by the real ``compute_psi``, and a couple of
``SchemaDrift`` findings. Because detection is derived from the deterministic
warehouse series, every signal is reproducible for a given ``(clock, seed)``.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl.core.analyzers.anomaly_detector import (
    SCOPE_EVENT,
    SCOPE_EVENT_TYPE,
    SCOPE_PROJECT_TOTAL,
    DetectedAnomaly,
    SeriesPoint,
    detect_anomalies,
)
from tripl.core.analyzers.distribution_drift import DistributionDriftResult, compute_psi
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.scan_config import ScanConfig
from tripl.models.schema_drift import SchemaDrift
from tripl.services.demo import noise
from tripl.services.demo.builders.warehouse import SPIKE_EVENT_NAME
from tripl.services.demo.scenario import DemoContext


async def build_monitoring(session: AsyncSession, ctx: DemoContext) -> None:
    await _build_anomaly_settings(session, ctx)
    await _build_anomalies(session, ctx)
    await _build_attributions(session, ctx)
    await _build_distribution_drift(session, ctx)
    await _build_schema_drift(session, ctx)


async def _build_anomaly_settings(session: AsyncSession, ctx: DemoContext) -> None:
    # ``detect.py`` bails out (and purges any anomalies) when this row is absent or
    # disabled, so the scan's advertised anomaly_detection_enabled flag must be
    # backed by an enabled settings row. Values mirror DEMO_ANOMALY_SETTINGS so the
    # seeded anomalies are exactly what the worker would produce.
    settings = noise.DEMO_ANOMALY_SETTINGS
    session.add(
        ProjectAnomalySettings(
            project_id=ctx.project_id,
            anomaly_detection_enabled=True,
            detect_project_total=True,
            detect_event_types=True,
            detect_events=True,
            detect_metrics=True,
            baseline_window_buckets=settings.baseline_window_buckets,
            min_history_buckets=settings.min_history_buckets,
            sigma_threshold=settings.sigma_threshold,
            min_expected_count=settings.min_expected_count,
            # Nothing to settle: the synthetic warehouse serves every hour
            # complete. The default two-hour allowance withheld the newest bucket,
            # where the spike is seeded, so the first scheduled collection purged
            # the demo's seeded anomalies and re-emitted none.
            anomaly_ingestion_settling_minutes=0,
        )
    )
    await session.flush()


@dataclass(frozen=True)
class _AnomalyScope:
    """One detector scope: its series plus the ``MetricAnomaly`` identity columns."""

    series: dict[datetime, int]
    scope_type: str
    scope_ref: str
    event_id: uuid.UUID | None
    event_type_id: uuid.UUID | None


def _detect_scope_anomalies(
    scopes: list[_AnomalyScope], *, evaluation_start: datetime, evaluation_end: datetime
) -> list[list[DetectedAnomaly]]:
    """Run the real detector over every scope. Pure CPU, no session.

    Each hourly series is long enough to trigger the detector's robust MSTL fit,
    about 1.4 s per scope, so three scopes held the API event loop for ~4 s on
    every demo create or reset and stalled every other request on that worker.
    The caller runs this in a thread and only adds the rows on the loop
    (tripl-0zpq.251).
    """
    results: list[list[DetectedAnomaly]] = []
    for scope in scopes:
        points = [
            SeriesPoint(bucket=bucket, count=count)
            for bucket, count in sorted(scope.series.items())
        ]
        results.append(
            detect_anomalies(
                points,
                interval=timedelta(hours=1),
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                settings=noise.DEMO_ANOMALY_SETTINGS,
            ).anomalies
        )
    return results


async def _build_anomalies(session: AsyncSession, ctx: DemoContext) -> None:
    evaluation_start = ctx.now - timedelta(hours=noise.DEMO_EVAL_WINDOW_HOURS)
    evaluation_end = ctx.now
    screen_view_type_id = ctx.event_type_ids["screen_view"]
    spike_event_id = ctx.event_ids[SPIKE_EVENT_NAME]

    # Event-type scope: the screen_view aggregate (Home rolls up into it).
    screen_view_series = {
        bucket: count
        for (et_id, bucket), count in ctx.type_bucket_counts.items()
        if et_id == screen_view_type_id
    }
    # Project-total scope: sum of every type aggregate per bucket (scope_ref is the
    # scan_config_id, per metrics_service).
    project_total_series: dict[datetime, int] = {}
    for (_et_id, bucket), count in ctx.type_bucket_counts.items():
        project_total_series[bucket] = project_total_series.get(bucket, 0) + count

    scopes = [
        # Event scope: Home Screen View's own series.
        _AnomalyScope(
            series=dict(ctx.home_series),
            scope_type=SCOPE_EVENT,
            scope_ref=str(spike_event_id),
            event_id=spike_event_id,
            event_type_id=None,
        ),
        _AnomalyScope(
            series=screen_view_series,
            scope_type=SCOPE_EVENT_TYPE,
            scope_ref=str(screen_view_type_id),
            event_id=None,
            event_type_id=screen_view_type_id,
        ),
        _AnomalyScope(
            series=project_total_series,
            scope_type=SCOPE_PROJECT_TOTAL,
            scope_ref=str(ctx.scan_config_id),
            event_id=None,
            event_type_id=None,
        ),
    ]
    detected = await asyncio.to_thread(
        _detect_scope_anomalies,
        scopes,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
    )
    for scope, anomalies in zip(scopes, detected, strict=True):
        for anomaly in anomalies:
            session.add(
                MetricAnomaly(
                    scan_config_id=ctx.scan_config_id,
                    scope_type=scope.scope_type,
                    scope_ref=scope.scope_ref,
                    event_id=scope.event_id,
                    event_type_id=scope.event_type_id,
                    bucket=anomaly.bucket,
                    actual_count=anomaly.actual_count,
                    expected_count=anomaly.expected_count,
                    stddev=anomaly.stddev,
                    z_score=anomaly.z_score,
                    direction=anomaly.direction,
                )
            )
    await session.flush()


async def _build_attributions(session: AsyncSession, ctx: DemoContext) -> None:
    """Why did it change, for the seeded anomalies (F02, #255).

    Runs the worker's own pass over the seeded breakdown series, so the demo's
    split is exactly what a scan would store. Sync code on the async session's
    connection, like every other worker helper reused here.
    """
    # Imported here: the worker package pulls in the Celery task graph, which
    # the demo seeder should not load at import time.
    from tripl.worker.tasks.metrics.attribution import recompute_anomaly_attributions

    evaluation_start = ctx.now - timedelta(hours=noise.DEMO_EVAL_WINDOW_HOURS)
    evaluation_end = ctx.now

    def _run(sync_session: Session) -> None:
        config = sync_session.get(ScanConfig, ctx.scan_config_id)
        if config is None:
            return
        recompute_anomaly_attributions(
            sync_session,
            config,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            now=ctx.now,
        )

    await session.run_sync(_run)
    await session.flush()


def _compute_drift_ladder(now: datetime) -> list[tuple[datetime, DistributionDriftResult]]:
    """One ``(bucket, PSI)`` per day of the drift span. Pure CPU, no session.

    Runs in a thread beside the detector for the same reason (tripl-0zpq.251).
    """
    baseline_counts = noise.shares_to_counts(
        noise.platform_shares(0.0), noise.DEMO_DRIFT_DAILY_TOTAL
    )
    ladder: list[tuple[datetime, DistributionDriftResult]] = []
    for days_back in range(noise.DEMO_DRIFT_SPAN_DAYS, 0, -1):
        drift_bucket = (now - timedelta(days=days_back)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        current_shares = noise.platform_shares(noise.drift_span_progress(float(days_back)))
        current_counts = noise.shares_to_counts(current_shares, noise.DEMO_DRIFT_DAILY_TOTAL)
        ladder.append((drift_bucket, compute_psi(baseline_counts, current_counts)))
    return ladder


async def _build_distribution_drift(session: AsyncSession, ctx: DemoContext) -> None:
    """One daily DistributionDrift row across the drift span, PSI from real
    ``compute_psi`` over the window-start baseline vs each day's drifted mix."""
    screen_view_type_id = ctx.event_type_ids["screen_view"]
    for drift_bucket, result in await asyncio.to_thread(_compute_drift_ladder, ctx.now):
        session.add(
            DistributionDrift(
                scan_config_id=ctx.scan_config_id,
                event_type_id=screen_view_type_id,
                field_name="platform",
                bucket=drift_bucket,
                psi=result.psi,
                band=result.band,
                baseline_total=result.baseline_total,
                current_total=result.current_total,
                top_movers=[
                    {
                        "value": mover.value,
                        "baseline_share": mover.baseline_share,
                        "current_share": mover.current_share,
                        "contribution": mover.contribution,
                    }
                    for mover in result.top_movers
                ],
            )
        )
    await session.flush()


async def _build_schema_drift(session: AsyncSession, ctx: DemoContext) -> None:
    purchase_type_id = ctx.event_type_ids["purchase"]
    session.add(
        SchemaDrift(
            event_type_id=purchase_type_id,
            scan_config_id=ctx.scan_config_id,
            field_name="amount",
            drift_type="type_changed",
            observed_type="String",
            declared_type="number",
            sample_value="9.99",
            status="open",
        )
    )
    session.add(
        SchemaDrift(
            event_type_id=purchase_type_id,
            scan_config_id=ctx.scan_config_id,
            field_name="currency",
            drift_type="enum_violation",
            observed_type=None,
            declared_type=None,
            sample_value="GBP",
            status="open",
        )
    )
    await session.flush()
