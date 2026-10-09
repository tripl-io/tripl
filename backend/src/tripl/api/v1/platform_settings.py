"""The operator's own settings: the platform console (F20 PR9).

``/api/v1/platform/settings`` is the operator scope, whole: tripl's own
infrastructure (``OPERATOR_FIELDS``: public URL, security, observability,
storage), the ``system`` block, and the operator's values of the
organization fields (mail, AI chat, search embeddings, row limits) — the
defaults every organization without its own value
inherits (``ORG_SETTINGS_OPERATOR_FALLBACK=all``) and the relay account mail
(sign-up, password reset, invitations) always uses. Platform admins only, from
a browser session; the flag grants no organization access and an organization
role grants none here. Org-free: no organization is resolved or bound.

A change to the operator's embedding values reaches the organizations that
inherit them through the stale-search sweep (F20 PR10), not an immediate
reindex: every inheriting organization's documents move at once, which is what
the sweep's per-run budget exists to pace.
"""

from __future__ import annotations

from fastapi import APIRouter

from tripl.api.deps import PlatformAdminUserDep, SessionDep
from tripl.schemas.app_settings import (
    AiSettingsTestRequest,
    EmailSettingsTestRequest,
    ServiceSettingsResponse,
    ServiceSettingsUpdate,
    SettingsTestResponse,
    TelemetryStatusResponse,
)
from tripl.services import (
    _settings_probe,
    app_settings_service,
    audit_service,
    org_settings_service,
    telemetry_service,
)

router = APIRouter(prefix="/platform/settings", tags=["platform"])


@router.get("", response_model=ServiceSettingsResponse)
async def get_platform_settings(
    session: SessionDep,
    _current_user: PlatformAdminUserDep,
) -> ServiceSettingsResponse:
    return ServiceSettingsResponse.model_validate(
        await app_settings_service.get_service_settings(session)
    )


@router.patch("", response_model=ServiceSettingsResponse)
async def patch_platform_settings(
    session: SessionDep,
    current_user: PlatformAdminUserDep,
    payload: ServiceSettingsUpdate,
) -> ServiceSettingsResponse:
    changes = org_settings_service.flatten_changes(payload.model_dump(exclude_unset=True))
    overrides = await app_settings_service.update_service_overrides(session, changes)
    await audit_service.record(
        session,
        user=current_user,
        action="settings.update",
        target_type="settings",
        target_id=None,
        payload=org_settings_service.audit_scope_payload(sorted(changes), None),
        organization_id=None,
    )
    return ServiceSettingsResponse.model_validate(
        await app_settings_service.service_settings_payload(session, overrides)
    )


@router.post("/ai/test", response_model=SettingsTestResponse)
async def test_platform_ai_settings(
    session: SessionDep,
    _current_user: PlatformAdminUserDep,
    payload: AiSettingsTestRequest,
) -> SettingsTestResponse:
    """Probe the operator's AI provider (what organizations inherit)."""
    config = await app_settings_service.get_ai_config(session, org_id=None)
    return await _settings_probe.probe_ai(config, payload.prompt)


@router.post("/email/test", response_model=SettingsTestResponse)
async def test_platform_email_settings(
    session: SessionDep,
    current_user: PlatformAdminUserDep,
    payload: EmailSettingsTestRequest,
) -> SettingsTestResponse:
    """Send one probe through the operator's relay: the one account mail uses."""
    config = await app_settings_service.get_operator_email_config(session)
    return await _settings_probe.probe_email(config, payload.recipient or current_user.email)


@router.get("/telemetry", response_model=TelemetryStatusResponse)
async def get_telemetry(
    session: SessionDep,
    _current_user: PlatformAdminUserDep,
) -> TelemetryStatusResponse:
    """The usage ping: whether it is sent (and if not, why), where to, and what it last held."""
    return TelemetryStatusResponse.model_validate(await telemetry_service.status(session))
