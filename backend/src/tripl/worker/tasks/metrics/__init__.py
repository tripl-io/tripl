"""Celery tasks for collecting time-bucketed event metrics from ClickHouse.

Scheduled collection refreshes catalog events with the same cardinality analysis
pipeline as the manual scan, then collects time-bucketed counts. Explicit
metrics replay reuses the existing catalog so replay chunking bounds every
warehouse query it issues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tripl.worker.tasks.metrics._helpers import (
    ACTIVE_SCAN_JOB_STATUSES,
    MAX_BREAKDOWN_VALUE_LENGTH,
    RECENT_SIGNAL_WINDOW,
    SCOPE_SCHEMA_DRIFT,
    STALE_ACTIVE_SCAN_JOB_TIMEOUT,
    _build_adapter,
    _ceil_to_interval,
    _fail_stale_active_scan_job,
    _floor_to_interval,
    _get_active_scan_job,
    _get_scan_job_activity_at,
    _get_sync_session,
    _normalize_job_timestamp,
    _parse_task_datetime,
)
from tripl.worker.tasks.metrics.collect import _bump_event_last_seen
from tripl.worker.tasks.metrics.detect import (
    _recalculate_metric_anomalies,
    _recalculate_metric_breakdown_anomalies,
)
from tripl.worker.tasks.metrics.dispatch import _prepare_alert_deliveries
from tripl.worker.tasks.metrics.metric_rows import (
    _build_event_name_from_row,
    _collect_distribution_drift_rows,
    _collect_metric_breakdown_rows,
    _delete_distribution_drifts_window,
    _delete_event_metric_breakdowns_window,
    _delete_event_metrics_window,
    _get_scan_json_value_path_map,
    _is_supported_distribution_drift_field,
    _is_supported_metric_breakdown_column,
    _normalize_breakdown_value,
    _serialize_distribution_top_movers,
    _upsert_event_metric_breakdown_rows,
    _upsert_event_metrics_rows,
)
from tripl.worker.tasks.metrics.regression import _recalculate_release_regressions
from tripl.worker.tasks.metrics.schedule import check_metrics_due
from tripl.worker.tasks.metrics.schema_drift import (
    _detect_field_contract_violations,
    _diff_event_type_schema,
    _upsert_schema_drifts,
)
from tripl.worker.tasks.metrics.tasks import collect_metrics
from tripl.worker.tasks.metrics.urls import (
    _build_alert_audit_url,
    _build_event_details_url,
    _build_item_paths,
    _build_monitoring_url,
    _get_org_slug,
    _get_project_slug,
    _trim_alert_text,
)

# Re-exported from sibling modules so existing `tripl.worker.tasks.metrics.<name>`
# attribute access keeps working after the split.
__all__ = [
    "ACTIVE_SCAN_JOB_STATUSES",
    "MAX_BREAKDOWN_VALUE_LENGTH",
    "RECENT_SIGNAL_WINDOW",
    "SCOPE_SCHEMA_DRIFT",
    "STALE_ACTIVE_SCAN_JOB_TIMEOUT",
    "_build_adapter",
    "_build_alert_audit_url",
    "_build_event_details_url",
    "_build_event_name_from_row",
    "_build_item_paths",
    "_build_monitoring_url",
    "_ceil_to_interval",
    "_bump_event_last_seen",
    "_collect_distribution_drift_rows",
    "_collect_metric_breakdown_rows",
    "_delete_distribution_drifts_window",
    "_delete_event_metric_breakdowns_window",
    "_delete_event_metrics_window",
    "_diff_event_type_schema",
    "_detect_field_contract_violations",
    "_fail_stale_active_scan_job",
    "_floor_to_interval",
    "_get_active_scan_job",
    "_get_org_slug",
    "_get_project_slug",
    "_get_scan_job_activity_at",
    "_get_scan_json_value_path_map",
    "_get_sync_session",
    "_is_supported_distribution_drift_field",
    "_is_supported_metric_breakdown_column",
    "_normalize_breakdown_value",
    "_normalize_job_timestamp",
    "_prepare_alert_deliveries",
    "_parse_task_datetime",
    "_recalculate_metric_anomalies",
    "_recalculate_metric_breakdown_anomalies",
    "_recalculate_release_regressions",
    "_serialize_distribution_top_movers",
    "_trim_alert_text",
    "_upsert_event_metric_breakdown_rows",
    "_upsert_event_metrics_rows",
    "_upsert_schema_drifts",
    "check_metrics_due",
    "collect_metrics",
    "send_alert_delivery",
]

if TYPE_CHECKING:
    from tripl.worker.tasks.alerts import send_alert_delivery


def __getattr__(name: str) -> Any:
    """Keep the legacy task alias without importing alerts during registration."""
    if name == "send_alert_delivery":
        from tripl.worker.tasks.alerts import send_alert_delivery

        return send_alert_delivery
    raise AttributeError(name)
