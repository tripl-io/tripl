"""Periodic "scan overdue" sweep (issue #269).

A late warehouse load is reported by the per-run dispatch: the scan still
collects, ``_prepare_alert_deliveries`` sees the stale ``last_event_at`` and
emits one ``source_freshness`` candidate. A scan that STOPPED collecting has no
run to report it — nothing calls the dispatch — so this beat task walks every
scheduled scan config on a timer, and for each overdue one pushes a
``source_freshness`` candidate through the same rule matcher, the same
``AlertRuleState`` send gate and the same delivery minting the dispatch uses.

What this sweep emits, and what it leaves alone:

* ``overdue`` only. ``late`` stays with the per-run dispatch, and both land on
  the ONE state key ``(source_freshness, <config id>)``, so a delay that turns
  into a stopped scan (or back) never doubles.
* It opens and advances rule states but never CLOSES them. The state's
  lifecycle belongs to the dispatch: the first successful collection after the
  outage runs ``_prepare_alert_deliveries``, which closes the state when the
  scan is fresh again (and keeps it open when it is still merely late, where
  ``last_event_at`` <= the overdue bucket means no second send). Closing here
  would race that collection and flap the incident.
* One outage = one alert. The candidate's ``bucket`` is ``last_collection_at``,
  which does not move while the scan is stopped, so the gate's "newer bucket
  AND cooldown elapsed" never re-sends it on later sweeps.

The gate below is the dispatch's own per-scope gate reduced to the one scope
family the sweep emits; it reuses every helper that owns a decision
(``_claim_rule_state``, ``_cooldown_elapsed``, ``_correlation_group_id``,
``_suppressed_correlation_group_ids``, ``_create_deliveries``,
``_buffer_pending_items``) so the two paths cannot disagree about a send.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from tripl.alerting_matching import AlertMatchCandidate, rule_matches_anomaly
from tripl.core.bucketing import to_utc
from tripl.models.alert_destination import AlertDestination
from tripl.models.alert_rule import AlertRule
from tripl.models.alert_rule_state import AlertRuleState
from tripl.models.project import Project
from tripl.models.scan_config import ScanConfig
from tripl.services.active_org_scope import project_in_active_org
from tripl.services.source_freshness import STATUS_OVERDUE
from tripl.worker.celery_app import celery_app
from tripl.worker.db import _get_sync_session
from tripl.worker.tasks._demo_pause import is_demo_paused
from tripl.worker.tasks.metrics.alert_payload import (
    _build_alert_scope_names,
    _load_enabled_alert_destinations,
)
from tripl.worker.tasks.metrics.dispatch import (
    _as_utc,
    _bucket_is_newer,
    _buffer_pending_items,
    _claim_rule_state,
    _cooldown_elapsed,
    _correlation_group_id,
    _create_deliveries,
    _latest_bucket,
    _scope_partition_id,
    _suppressed_correlation_group_ids,
)
from tripl.worker.tasks.metrics.signals import _get_source_freshness_candidates
from tripl.worker.tasks.metrics.urls import _get_project_slug

logger = logging.getLogger(__name__)

# Distinct from the metrics dispatchers (…017 / …018) and the digest flusher
# (…019) so none of them can starve another.
_FRESHNESS_SWEEP_ADVISORY_LOCK_KEY = 4_021_968_020

_OVERDUE_ONLY: frozenset[str] = frozenset({STATUS_OVERDUE})


def _sweep_candidate_configs(session: Session, *, now: datetime) -> list[ScanConfig]:
    """Scheduled scan configs of projects with a live freshness rule.

    Scheduled = interval AND time column, the same pair ``check_metrics_due``
    collects on, so a manual-only scan is never "overdue". The project must
    have an enabled rule that opted into ``include_source_freshness`` on an
    enabled destination: without one nothing could be delivered, and skipping
    here keeps the tick one query on an instance that never opted in.

    Paused demos are skipped: the demo pause gate stops their collection on
    purpose, which is not an outage.
    """
    has_freshness_rule = exists(
        select(AlertRule.id)
        .join(AlertDestination, AlertDestination.id == AlertRule.destination_id)
        .where(
            AlertDestination.project_id == ScanConfig.project_id,
            AlertDestination.enabled.is_(True),
            AlertRule.enabled.is_(True),
            AlertRule.include_source_freshness.is_(True),
        )
    )
    rows = session.execute(
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
            has_freshness_rule,
            project_in_active_org(),
        )
        .order_by(ScanConfig.id)
    ).all()
    return [
        config
        for config, is_demo, seeded_at, last_accessed in rows
        if not (is_demo and is_demo_paused(seeded_at, last_accessed, now))
    ]


def _deliver_freshness_candidates(
    session: Session,
    config: ScanConfig,
    candidates: list[AlertMatchCandidate],
    *,
    now: datetime,
) -> tuple[list[uuid.UUID], int]:
    """Run ``candidates`` through the dispatch's send gate; ``(ids, buffered)``.

    Mirrors ``dispatch._prepare_alert_deliveries`` for one config-partitioned
    scope family, minus the close loop (see the module docstring). The mute
    check is here too: a delivery path without one is the tripl-jfm3.99 bug.
    """
    destinations = _load_enabled_alert_destinations(session, config.project_id)
    if not destinations or not candidates:
        return [], 0

    project_slug = _get_project_slug(session, config.project_id)
    scope_names = _build_alert_scope_names(session, candidates)
    suppressed_group_ids = _suppressed_correlation_group_ids(session, project_id=config.project_id)
    delivery_ids: list[uuid.UUID] = []
    buffered_count = 0

    for destination in destinations:
        # A cadence destination's cadence is its rate limiter; see the same
        # line in ``_prepare_alert_deliveries``.
        cooldown_applies = destination.delivery_schedule_cron is None
        for rule in (rule for rule in destination.rules if rule.enabled):
            # Freshness candidates never carry an event, so the event-type map
            # the dispatch builds is empty by construction.
            matched = [
                candidate
                for candidate in candidates
                if rule_matches_anomaly(rule, candidate, event_type_by_event_id={})
            ]
            anomalies_to_send: list[AlertMatchCandidate] = []
            for anomaly in matched:
                state_config_id = _scope_partition_id(anomaly.scope_type, config_id=config.id)
                state = session.execute(
                    select(AlertRuleState).where(
                        AlertRuleState.rule_id == rule.id,
                        AlertRuleState.scan_config_id == state_config_id,
                        AlertRuleState.scope_type == anomaly.scope_type,
                        AlertRuleState.scope_ref == anomaly.scope_ref,
                    )
                ).scalar_one_or_none()
                should_send = False
                if state is None:
                    state, created = _claim_rule_state(
                        session,
                        rule_id=rule.id,
                        scan_config_id=state_config_id,
                        scope_type=anomaly.scope_type,
                        scope_ref=anomaly.scope_ref,
                        now=now,
                        bucket=anomaly.bucket,
                    )
                    if state is None:
                        continue
                    if created:
                        should_send = True
                    else:
                        state.last_anomaly_bucket = _latest_bucket(
                            anomaly.bucket, state.last_anomaly_bucket
                        )
                else:
                    if not state.is_active:
                        state.is_active = True
                        state.opened_at = now
                        state.closed_at = None
                        should_send = not cooldown_applies or _cooldown_elapsed(
                            state.last_notified_at,
                            now=now,
                            cooldown_minutes=rule.cooldown_minutes,
                        )
                    elif state.last_notified_at is None or (
                        _bucket_is_newer(anomaly.bucket, state.last_anomaly_bucket)
                        and (
                            not cooldown_applies
                            or _cooldown_elapsed(
                                state.last_notified_at,
                                now=now,
                                cooldown_minutes=rule.cooldown_minutes,
                            )
                        )
                    ):
                        should_send = True
                    state.last_anomaly_bucket = _latest_bucket(
                        anomaly.bucket, state.last_anomaly_bucket
                    )
                if should_send:
                    anomalies_to_send.append(anomaly)

            if not anomalies_to_send:
                continue
            # Same NULL-means-not-muted reading as the dispatch (tripl-a50u).
            rule_muted_until = _as_utc(rule.muted_until)
            if rule_muted_until is not None and rule_muted_until > now:
                continue

            correlation_by_anomaly = {
                id(anomaly): _correlation_group_id(
                    scan_config_id=_scope_partition_id(anomaly.scope_type, config_id=config.id),
                    rule_id=rule.id,
                    scope_type=anomaly.scope_type,
                    scope_ref=anomaly.scope_ref,
                    direction=anomaly.direction,
                )
                for anomaly in anomalies_to_send
            }
            if suppressed_group_ids:
                anomalies_to_send = [
                    anomaly
                    for anomaly in anomalies_to_send
                    if correlation_by_anomaly[id(anomaly)] not in suppressed_group_ids
                ]
            if not anomalies_to_send:
                continue

            if destination.delivery_schedule_cron is None:
                delivery_ids.extend(
                    _create_deliveries(
                        session,
                        config,
                        project_slug=project_slug,
                        rule=rule,
                        destination=destination,
                        anomalies=anomalies_to_send,
                        scope_names=scope_names,
                        correlation_by_anomaly=correlation_by_anomaly,
                        scan_job_id=None,
                    )
                )
            else:
                buffered_count += _buffer_pending_items(
                    session,
                    config,
                    rule=rule,
                    destination=destination,
                    anomalies=anomalies_to_send,
                    scope_names=scope_names,
                    correlation_by_anomaly=correlation_by_anomaly,
                    scan_job_id=None,
                    now=now,
                )
    return delivery_ids, buffered_count


def _sweep_overdue_sources(
    session: Session,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, int], list[uuid.UUID]]:
    """One sweep pass; commits per config so one failure cannot sink the rest."""
    reference = to_utc(now) if now is not None else datetime.now(UTC)
    summary = {"checked": 0, "overdue": 0, "alerts_queued": 0, "alerts_buffered": 0, "failed": 0}
    delivery_ids: list[uuid.UUID] = []
    for config in _sweep_candidate_configs(session, now=reference):
        summary["checked"] += 1
        config_id = config.id
        try:
            candidates = _get_source_freshness_candidates(
                session, config, reference, statuses=_OVERDUE_ONLY
            )
            if not candidates:
                continue
            summary["overdue"] += 1
            ids, buffered = _deliver_freshness_candidates(
                session, config, list(candidates.values()), now=reference
            )
            session.commit()
        except Exception:
            session.rollback()
            summary["failed"] += 1
            logger.exception("sweep_overdue_sources: scan config %s failed", config_id)
            continue
        delivery_ids.extend(ids)
        summary["alerts_queued"] += len(ids)
        summary["alerts_buffered"] += buffered
    return summary, delivery_ids


@celery_app.task(name="tripl.worker.tasks.metrics.sweep_overdue_sources")  # type: ignore[untyped-decorator]
def sweep_overdue_sources() -> dict[str, int]:
    """Beat entry point: alert on every scheduled scan that stopped collecting."""
    from tripl.worker.tasks.alerts import send_alert_delivery
    from tripl.worker.tasks.metrics.schedule import (
        _release_advisory_lock,
        _try_acquire_advisory_lock,
    )

    session = _get_sync_session()
    lock_conn = None
    try:
        lock_conn, acquired = _try_acquire_advisory_lock(
            session, _FRESHNESS_SWEEP_ADVISORY_LOCK_KEY
        )
        if not acquired:
            logger.info("sweep_overdue_sources: another sweep holds the lock; skipping")
            return {"checked": 0, "overdue": 0, "alerts_queued": 0, "alerts_buffered": 0}
        summary, delivery_ids = _sweep_overdue_sources(session)
        # After the per-config commits, so a worker never picks up a delivery
        # row that is not visible yet.
        for delivery_id in delivery_ids:
            send_alert_delivery.delay(str(delivery_id))
        return summary
    finally:
        _release_advisory_lock(lock_conn, _FRESHNESS_SWEEP_ADVISORY_LOCK_KEY)
        session.close()
