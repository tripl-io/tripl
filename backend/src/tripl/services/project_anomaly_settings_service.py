import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.models.anomaly_scope_override import AnomalyScopeOverride
from tripl.models.project_anomaly_settings import (
    DEFAULT_ANOMALY_DETECTION_ENABLED,
    DEFAULT_ANOMALY_INGESTION_SETTLING_MINUTES,
    DEFAULT_BASELINE_WINDOW_BUCKETS,
    DEFAULT_DETECT_SCOPE,
    DEFAULT_MIN_EXPECTED_COUNT,
    DEFAULT_MIN_HISTORY_BUCKETS,
    DEFAULT_RECENT_SIGNAL_WINDOW_HOURS,
    DEFAULT_SIGMA_THRESHOLD,
    ProjectAnomalySettings,
)
from tripl.models.scan_config import ScanConfig
from tripl.schemas.project_anomaly_settings import (
    AnomalyScopeOverrideListResponse,
    AnomalyScopeOverrideResponse,
    ProjectAnomalySettingsResponse,
    ProjectAnomalySettingsUpdate,
    settling_window_conflict,
)
from tripl.services._project_settings_rows import get_or_create_project_row
from tripl.services.holiday_calendar import sync_project_holidays
from tripl.services.project_lookup import resolve_project_id


def _defaults_response(project_id: uuid.UUID) -> ProjectAnomalySettingsResponse:
    """The settings of a project that never saved its own, with no row written.

    Built from the constants the columns default to: a transient row would read
    ``None`` everywhere, since column defaults apply only at flush.
    """
    return ProjectAnomalySettingsResponse(
        project_id=project_id,
        anomaly_detection_enabled=DEFAULT_ANOMALY_DETECTION_ENABLED,
        detect_project_total=DEFAULT_DETECT_SCOPE,
        detect_event_types=DEFAULT_DETECT_SCOPE,
        detect_events=DEFAULT_DETECT_SCOPE,
        detect_metrics=DEFAULT_DETECT_SCOPE,
        baseline_window_buckets=DEFAULT_BASELINE_WINDOW_BUCKETS,
        min_history_buckets=DEFAULT_MIN_HISTORY_BUCKETS,
        sigma_threshold=DEFAULT_SIGMA_THRESHOLD,
        min_expected_count=DEFAULT_MIN_EXPECTED_COUNT,
        recent_signal_window_hours=DEFAULT_RECENT_SIGNAL_WINDOW_HOURS,
        anomaly_ingestion_settling_minutes=DEFAULT_ANOMALY_INGESTION_SETTLING_MINUTES,
    )


async def get_project_anomaly_settings(
    session: AsyncSession,
    slug: str,
) -> ProjectAnomalySettingsResponse:
    """Read-only: a project that never saved its settings gets the defaults back
    without a row being written (GETs must not mutate the database)."""
    project_id = await resolve_project_id(session, slug)
    settings = await session.scalar(
        select(ProjectAnomalySettings).where(ProjectAnomalySettings.project_id == project_id)
    )
    if settings is None:
        return _defaults_response(project_id)
    return ProjectAnomalySettingsResponse.model_validate(settings)


_PAIRED_TIMING_FIELDS = ("anomaly_ingestion_settling_minutes", "recent_signal_window_hours")
_PAIRED_HISTORY_FIELDS = ("min_history_buckets", "baseline_window_buckets")


def _reject_incoherent_timings(
    patch: Mapping[str, Any],
    settings: ProjectAnomalySettings,
) -> None:
    """Refuse a patch that would leave the two timing dials cancelling out.

    Checked on the MERGED settings rather than on the request body, because the
    same collision arrives from both directions and a partial patch shows only
    one of them: raising the allowance under a stored window, or lowering the
    window under a stored allowance.

    Only patches that TOUCH one of the pair are checked. A row written before
    this guard existed can still hold the illegal combination, and a retroactive
    check would lock every other detection setting on that project behind fixing
    it — the guard exists to stop the pair being written, not to hold the form
    hostage.
    """
    if not any(patch.get(field) is not None for field in _PAIRED_TIMING_FIELDS):
        return
    settling = patch.get("anomaly_ingestion_settling_minutes")
    window = patch.get("recent_signal_window_hours")
    conflict = settling_window_conflict(
        settling_minutes=(
            settings.anomaly_ingestion_settling_minutes if settling is None else settling
        ),
        recent_window_hours=(settings.recent_signal_window_hours if window is None else window),
    )
    if conflict is not None:
        raise HTTPException(status_code=422, detail=conflict)


