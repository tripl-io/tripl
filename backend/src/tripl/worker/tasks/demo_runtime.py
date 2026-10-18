"""Demo runtime: keep demo projects' OWN history bounded.

A demo is advanced the way any project is: its scan config reads the shared
demo warehouse (:mod:`tripl.core.demo_warehouse`) and the ordinary scheduled
collection (``metrics.schedule.check_metrics_due``) collects it, subject to the
demo's idle pause and collection cooldown. Nothing here writes a metric.

What a live warehouse does not do for tripl is forget. The warehouse keeps
``DEMO_WAREHOUSE_RETENTION_DAYS`` and drops older day partitions; a demo
project would otherwise keep every collected hour forever, and a public demo
creates demos by the hundred. :func:`prune_demo_history` (daily) deletes a demo
project's OBSERVED rows that fall behind the same horizon, so tripl's own
database stays bounded by "demos x retention" rather than by age. The authored
plan (events, fields, variables, branches, comments, rules) is never touched.

"Observed" covers more than the series: everything a running demo writes per
incident or per visitor click and that nothing else ever deletes goes behind
the same horizon. That is catalog-metric anomalies, bell notifications, triage
that pins a pruned signal, release chart markers, scan previews and dry runs,
and the inbox rows of incidents whose every delivery is gone. What a person
decided and what still applies stays: an inbox decision or a mute in force,
and a chart marker someone drew.

The horizon is the warehouse's retention on purpose: a row older than it can
never be collected again, and keeping a demo's series longer than the warehouse
can reproduce it is what let an old resume window score a gap as zeros. The
collector clamps a demo window to the same horizon
(``metrics.demo_window.clamp_demo_window_start``).

Legacy demos
------------
A demo built by recipe 4 reads a per-demo ``synthetic`` source whose history
was seeded into tripl and advanced by a synthetic tick that no longer exists.
It is frozen: the scheduler skips it (``metrics.schedule``), the app shows a
"built by an older version" banner with Reset, and this prune still bounds it.

Tests monkey-patch ``_get_sync_session`` on this module's globals (the shared
sync-session pattern) and pass an explicit ``now`` (fake clock).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from tripl.config import settings
from tripl.core.bucketing import to_utc
from tripl.models.alert_correlation_state import AlertCorrelationState
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_owner_notification import AlertOwnerNotification
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.chart_annotation import ChartAnnotation
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import (
    AlertInboxStatus,
    ChartAnnotationSource,
    MetricScopeType,
    ProjectGenerationStatus,
    SignalTriageAction,
)
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.incident_summary import IncidentSummary
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.notification import Notification
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_dry_run_job import ScanDryRunJob
from tripl.models.scan_job import ScanJob
from tripl.models.scan_preview_job import ScanPreviewJob
from tripl.models.schema_drift import SchemaDrift
from tripl.models.signal_triage import SignalTriage
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session
from tripl.worker.utils.advisory_lock import release_advisory_lock, try_acquire_advisory_lock

logger = logging.getLogger(__name__)

# Stable log event name so a per-demo prune failure is greppable in aggregated
# logs even though the sweep swallows it to keep pruning the other demos.
DEMO_PRUNE_FAILED_EVENT = "demo.runtime.prune_failed"

# One sweep at a time. The key the retired ``advance_demos`` sweep held, so an
# old worker still running that sweep during an upgrade and a new one pruning
# never interleave. Distinct from the metrics dispatchers (…017 / …018), the
# digest flusher (…019) and the overdue-source sweep (…020).
_PRUNE_DEMOS_ADVISORY_LOCK_KEY = 4_021_968_021


def demo_retention_cutoff(now: datetime) -> datetime:
    """The oldest instant a demo keeps: the warehouse's retention, on the hour."""
    hour = to_utc(now).replace(minute=0, second=0, microsecond=0)
    return hour - timedelta(days=settings.demo_warehouse_retention_days)


