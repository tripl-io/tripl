import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from tripl.services.holiday_calendar import is_supported_country


class ProjectAnomalySettingsUpdate(BaseModel):
    anomaly_detection_enabled: bool | None = None
    detect_project_total: bool | None = None
    detect_event_types: bool | None = None
    detect_events: bool | None = None
    detect_metrics: bool | None = None
    baseline_window_buckets: int | None = Field(None, ge=1)
    min_history_buckets: int | None = Field(None, ge=1)
    sigma_threshold: float | None = Field(None, ge=0.1)
    min_expected_count: int | None = Field(None, ge=0)
    # Capped at 30 days: beyond that the "open signal" count stops describing
    # anything current, and the latest-scan horizon (max(window, 3 x interval))
    # already covers long scan intervals.
    recent_signal_window_hours: int | None = Field(None, ge=1, le=720)
    # Wall-clock allowance for the warehouse to finish delivering a bucket
    # before its value is scored. 0 scores every collected
    # bucket immediately; capped at 24h because the allowance is also the
    # detection latency it buys, and the trailing re-eval window that re-scores
    # the held-back buckets is 30 buckets wide.
    #
    # The two fields above ALSO constrain each other — see
    # ``settling_window_conflict``. That rule cannot be a field constraint here:
    # this is a partial patch, so a body carrying only one of the pair is blind
    # to half of the collision. It is enforced on the merged settings in
    # ``project_anomaly_settings_service.update_project_anomaly_settings``.
    anomaly_ingestion_settling_minutes: int | None = Field(None, ge=0, le=1440)
    # A country's public holidays as planned events (F18): an ISO 3166-1
    # alpha-2 code from GET .../holiday-countries, or null to turn it off.
    holiday_country: str | None = Field(None, min_length=2, max_length=2)

    @field_validator("holiday_country")
    @classmethod
    def _check_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.upper()
        if not is_supported_country(code):
            raise ValueError("holiday_country must be a country with a holiday calendar")
        return code


def settling_window_conflict(
    *,
    settling_minutes: int,
    recent_window_hours: int,
) -> str | None:
    """Reject a settling allowance that reaches the open-signal window.

    A bucket is withheld from SCORING for ``anomaly_ingestion_settling_minutes``
    and then counts as an open signal for ``recent_signal_window_hours``. Once
    the allowance reaches the window, the newest anomaly the detector may emit is
    born older than the window on every grid the window governs (any interval
    with ``3 x interval <= window``, where the freshness floor in
    ``services.monitoring_utils._freshness_horizon`` does not bind), so the
    Anomalies page, the sidebar badge and the Overview stat all read zero — while
    alerting, which classifies against the settled head, keeps delivering. That
    silent disagreement is the thing being refused; ``tripl-l429.15``.

    REFUSED, not clamped: both numbers are operator-set, so clamping would
    silently rewrite whichever one was touched last and make the stored result
    depend on edit order. A 422 names both values and lets the operator pick.

    Deliberately interval-independent, and therefore slightly over-strict: a
    project whose scans are all daily has a freshness horizon of 72h and would
    survive a 24h allowance under a 24h window. Consulting the scan intervals
    would make the settings valid or invalid depending on scan rows that can
    change afterwards, with no re-validation hook — a stable rule that reads only
    the two settings is worth the occasional false refusal, and "score a day late
    but call a signal stale after a day" is incoherent on its own terms.

    Returns the operator-facing message, or ``None`` when the pair is coherent.
    """
    window_minutes = recent_window_hours * 60
    if settling_minutes < window_minutes:
        return None
    return (
        f"Ingestion settling ({settling_minutes} min) must stay below the open signal "
        f"window ({recent_window_hours}h = {window_minutes} min). A bucket held back "
        "that long is already outside the window by the time it is scored, so every "
        "signal on this project would read as closed on the Anomalies page while "
        "alerts kept firing. Lower the settling allowance or raise the window."
    )


class ProjectAnomalySettingsResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    anomaly_detection_enabled: bool
    detect_project_total: bool
    detect_event_types: bool
    detect_events: bool
    detect_metrics: bool
    baseline_window_buckets: int
    min_history_buckets: int
    sigma_threshold: float
    min_expected_count: int
    recent_signal_window_hours: int
    anomaly_ingestion_settling_minutes: int
    holiday_country: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AnomalyScopeOverrideResponse(BaseModel):
    """One scope the false-positive ratchet has made stricter.

    ``sigma_threshold`` / ``min_expected_count`` are ABSOLUTE and replace the
    project settings above for this scope only. Deleting the override is the
    undo — the scope falls straight back to the project values.
    """

    id: uuid.UUID
    # NULL for ``metric`` scopes: catalog metric series are project-global.
    scan_config_id: uuid.UUID | None
    scan_config_name: str | None = None
    scope_type: str
    scope_ref: str
    scope_name: str
    sigma_threshold: float
    min_expected_count: int
    false_positive_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AnomalyScopeOverrideListResponse(BaseModel):
    items: list[AnomalyScopeOverrideResponse]
    total: int
