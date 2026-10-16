"""Monitoring builder: how the demo team configured detection.

Authors the project's anomaly settings (detection on, the demo thresholds, no
ingestion settling), one per-scope override a team would set on a noisy series,
and the field contract that makes the warehouse's zero-amount purchases a
schema finding.

No holiday calendar, deliberately: a calendar's holidays are project-wide
expected windows, so on a holiday the day's incident would be tagged planned and
a demo created that day would open with an empty alert inbox. The Detection
settings page still offers one to pick.

The anomalies, drifts and regressions these settings produce are not written
here: the provisioning pipeline's replay detects them from the warehouse.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tripl.core.demo_warehouse import SCHEMA_DRIFT_EVENT, roster
from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.domain_enums import MetricScopeType
from tripl.models.field_definition import FieldDefinition
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.services.demo import noise
from tripl.services.demo.scenario import DemoContext

#: A noisy, low-volume series the team quietened rather than muted.
OVERRIDE_EVENT_NAME = "App Crash"
OVERRIDE_SIGMA_THRESHOLD = 4.0
OVERRIDE_MIN_EXPECTED_COUNT = 20
#: The smallest charge the store can take: the warehouse records a few
#: Purchase Completed rows at 0.00, which this contract reports.
AMOUNT_CONTRACT_MIN_VALUE = 0.01


async def build_monitoring(session: AsyncSession, ctx: DemoContext) -> None:
    settings = noise.DEMO_ANOMALY_SETTINGS
    # ``detect.py`` purges every anomaly of a project without an enabled
    # settings row, so the scan's ``anomaly_detection_enabled`` needs this one.
    session.add(
        ProjectAnomalySettings(
            project_id=ctx.project_id,
            anomaly_detection_enabled=True,
            detect_project_total=True,
            detect_event_types=True,
            detect_events=True,
            detect_metrics=True,
            # Platform stories are told by the attribution split and release
            # regressions; scoring each platform's series too would triple the
            # scopes every collection fits.
            detect_platform_breakdowns=False,
            baseline_window_buckets=settings.baseline_window_buckets,
            min_history_buckets=settings.min_history_buckets,
            sigma_threshold=settings.sigma_threshold,
            min_expected_count=settings.min_expected_count,
            # The writer completes every hour before a collection reads it.
            anomaly_ingestion_settling_minutes=noise.DEMO_INGESTION_SETTLING_MINUTES,
        )
    )
    override_event_id = ctx.event_ids.get(OVERRIDE_EVENT_NAME)
    if override_event_id is not None:
        session.add(
            AnomalyScopeOverride(
                project_id=ctx.project_id,
                scan_config_id=ctx.scan_config_id,
                scope_type=MetricScopeType.event.value,
                scope_ref=str(override_event_id),
                scope_name=OVERRIDE_EVENT_NAME,
                sigma_threshold=OVERRIDE_SIGMA_THRESHOLD,
                min_expected_count=OVERRIDE_MIN_EXPECTED_COUNT,
            )
        )
    amount_field_id = ctx.field_ids.get(f"{roster.event(SCHEMA_DRIFT_EVENT).event_type}.amount")
    if amount_field_id is not None:
        amount_field = await session.get(FieldDefinition, amount_field_id)
        if amount_field is not None:
            amount_field.contract_min_value = AMOUNT_CONTRACT_MIN_VALUE
    await session.flush()