@celery_app.task(name="tripl.worker.tasks.demo_runtime.prune_demo_history")  # type: ignore[untyped-decorator]
def prune_demo_history(now: datetime | None = None) -> dict[str, object]:
    """Delete every demo project's observed rows older than the retention horizon.

    Selector: ``is_demo AND generation_status='ready'`` (a demo still seeding is
    the seeder's; a failed shell is swept by ``demo_service``). Paused demos are
    pruned too: being idle is exactly when a demo's history has stopped growing
    and only ages. ``now`` is injectable for tests.
    """
    tick_now = to_utc(now) if now is not None else datetime.now(UTC)
    cutoff = demo_retention_cutoff(tick_now)
    session = _get_sync_session()
    lock_conn, acquired = try_acquire_advisory_lock(session, _PRUNE_DEMOS_ADVISORY_LOCK_KEY)
    if not acquired:
        logger.info("prune_demo_history: another sweep holds the lock; skipping")
        session.close()
        return {"running": True, "pruned": 0, "failed": 0}
    pruned = 0
    failed = 0
    try:
        candidates = session.execute(
            select(Project.id, Project.slug).where(
                Project.is_demo.is_(True),
                Project.generation_status == ProjectGenerationStatus.ready.value,
            )
        ).all()
        # End the read transaction: each demo is pruned in a transaction of its own.
        session.rollback()
        for project_id, slug in candidates:
            try:
                _acquire_project_xact_lock(session, project_id)
                _prune_retention(session, project_id, cutoff)
                session.commit()
                pruned += 1
            except Exception:
                logger.exception("%s slug=%s", DEMO_PRUNE_FAILED_EVENT, slug)
                session.rollback()
                failed += 1
        logger.info(
            "prune_demo_history: %d demos pruned before %s, %d failed",
            pruned,
            cutoff.isoformat(),
            failed,
        )
        return {"pruned": pruned, "failed": failed, "cutoff": cutoff.isoformat()}
    finally:
        release_advisory_lock(lock_conn, _PRUNE_DEMOS_ADVISORY_LOCK_KEY, name="prune_demo_history")
        session.close()


