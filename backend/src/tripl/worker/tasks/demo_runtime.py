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

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from tripl.config import settings
from tripl.core.bucketing import to_utc
from tripl.models.alert_delivery import AlertDelivery
from tripl.models.alert_delivery_item import AlertDeliveryItem
from tripl.models.alert_owner_notification import AlertOwnerNotification
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.coverage_metric import CoverageMetric
from tripl.models.distribution_drift import DistributionDrift
from tripl.models.domain_enums import ProjectGenerationStatus
from tripl.models.event_metric import EventMetric
from tripl.models.event_metric_breakdown import EventMetricBreakdown
from tripl.models.metric_anomaly import MetricAnomaly
from tripl.models.metric_anomaly_attribution import MetricAnomalyAttribution
from tripl.models.metric_baseline import MetricBaseline
from tripl.models.metric_breakdown_anomaly import MetricBreakdownAnomaly
from tripl.models.metric_definition import MetricDefinition
from tripl.models.metric_value import MetricValue
from tripl.models.metric_value_breakdown import MetricValueBreakdown
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.models.scan_job import ScanJob
from tripl.models.schema_drift import SchemaDrift
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
