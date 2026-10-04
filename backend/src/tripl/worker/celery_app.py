from datetime import timedelta

from celery import Celery
from celery.schedules import crontab
from celery.signals import beat_init, setup_logging, worker_init, worker_process_init

from tripl.config import settings
from tripl.logging_config import configure_logging
from tripl.observability.metrics import install_celery_instrumentation
from tripl.observability.tracing import setup_worker_tracing
from tripl.services.app_settings_service import apply_startup_service_overrides
from tripl.worker.db import dispose_engine

# Importing this module in an API request must not read the settings database.
# Celery's own process-start signals apply persisted overrides instead.

celery_app = Celery("tripl")
celery_app.conf.broker_url = settings.rabbitmq_url
celery_app.conf.result_backend = None
celery_app.conf.task_serializer = "json"
celery_app.conf.accept_content = ["json"]
celery_app.conf.timezone = "UTC"
celery_app.conf.broker_connection_retry_on_startup = True
# RabbitMQ 4 refuses transient non-exclusive queues (the deprecated feature
# `transient_nonexcl_queues`). Celery's remote-control queues (the worker's
# pidbox, and the reply queue `mingle` and `inspect` declare) and its event
# queue (`gossip`'s celeryev) are exactly that by default, so the worker failed
# at startup and died (RestartFreqExceeded). Exclusive, they live and die with
# the connection that declared them, which is all they were for.
celery_app.conf.control_queue_exclusive = True
celery_app.conf.event_queue_exclusive = True

# A request-path publish must fail fast. Most dispatches happen inside async API
# handlers — services/_celery_dispatch.py keeps them off the event loop, but a
# caller still waits for the thread — and Celery's defaults (a 4s connect
# timeout against 1+3 publish attempts) let one unreachable broker hold that
# caller for ~19s. These bounds cut the worst case to ~7s. The worker's own
# startup connect is unaffected: broker_connection_retry_on_startup keeps
# retrying it.
celery_app.conf.broker_connection_timeout = 2.0
celery_app.conf.task_publish_retry_policy = {
    "max_retries": 2,
    "interval_start": 0,
    "interval_step": 0.2,
    "interval_max": 0.5,
}

# Reliability: only ack tasks after successful completion so a crashed worker
# re-queues them. Combined with reject_on_worker_lost for hard kills (OOM, SIGKILL).
celery_app.conf.task_acks_late = True
celery_app.conf.task_reject_on_worker_lost = True

# Default hard cap so a runaway task can't block the queue. Long-running tasks
# such as metrics replay may override this per task. Soft limit raises
# SoftTimeLimitExceeded so tasks can clean up; hard limit SIGKILLs the worker
# after the grace period.
celery_app.conf.task_soft_time_limit = 55 * 60  # 55 min
celery_app.conf.task_time_limit = 60 * 60  # 60 min

# Prefetch 1 task per worker — prevents one slow worker from hoarding the queue
# while others idle. Safer default for unpredictable task durations.
celery_app.conf.worker_prefetch_multiplier = 1

# Celery reads default_retry_delay from the Task class, not app configuration.
celery_app.Task.default_retry_delay = 30