def _reject_incoherent_history(patch: Mapping[str, Any], settings: ProjectAnomalySettings) -> None:
    if not any(field in patch for field in _PAIRED_HISTORY_FIELDS):
        return
    minimum = patch.get("min_history_buckets", settings.min_history_buckets)
    window = patch.get("baseline_window_buckets", settings.baseline_window_buckets)
    if minimum > window:
        raise HTTPException(
            status_code=422,
            detail=(
                f"min_history_buckets ({minimum}) must not exceed "
                f"baseline_window_buckets ({window}); otherwise rolling anomaly scoring "
                "cannot collect enough baseline buckets."
            ),
        )


async def update_project_anomaly_settings(
    session: AsyncSession,
    slug: str,
    data: ProjectAnomalySettingsUpdate,
) -> ProjectAnomalySettings:
    project_id = await resolve_project_id(session, slug)
    settings = await get_or_create_project_row(session, ProjectAnomalySettings, project_id)
    patch = data.model_dump(exclude_unset=True, exclude_none=True)
    _reject_incoherent_timings(patch, settings)
    _reject_incoherent_history(patch, settings)
    # The one field null means something for: it turns the calendar off.
    calendar_set = "holiday_country" in data.model_fields_set
    patch.pop("holiday_country", None)
    for key, value in patch.items():
        setattr(settings, key, value)
    if calendar_set:
        settings.holiday_country = data.holiday_country
        await session.flush()
        await session.run_sync(sync_project_holidays, project_id)
    await session.commit()
    await session.refresh(settings)
    return settings


async def list_anomaly_scope_overrides(
    session: AsyncSession,
    slug: str,
) -> AnomalyScopeOverrideListResponse:
    """Every scope the false-positive ratchet has tightened, newest first.

    This IS the undo surface: the ratchet is permanent and does not decay, so
    the only way back to the project-wide sensitivity for a scope is to see the
    override and delete it.
    """
    project_id = await resolve_project_id(session, slug)
    rows = (
        await session.execute(
            select(AnomalyScopeOverride, ScanConfig.name)
            .outerjoin(ScanConfig, ScanConfig.id == AnomalyScopeOverride.scan_config_id)
            .where(AnomalyScopeOverride.project_id == project_id)
            .order_by(AnomalyScopeOverride.updated_at.desc())
        )
    ).all()
    items = [
        AnomalyScopeOverrideResponse(
            id=override.id,
            scan_config_id=override.scan_config_id,
            scan_config_name=scan_config_name,
            scope_type=str(override.scope_type),
            scope_ref=override.scope_ref,
            scope_name=override.scope_name,
            sigma_threshold=override.sigma_threshold,
            min_expected_count=override.min_expected_count,
            false_positive_count=override.false_positive_count,
            created_at=override.created_at,
            updated_at=override.updated_at,
        )
        for override, scan_config_name in rows
    ]
    return AnomalyScopeOverrideListResponse(items=items, total=len(items))


async def get_anomaly_scope_override(
    session: AsyncSession,
    slug: str,
    override_id: uuid.UUID,
) -> AnomalyScopeOverride:
    project_id = await resolve_project_id(session, slug)
    override = await session.get(AnomalyScopeOverride, override_id)
    if override is None or override.project_id != project_id:
        raise HTTPException(status_code=404, detail="Anomaly scope override not found")
    return override


async def delete_anomaly_scope_override(
    session: AsyncSession,
    slug: str,
    override_id: uuid.UUID,
) -> None:
    override = await get_anomaly_scope_override(session, slug, override_id)
    await session.delete(override)
    await session.commit()