def _acquire_project_xact_lock(session: Session, project_id: uuid.UUID) -> None:
    """Take a per-project transaction advisory lock so two workers can't interleave.

    No-op off PostgreSQL (SQLite tests have no advisory locks). The lock
    auto-releases when the transaction commits/rolls back.
    """
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return
    key = int.from_bytes(project_id.bytes[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _prune_retention(session: Session, project_id: uuid.UUID, cutoff: datetime) -> None:
    """Delete one demo's observed rows older than ``cutoff`` (caps DB growth).

    Children are deleted before their parents explicitly rather than left to
    ``ON DELETE CASCADE``: SQLite (the unit suite) does not enforce foreign
    keys, and an orphaned child is a row a later read joins to nothing.
    """
    config_ids = list(
        session.execute(select(ScanConfig.id).where(ScanConfig.project_id == project_id))
        .scalars()
        .all()
    )
    if config_ids:
        old_anomalies = select(MetricAnomaly.id).where(
            MetricAnomaly.scan_config_id.in_(config_ids), MetricAnomaly.bucket < cutoff
        )
        session.execute(
            delete(MetricAnomalyAttribution).where(
                MetricAnomalyAttribution.anomaly_id.in_(old_anomalies)
            )
        )
        for model in (
            EventMetric,
            EventMetricBreakdown,
            CoverageMetric,
            MetricAnomaly,
            MetricBreakdownAnomaly,
            # The chart band every scored bucket writes.
            MetricBaseline,
            DistributionDrift,
        ):
            session.execute(
                delete(model).where(
                    model.scan_config_id.in_(config_ids),
                    model.bucket < cutoff,
                )
            )
        # A schema drift the warehouse still shows is re-detected (its
        # ``detected_at`` refreshes) on every catalog run, so only one the
        # warehouse stopped showing ages past the horizon.
        session.execute(
            delete(SchemaDrift).where(
                SchemaDrift.scan_config_id.in_(config_ids), SchemaDrift.detected_at < cutoff
            )
        )
        session.execute(
            delete(ScanJob).where(
                ScanJob.scan_config_id.in_(config_ids), ScanJob.created_at < cutoff
            )
        )

    old_deliveries = select(AlertDelivery.id).where(
        AlertDelivery.project_id == project_id, AlertDelivery.created_at < cutoff
    )
    session.execute(
        update(AlertRuleState)
        .where(AlertRuleState.last_notified_delivery_id.in_(old_deliveries))
        .values(last_notified_delivery_id=None)
    )
    session.execute(
        delete(AlertOwnerNotification).where(AlertOwnerNotification.delivery_id.in_(old_deliveries))
    )
    session.execute(
        delete(AlertDeliveryItem).where(AlertDeliveryItem.delivery_id.in_(old_deliveries))
    )
    session.execute(
        delete(AlertDelivery).where(
            AlertDelivery.project_id == project_id, AlertDelivery.created_at < cutoff
        )
    )

    # Catalog metric values for THIS project (both scan-scoped and NULL-scoped).
    metric_ids = select(MetricDefinition.id).where(MetricDefinition.project_id == project_id)
    session.execute(
        delete(MetricValueBreakdown).where(
            MetricValueBreakdown.metric_definition_id.in_(metric_ids),
            MetricValueBreakdown.bucket < cutoff,
        )
    )
    session.execute(
        delete(MetricValue).where(
            MetricValue.metric_definition_id.in_(metric_ids), MetricValue.bucket < cutoff
        )
    )
    _prune_metric_scope_anomalies(session, project_id, cutoff)
    _prune_inbox_leftovers(session, project_id, cutoff)
    _prune_signal_triage(session, project_id, cutoff)
    # A release marker is redrawn only while its activation sits inside the
    # loaded slice (``RELEASE_ANNOTATION_LOOKBACK``, shorter than the horizon),
    # so one older than the horizon is never drawn again and marks a bucket no
    # chart of the demo shows. Markers a person drew (``manual``) or a client
    # posted (``api``) are theirs and stay.
    session.execute(
        delete(ChartAnnotation).where(
            ChartAnnotation.project_id == project_id,
            ChartAnnotation.source == ChartAnnotationSource.release.value,
            ChartAnnotation.bucket < cutoff,
        )
    )
    # A visitor's previews and dry runs: answers read once. The maintenance
    # cleanup takes them after a week instance-wide; this keeps a demo bounded
    # by the horizon even where that retention is set longer.
    for preview_model in (ScanPreviewJob, ScanDryRunJob):
        session.execute(
            delete(preview_model).where(
                preview_model.project_id == project_id,
                func.coalesce(
                    preview_model.completed_at, preview_model.updated_at, preview_model.created_at
                )
                < cutoff,
            )
        )
    # The bell's history. A signal is announced again only within a few days
    # (``notification_producers.ALREADY_TOLD_WINDOW``) and throttled within
    # hours, so nothing reads a notification this old but the bell itself,
    # and what it points at (an anomaly, a delivery) is pruned above.
    session.execute(
        delete(Notification).where(
            Notification.project_id == project_id, Notification.created_at < cutoff
        )
    )


def _prune_metric_scope_anomalies(
    session: Session, project_id: uuid.UUID, cutoff: datetime
) -> None:
    """Catalog-metric anomalies older than the horizon, with their attributions.

    They hang off no scan config: ``detect`` writes them with a NULL
    ``scan_config_id``, ``scope_type='metric'`` and the metric's id as TEXT in
    ``scope_ref`` (the same gap ``project_service.purge_project_rows`` closes on
    a project delete), so the scan-scoped sweep above never reaches them. The
    ids are matched as Python strings rather than by a SQL CAST: SQLite stores
    a UUID as 32 hex digits, PostgreSQL's text form has dashes.
    """
    metric_refs = [
        str(metric_id)
        for metric_id in session.execute(
            select(MetricDefinition.id).where(MetricDefinition.project_id == project_id)
        ).scalars()
    ]
    if not metric_refs:
        return
    old_metric_anomalies = (
        MetricAnomaly.scan_config_id.is_(None),
        MetricAnomaly.scope_type == MetricScopeType.metric.value,
        MetricAnomaly.scope_ref.in_(metric_refs),
        MetricAnomaly.bucket < cutoff,
    )
    session.execute(
        delete(MetricAnomalyAttribution).where(
            MetricAnomalyAttribution.anomaly_id.in_(
                select(MetricAnomaly.id).where(*old_metric_anomalies)
            )
        )
    )
    session.execute(delete(MetricAnomaly).where(*old_metric_anomalies))


def _prune_inbox_leftovers(session: Session, project_id: uuid.UUID, cutoff: datetime) -> None:
    """Incident rows whose every delivery item the prune has already deleted.

    An incident is a virtual group of delivery items (``correlation_group_id``);
    its ``alert_correlation_states`` row holds the inbox decision and its
    ``incident_summaries`` row the cached summary. Once the group's last item is
    gone the inbox cannot list it and the summary endpoint 404s it, so both
    rows describe nothing. Runs after the delivery prune above.

    A correlation state is deleted only when it holds no decision in force.
    Its id is stable per (rule, scope, direction), not per incident, and
    ``dispatch._suppressed_correlation_group_ids`` enforces an acknowledged,
    resolved, false-positive or muted state with no time bound: a suppressed
    incident writes no deliveries, so "no items left" is exactly what a
    long-silenced one looks like, and deleting its state would page the
    visitor again. So only ``open`` states and mutes that lapsed before the
    horizon go; the rest are bounded by the plan's rules and scopes, not by age.
    ``last_seen_at`` is checked as well as ``updated_at`` because the firing
    path moves only the former.
    """
    live_groups = (
        select(AlertDeliveryItem.correlation_group_id)
        .join(AlertDelivery, AlertDelivery.id == AlertDeliveryItem.delivery_id)
        .where(
            AlertDelivery.project_id == project_id,
            AlertDeliveryItem.correlation_group_id.is_not(None),
        )
    )
    session.execute(
        delete(AlertCorrelationState).where(
            AlertCorrelationState.project_id == project_id,
            AlertCorrelationState.correlation_group_id.not_in(live_groups),
            AlertCorrelationState.updated_at < cutoff,
            or_(
                AlertCorrelationState.last_seen_at.is_(None),
                AlertCorrelationState.last_seen_at < cutoff,
            ),
            or_(
                AlertCorrelationState.status == AlertInboxStatus.open.value,
                and_(
                    AlertCorrelationState.status == AlertInboxStatus.muted.value,
                    AlertCorrelationState.muted_until.is_not(None),
                    AlertCorrelationState.muted_until < cutoff,
                ),
            ),
        )
    )
    session.execute(
        delete(IncidentSummary).where(
            IncidentSummary.project_id == project_id,
            IncidentSummary.correlation_group_id.not_in(live_groups),
            IncidentSummary.updated_at < cutoff,
        )
    )


def _prune_signal_triage(session: Session, project_id: uuid.UUID, cutoff: datetime) -> None:
    """Triage that answers a signal the prune has deleted, and long-lapsed mutes.

    An acknowledgement or a verdict pins ONE signal by its ``bucket``; the
    anomaly at that bucket is gone once it is older than the horizon, so the
    row answers nothing. Its ``expected`` chart marker is the visitor's own and
    stays (the FK sets ``annotation_id`` NULL on the way). A mute carries no
    bucket and covers its scope until ``muted_until``: one with no end, or one
    still to lapse, is a decision in force and stays; one that lapsed before
    the horizon hides nothing and goes.
    """
    session.execute(
        delete(SignalTriage).where(
            SignalTriage.project_id == project_id,
            or_(
                and_(SignalTriage.bucket.is_not(None), SignalTriage.bucket < cutoff),
                and_(
                    SignalTriage.action == SignalTriageAction.muted.value,
                    SignalTriage.muted_until.is_not(None),
                    SignalTriage.muted_until < cutoff,
                ),
            ),
        )
    )