celery_app.conf.beat_schedule = {
    "check-metrics-due": {
        "task": "tripl.worker.tasks.metrics.check_metrics_due",
        # Scans schedule on interval boundaries (15m, 1h, 6h, …), so 5-minute
        # polling is more than enough and leaves headroom if the dispatcher
        # itself becomes slow against a growing scan_configs table.
        "schedule": crontab(minute="*/5"),
    },
    "check-metric-definitions-due": {
        "task": "tripl.worker.tasks.metrics.check_metric_definitions_due",
        # Catalog metrics schedule on interval boundaries (15m, 1h, …) just like
        # scans, so a 5-minute dispatcher tick is plenty. Runs independently of
        # check-metrics-due (separate task + advisory lock).
        "schedule": crontab(minute="*/5"),
    },
    "cleanup-schema-drifts": {
        "task": "tripl.worker.tasks.maintenance.cleanup_schema_drifts",
        # Daily prune is plenty — drift rows past retention are filtered
        # out at read time anyway, the cleanup only reclaims storage.
        "schedule": crontab(hour=3, minute=0),
    },
    "cleanup-scan-jobs": {
        "task": "tripl.worker.tasks.maintenance.cleanup_scan_jobs",
        "schedule": crontab(hour=4, minute=0),
    },
    "cleanup-distribution-drifts": {
        "task": "tripl.worker.tasks.maintenance.cleanup_distribution_drifts",
        "schedule": crontab(hour=5, minute=0),
    },
    "sweep-orphan-photo-blobs": {
        "task": "tripl.worker.tasks.maintenance.sweep_orphan_photo_blobs",
        # Daily: an orphan costs only storage, and PHOTO_ORPHAN_SWEEP_GRACE_HOURS
        # already holds every blob back for a day.
        "schedule": crontab(hour=5, minute=30),
    },
    "requeue-stranded-alert-deliveries": {
        "task": "tripl.worker.tasks.maintenance.requeue_stranded_alert_deliveries",
        # Every 5 minutes — deliveries are only considered stranded after
        # STRANDED_DELIVERY_MINUTES, so this just bounds detection latency for
        # rows the worker/broker failed to dispatch.
        "schedule": crontab(minute="*/5"),
    },
    "requeue-stranded-org-deletions": {
        "task": "tripl.worker.tasks.org_delete.requeue_stranded_org_deletions",
        # Hourly: a row counts as stranded only after STRANDED_DELETION_GRACE
        # (2 h, above the task time limit) without a purge job touching it, and
        # all it costs meanwhile is a slug and some storage.
        "schedule": crontab(minute=17),
    },
    "retire-idle-organizations": {
        "task": "tripl.worker.tasks.org_delete.retire_idle_organizations",
        # Daily: a no-op unless IDLE_ORG_RETENTION_DAYS is set on a hosted
        # instance (a public demo), where it keeps the database from only growing.
        "schedule": crontab(minute=41, hour=3),
    },
    "sync-holiday-calendars": {
        "task": "tripl.worker.tasks.holiday_calendar.sync_holiday_calendars",
        # Daily: keeps each project's holiday planned events one year ahead.
        "schedule": crontab(minute=17, hour=2),
    },
    "send-weekly-plan-digest": {
        "task": "tripl.worker.tasks.alerts.send_weekly_plan_digest",
        "schedule": crontab(day_of_week=1, hour=8, minute=0),
    },
    "check-deprecated-sunset-events": {
        "task": "tripl.worker.tasks.alerts.check_deprecated_sunset_events",
        # Scheduled rather than deleted, which was the live alternative: the
        # task was registered but on nobody's timer, so its output could not
        # reach a reader. Kept because the feature is already half-shipped —
        # the weekly digest above renders the identical counter from the
        # identical predicate to the identical set of destinations
        # ("- Deprecated events still receiving data: N", built by
        # alerts_messages._build_plan_digest_message). What a count cannot do is
        # NAME the events, and "3" once a week is not something an operator can
        # act on. This task is that line expanded.
        #
        # Daily, and not tighter, because both sides of the comparison move
        # slowly: ``sunset_at`` is a date an owner typed into the plan, and
        # ``last_seen_at`` is refreshed by a scan, so at most once per scan
        # interval. A sub-daily tick could only re-send an unchanged list.
        #
        # Daily, and not weekly, because the task holds no per-event
        # suppression state — every run re-sends the same true count and the
        # same capped page of names under it — so this number IS the repeat
        # rate, and repeating is the point: data still arriving for an event
        # the plan retired is a standing condition that should nag until
        # someone acts. At the digest's own cadence it would also arrive in the
        # same week as the line it exists to expand, which is a duplicate
        # rather than a follow-up.
        "schedule": crontab(hour=6, minute=0),
    },
    "check-lifecycle-findings": {
        "task": "tripl.worker.tasks.lifecycle.check_lifecycle_findings",
        # Daily (#258), ahead of the 06:00 sunset alert and the Monday 08:00
        # digest so both read today's findings. Both sides of each condition
        # move slowly — a sunset date an owner typed, volume windows of 24h and
        # 7d — so a tighter tick could only rewrite unchanged rows.
        "schedule": crontab(hour=5, minute=45),
    },
    "snapshot-project-health": {
        "task": "tripl.worker.tasks.health.snapshot_project_health",
        # Daily (F15, #268): after the 05:45 lifecycle findings the score reads,
        # and before the Monday 08:00 plan digest, whose "Plan health" line reads
        # today's snapshot. The score moves on day-scale windows (7d/30d seen,
        # 7d distribution drift), so a tighter tick would only rewrite the row.
        "schedule": crontab(hour=5, minute=55),
    },
    "sync-implementation-tickets": {
        "task": "tripl.worker.tasks.implementation_tickets.sync_implementation_tickets",
        # Poll every 5 minutes — implementation tickets close on human timescales
        # (a dev finishing a Jira issue), so tighter polling buys nothing and only
        # adds load against the tracker's REST API.
        "schedule": crontab(minute="*/5"),
    },
    "reindex-stale-search-documents": {
        "task": "tripl.worker.tasks.search.reindex_stale_search_documents",
        # Every 10 minutes, and each pass takes only STALE_REINDEX_BRANCHES_PER_RUN
        # branches. A builder bump is rare and the corpus it has to repair is
        # bounded by the branch count, so this trades speed for staying out of the
        # API's way — a 10-branch instance is fully converted inside an hour, the
        # same order as the delay main already has (it waits for the next scan).
        # Between bumps the query matches nothing and a pass is one indexed lookup.
        "schedule": crontab(minute="*/10"),
    },
    "requeue-stranded-search-embeddings": {
        "task": "tripl.worker.tasks.search.requeue_stranded_search_embeddings",
        # Every 15 minutes — embeddings refresh event-driven after each reindex;
        # this chaser only bounds how long a lost queue message or an exhausted
        # batch retry can leave documents pending (STRANDED_EMBEDDING_MINUTES).
        "schedule": crontab(minute="*/15"),
    },
    "flush-due-alert-digests": {
        "task": "tripl.worker.tasks.alert_flush.flush_due_alert_digests",
        # 60s, not the 300s the other dispatchers use. Those poll for work that
        # lands on 15m/1h/6h boundaries and nobody watches the clock for; a
        # digest cadence is a wall-clock time the operator TYPED, and the UI
        # offers a cron minute field. At 300s "daily at 09:00" would arrive
        # anywhere in [09:00, 09:05) and the minute field would be a lie.
        #
        # The tick is cheap enough to justify it: one indexed read of
        # alert_destinations filtered to the enabled+scheduled rows (tens, not
        # millions), and nothing else at all when none are due. Compare
        # check-metrics-due, which does a grouped max(bucket) over the metrics
        # table every 300s.
        "schedule": crontab(minute="*"),
    },
    "sweep-overdue-sources": {
        "task": "tripl.worker.tasks.metrics.sweep_overdue_sources",
        # Every 15 minutes (#269). A scan that STOPPED collecting has no
        # per-run dispatch to raise its "scan overdue" alert, so this sweep
        # does: overdue means no completed collection for 2 x interval, and the
        # shortest interval is 15m, so a quarter-hour tick bounds detection
        # latency at one interval past the threshold. Idempotent per outage —
        # the AlertRuleState gate sends each stopped scan once.
        "schedule": crontab(minute="*/15"),
    },
    "advance-demos": {
        "task": "tripl.worker.tasks.demo_runtime.advance_demos",
        # Every 5 minutes — demos advance on hourly bucket boundaries, so 5-minute
        # polling keeps them inside the freshness horizon with headroom, and the
        # per-demo tick is idempotent so an early/overlapping run is a no-op. A
        # no-op entirely when demo_runtime_enabled is false. Independent of the
        # metrics dispatchers (own task + per-project advisory lock).
        "schedule": crontab(minute="*/5"),
    },
    "send-instant-notification-emails": {
        "task": "tripl.worker.tasks.notification_email.send_notification_emails",
        # Every minute (#259): the safety net under the per-write enqueue, so a
        # lost broker message delays an "instant" email by a minute instead of
        # dropping it. Cheap when idle: one indexed read of unemailed rows from
        # the last 24h. The emailed_at claim makes an overlap with the enqueued
        # run harmless.
        "schedule": crontab(minute="*"),
    },
    "deliver-audit-webhooks": {
        "task": "tripl.worker.tasks.audit_webhook.deliver_audit_webhooks",
        # Every 30 seconds (F20): the one entry off the crontab grid. An audit
        # webhook feeds a SIEM, where a minute of lag is visible, and the tick
        # is one indexed read of due outbox rows when nothing is queued. Not a
        # wall-clock time anyone typed, so no boundary to align to. A tick not
        # started within its interval is dropped; the next one covers it.
        "schedule": timedelta(seconds=30),
        "options": {"expires": 30},
    },
    "send-notification-digest-daily": {
        "task": "tripl.worker.tasks.notification_email.send_notification_digest",
        "schedule": crontab(hour=7, minute=0),
        "args": ("daily",),
    },
    "send-notification-digest-weekly": {
        "task": "tripl.worker.tasks.notification_email.send_notification_digest",
        # Monday, after the daily run and before the 08:00 plan digest.
        "schedule": crontab(day_of_week=1, hour=7, minute=15),
        "args": ("weekly",),
    },
}


