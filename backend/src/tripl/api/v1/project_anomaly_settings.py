import uuid

from fastapi import APIRouter, Depends

from tripl.api.deps import EditorUserDep, SessionDep, get_editor_user
from tripl.models.project_anomaly_settings import ProjectAnomalySettings
from tripl.schemas.project_anomaly_settings import (
    AnomalyScopeOverrideListResponse,
    ProjectAnomalySettingsResponse,
    ProjectAnomalySettingsUpdate,
)
from tripl.services import audit_service, project_anomaly_settings_service
from tripl.services.holiday_calendar import supported_countries

router = APIRouter(
    prefix="/projects/{slug}/anomaly-settings",
    tags=["anomaly-settings"],
)
_editor_required = [Depends(get_editor_user)]


@router.get("", response_model=ProjectAnomalySettingsResponse)
async def get_project_anomaly_settings(
    session: SessionDep, slug: str
) -> ProjectAnomalySettingsResponse:
    return await project_anomaly_settings_service.get_project_anomaly_settings(session, slug)


@router.get("/holiday-countries", response_model=list[str])
async def list_holiday_countries(slug: str) -> list[str]:
    """ISO 3166-1 alpha-2 codes ``holiday_country`` accepts (F18)."""
    return list(supported_countries())


@router.patch(
    "",
    response_model=ProjectAnomalySettingsResponse,
    dependencies=_editor_required,
)
async def update_project_anomaly_settings(
    session: SessionDep,
    slug: str,
    data: ProjectAnomalySettingsUpdate,
    current_user: EditorUserDep,
) -> ProjectAnomalySettings:
    updated = await project_anomaly_settings_service.update_project_anomaly_settings(
        session,
        slug,
        data,
    )
    await audit_service.record(
        session,
        user=current_user,
        action="anomaly_settings.update",
        target_type="anomaly_settings",
        target_id=None,
        project_slug=slug,
        payload=data.model_dump(exclude_unset=True),
    )
    return updated


@router.get("/scope-overrides", response_model=AnomalyScopeOverrideListResponse)
async def list_anomaly_scope_overrides(
    session: SessionDep,
    slug: str,
) -> AnomalyScopeOverrideListResponse:
    return await project_anomaly_settings_service.list_anomaly_scope_overrides(session, slug)


@router.delete("/scope-overrides/{override_id}", status_code=204)
async def delete_anomaly_scope_override(
    session: SessionDep,
    slug: str,
    override_id: uuid.UUID,
    current_user: EditorUserDep,
) -> None:
    """Undo one false-positive ratchet: the scope returns to the project setting."""
    existing = await project_anomaly_settings_service.get_anomaly_scope_override(
        session, slug, override_id
    )
    scope_name = existing.scope_name or existing.scope_ref
    await project_anomaly_settings_service.delete_anomaly_scope_override(session, slug, override_id)
    await audit_service.record(
        session,
        user=current_user,
        action="anomaly_scope_override.delete",
        target_type="anomaly_scope_override",
        target_id=override_id,
        target_name=scope_name,
        project_slug=slug,
    )
