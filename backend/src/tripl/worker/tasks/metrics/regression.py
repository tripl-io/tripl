"""Release-regression recalculation.

Loads the per-version series stored in ``EventMetricBreakdown`` for a scan's
``app_version_column``, runs the pure release-regression model, and replaces the
scan's ``ReleaseRegression`` rows. Inert (and clears any stale rows) when the
scan has no version column configured.

The pass is anchored on the newest STORED bucket, not on the caller's collection
window, so a one-off replay of a past window refreshes the current release's
verdict instead of replacing it with whichever release was newest back then.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from tripl.core.analyzers.anomaly_detector import SCOPE_EVENT, SCOPE_EVENT_TYPE
from tripl.core.analyzers.release_regression import (
    RegressionSettings,
    ReleaseRegressionReport,
    ReleaseRegressionResult,
    detect_release_regressions_by_scope,
)
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.models.release_regression import ReleaseComparability, ReleaseRegression
from tripl.models.scan_config import ScanConfig
from tripl.services.version_activation import compile_prerelease_pattern, resolve_share_min

logger = logging.getLogger(__name__)


def _build_regression_settings(session: Session, config: ScanConfig) -> RegressionSettings:
    """Reuse the project's ``sigma_threshold`` for the regression significance
    test, and honor the scan's ``app_version_active_share_min`` and
    ``app_version_prerelease_pattern`` overrides so the analyzer decides
    eligibility on the same inputs the app-version series does; everything else
    stays at the model defaults."""
    share_min = resolve_share_min(config.app_version_active_share_min)
    prerelease_pattern = compile_prerelease_pattern(config.app_version_prerelease_pattern)
    project_settings = session.execute(
        select(ProjectAnomalySettings).where(ProjectAnomalySettings.project_id == config.project_id)
    ).scalar_one_or_none()
    if project_settings is None:
        return RegressionSettings(active_share_min=share_min, prerelease_pattern=prerelease_pattern)
    return RegressionSettings(
        sigma=project_settings.sigma_threshold,
        active_share_min=share_min,
        prerelease_pattern=prerelease_pattern,
    )


def _recalculate_release_regressions(session: Session, config: ScanConfig) -> int:
    """Recompute the scan's release-regression verdict for its CURRENT release.

    Takes no window on purpose. The anchor is the newest bucket this scan has
    STORED for its version column, never the window the caller happened to
    collect: a one-off replay states an arbitrary past window (the window
    resolver refuses only a window reaching into the current incomplete
    interval), and anchoring on it made the analyzer judge whichever
    release was newest *inside that window* — then persisted that verdict over
    the live one, which ``_get_active_release_regression_candidates`` reads as
    "current" one call later and turns into alerts attributed to a months-old
    release. Replay's own chunk deletes are window-bounded, so
    ``max(bucket)`` for the scan is still the true latest even mid-replay.

    A replay that backfills buckets INSIDE the live comparison window must still
    refresh the current verdict, which is why this is anchored rather than
    skipped on the replay path.
    """
    # Regressions describe the current latest release, so we always recompute
    # from scratch. Clearing first also makes the no-op paths self-healing. The
    # comparability verdicts go with them: a stale "not comparable yet" outliving
    # the pass that produced it is the same lie in the other direction.
    session.execute(delete(ReleaseRegression).where(ReleaseRegression.scan_config_id == config.id))
    session.execute(
        delete(ReleaseComparability).where(ReleaseComparability.scan_config_id == config.id)
    )

    if not config.app_version_column or not config.interval:
        session.flush()
        return 0

    settings = _build_regression_settings(session, config)
    latest_bucket = session.execute(
        select(sa_func.max(EventMetricBreakdown.bucket)).where(
            EventMetricBreakdown.scan_config_id == config.id,
            EventMetricBreakdown.breakdown_column == config.app_version_column,
        )
    ).scalar()
    if latest_bucket is None:
        session.flush()
        return 0
    load_from = latest_bucket - timedelta(days=settings.window_days * 2)

    rows = session.execute(
        select(
            EventMetricBreakdown.event_id,
            EventMetricBreakdown.event_type_id,
            EventMetricBreakdown.breakdown_value,
            EventMetricBreakdown.is_other,
            EventMetricBreakdown.bucket,
            EventMetricBreakdown.count,
        ).where(
            EventMetricBreakdown.scan_config_id == config.id,
            EventMetricBreakdown.breakdown_column == config.app_version_column,
            EventMetricBreakdown.bucket >= load_from,
            # A no-op by construction — ``latest_bucket`` is the max of this very
            # predicate's rows — kept so the loaded slice reads as what it is:
            # ``[anchor - 2 * window_days, anchor]``.
            EventMetricBreakdown.bucket <= latest_bucket,
        )
    ).all()
    if not rows:
        session.flush()
        return 0

    # Event-level rows are the project-wide volume proxy; "Other" is included in
    # total traffic (the maturity denominator) but never as a regression target.
    release_total_by_bucket: dict[str, dict[datetime, int]] = {}
    all_traffic_by_bucket: dict[datetime, int] = {}
    event_counts: dict[str, dict[str, dict[datetime, int]]] = {}
    type_counts: dict[str, dict[str, dict[datetime, int]]] = {}
    event_id_by_ref: dict[str, uuid.UUID] = {}
    type_id_by_ref: dict[str, uuid.UUID] = {}

    for event_id, event_type_id, version, is_other, bucket, count in rows:
        count = int(count)
        if event_id is not None:
            all_traffic_by_bucket[bucket] = all_traffic_by_bucket.get(bucket, 0) + count
            if not is_other:
                version_buckets = release_total_by_bucket.setdefault(version, {})
                version_buckets[bucket] = version_buckets.get(bucket, 0) + count
                ref = str(event_id)
                event_id_by_ref[ref] = event_id
                scope_buckets = event_counts.setdefault(ref, {}).setdefault(version, {})
                scope_buckets[bucket] = scope_buckets.get(bucket, 0) + count
        elif event_type_id is not None and not is_other:
            ref = str(event_type_id)
            type_id_by_ref[ref] = event_type_id
            scope_buckets = type_counts.setdefault(ref, {}).setdefault(version, {})
            scope_buckets[bucket] = scope_buckets.get(bucket, 0) + count

    def _persist(results: list[ReleaseRegressionResult], *, scope_type: str) -> int:
        for result in results:
            session.add(
                ReleaseRegression(
                    id=uuid.uuid4(),
                    scan_config_id=config.id,
                    scope_type=scope_type,
                    scope_ref=result.scope_ref,
                    event_id=(
                        event_id_by_ref[result.scope_ref] if scope_type == SCOPE_EVENT else None
                    ),
                    event_type_id=(
                        type_id_by_ref[result.scope_ref] if scope_type == SCOPE_EVENT_TYPE else None
                    ),
                    app_version_column=config.app_version_column,
                    version=result.version,
                    previous_version=result.previous_version,
                    kind=result.kind,
                    observed_count=result.observed_count,
                    expected_count=result.expected_count,
                    ratio=result.ratio,
                    share_prev=result.share_prev,
                    share_new=result.share_new,
                    release_share=result.release_share,
                    window_from=result.window_from,
                    window_to=result.window_to,
                )
            )
        return len(results)

    def _persist_verdict(report: ReleaseRegressionReport, *, scope_type: str) -> None:
        """Record the verdict, comparable or not.

        Written on every pass, not only the suppressed ones: a reader can only
        tell "this release is fine" from "this release cannot be judged yet" if
        the affirmative is also on the record. Without a row the API cannot
        distinguish either from "detection has never run for this scan".

        The per-scope rows deliberately carry the same verdict and the same
        ``emerging_share``: comparability is a property of the release, and the
        share stored is the value that decided it, not the score this partition
        happened to contribute.
        """
        session.add(
            ReleaseComparability(
                id=uuid.uuid4(),
                scan_config_id=config.id,
                scope_type=scope_type,
                app_version_column=config.app_version_column,
                version=report.version,
                previous_version=report.previous_version,
                comparable=report.comparable,
                reason=report.reason,
                emerging_share=report.emerging_share,
                max_emerging_share=settings.max_emerging_share,
            )
        )

    # Both partitions go into one call, which holds them to a single
    # comparability verdict. Event types are a coarsening of
    # events, so the two are estimates of the same population question on the
    # same release; judging each on its own let one persist composition-
    # normalized rows that the other had already ruled untrustworthy.
    reports = detect_release_regressions_by_scope(
        release_total_by_bucket=release_total_by_bucket,
        all_traffic_by_bucket=all_traffic_by_bucket,
        scope_counts_by_scope_type={SCOPE_EVENT: event_counts, SCOPE_EVENT_TYPE: type_counts},
        latest_bucket=latest_bucket,
        settings=settings,
    )

    detected = 0
    for scope_type, report in reports.items():
        if not report.comparable:
            # Not a clean bill of health: either the release's population is not
            # yet comparable to the baseline's — so the composition-normalized
            # findings are withheld — or no comparison happened at all. Say so,
            # rather than writing nothing and leaving zero rows to be read as
            # "no regressions". ``report.results`` still holds any
            # silent-event rows, which no population difference explains — those
            # are persisted. The share logged is the release-level one both
            # scopes were judged on, so the two lines agree by construction.
            logger.info(
                "release regression comparison withheld: scan=%s scope=%s reason=%s %s vs %s "
                "emerging_share=%.3f (bound %.3f); keeping %d missing-event row(s)",
                config.id,
                scope_type,
                report.reason,
                report.version,
                report.previous_version,
                report.emerging_share,
                settings.max_emerging_share,
                len(report.results),
            )
        _persist_verdict(report, scope_type=scope_type)
        detected += _persist(report.results, scope_type=scope_type)
    session.flush()
    return detected