def _configure_worker_runtime(**_kwargs: object) -> None:
    """Apply persisted settings before worker instrumentation and task execution."""
    apply_startup_service_overrides()
    configure_logging()
    if settings.prometheus_metrics_enabled:
        install_celery_instrumentation()
    setup_worker_tracing()


def _configure_beat_runtime(**_kwargs: object) -> None:
    """Apply persisted settings and logging when beat starts."""
    apply_startup_service_overrides()
    configure_logging()


def _configure_celery_logging(**_kwargs: object) -> None:
    """Own the root handler so Celery does not replace the configured format."""
    configure_logging()


worker_init.connect(_configure_worker_runtime, weak=False)
beat_init.connect(_configure_beat_runtime, weak=False)
setup_logging.connect(_configure_celery_logging, weak=False)

# The worker startup read creates a sync engine in the parent process. Dispose
# that inherited pool after prefork so each child opens its own connections.


@worker_process_init.connect  # type: ignore[untyped-decorator]
def _reset_sync_engine_after_fork(**_kwargs: object) -> None:
    dispose_engine()


# Import tasks so they are registered with the celery app
import tripl.worker.tasks.alert_digest_send  # noqa: F401, E402
import tripl.worker.tasks.alert_flush  # noqa: F401, E402
import tripl.worker.tasks.alert_owner_notify  # noqa: F401, E402
import tripl.worker.tasks.alerts  # noqa: F401, E402
import tripl.worker.tasks.audit_webhook  # noqa: F401, E402
import tripl.worker.tasks.demo_runtime  # noqa: F401, E402
import tripl.worker.tasks.docs_translate  # noqa: F401, E402
import tripl.worker.tasks.health  # noqa: F401, E402
import tripl.worker.tasks.holiday_calendar  # noqa: F401, E402
import tripl.worker.tasks.implementation_tickets  # noqa: F401, E402
import tripl.worker.tasks.lifecycle  # noqa: F401, E402
import tripl.worker.tasks.maintenance  # noqa: F401, E402
import tripl.worker.tasks.metrics  # noqa: F401, E402
import tripl.worker.tasks.metrics.freshness_sweep  # noqa: F401, E402
import tripl.worker.tasks.notification_email  # noqa: F401, E402
import tripl.worker.tasks.org_delete  # noqa: F401, E402
import tripl.worker.tasks.scan  # noqa: F401, E402
import tripl.worker.tasks.scan_dry_run  # noqa: F401, E402
import tripl.worker.tasks.search  # noqa: F401, E402
