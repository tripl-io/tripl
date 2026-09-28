"""An organization's own settings: mail, AI chat and row limits (F20 PR9).

Real routes under ``/api/v1/orgs/{org}/settings``: ``settings`` is not in
``ORG_REWRITE_PREFIXES``, so ``OrgPathRewriteMiddleware`` never rewrites them to
the legacy ``/settings`` (critique #10). Every route is gated on the path's
organization (``deps._resolve_path_org``: a stranger gets 404 before any 403).

* ``GET/PATCH/PUT /orgs/{org}/settings`` and the two probes — an owner or admin
  of the organization, from a browser session. A body carrying an operator
  field is a 422 (the update model forbids unknown keys).
* ``GET /orgs/{org}/settings/row-limits`` — any member (the scan form quotes the
  caps to whoever fills it in, as ``/settings/row-limits`` does).

Resolution is org override -> operator override -> env with the credential
group, fallback-policy and ceiling rules of ``app_settings_service``. On a
self-hosted instance the default organization's scope IS the operator's
(critique #17), and the response says so (``scope: "operator"``); writing its
SMTP relay or AI endpoint there still needs a platform admin (403), because the
operator's relay carries every user's account mail and other organizations
inherit both.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from tripl.api.deps import (
    ManagedOrgDep,
    PathOrgAdminUserDep,
    PathOrgMemberUserDep,
    SessionDep,
)
from tripl.schemas.app_settings import (
    AiSettingsTestRequest,
    EmailSettingsTestRequest,
    OrgSettingsResponse,
    OrgSettingsUpdate,
    RowLimitDefaultsResponse,
    SettingsTestResponse,
)
from tripl.services import (
    _settings_probe,
    app_settings_service,
    audit_service,
    org_settings_service,
)

router = APIRouter(prefix="/orgs/{org}/settings", tags=["organizations"])


async def _payload(session: SessionDep, org: ManagedOrgDep) -> OrgSettingsResponse:
    body: dict[str, Any] = await org_settings_service.org_settings_payload(
        session, org_id=org.id, org_slug=org.slug
    )
    return OrgSettingsResponse.model_validate(body)


@router.get("", response_model=OrgSettingsResponse)
async def get_org_settings(
    session: SessionDep,
    _current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
) -> OrgSettingsResponse:
    return await _payload(session, org)


@router.patch("", response_model=OrgSettingsResponse)
async def patch_org_settings(
    session: SessionDep,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
    payload: OrgSettingsUpdate,
) -> OrgSettingsResponse:
    changes = org_settings_service.flatten_changes(payload.model_dump(exclude_unset=True))
    scope = await org_settings_service.write_org_changes(
        session, org.id, changes, is_platform_admin=current_user.is_platform_admin
    )
    await audit_service.record(
        session,
        user=current_user,
        action="settings.update",
        target_type="settings",
        target_id=None,
        target_name=org.slug,
        payload=org_settings_service.audit_scope_payload(sorted(changes), scope),
        # The organization's audit feed either way: on a self-hosted default
        # organization the values are the operator's, but it was this
        # organization's admin who changed them, here.
        organization_id=org.id,
    )
    return await _payload(session, org)


@router.put("", response_model=OrgSettingsResponse)
async def put_org_settings(
    session: SessionDep,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
    payload: OrgSettingsUpdate,
) -> OrgSettingsResponse:
    """Identical to PATCH: a sparse override map, unset fields left untouched."""
    return await patch_org_settings(session, current_user, org, payload)


@router.get("/row-limits", response_model=RowLimitDefaultsResponse)
async def get_org_row_limits(
    session: SessionDep,
    _current_user: PathOrgMemberUserDep,
    org: ManagedOrgDep,
) -> RowLimitDefaultsResponse:
    """The organization's effective row caps, readable by every member."""
    config = await app_settings_service.get_row_limit_defaults(session, org_id=org.id)
    return RowLimitDefaultsResponse(
        scan_row_limit_default=config.scan_row_limit_default,
        metrics_row_limit_default=config.metrics_row_limit_default,
    )


@router.post("/ai/test", response_model=SettingsTestResponse)
async def test_org_ai_settings(
    session: SessionDep,
    _current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
    payload: AiSettingsTestRequest,
) -> SettingsTestResponse:
    """Probe the AI provider THIS organization would use, with its saved values."""
    config = await app_settings_service.get_ai_config(session, org_id=org.id)
    return await _settings_probe.probe_ai(config, payload.prompt)


@router.post("/email/test", response_model=SettingsTestResponse)
async def test_org_email_settings(
    session: SessionDep,
    current_user: PathOrgAdminUserDep,
    org: ManagedOrgDep,
    payload: EmailSettingsTestRequest,
) -> SettingsTestResponse:
    """Send one probe through THIS organization's relay; always 200."""
    config = await app_settings_service.get_email_config(session, org_id=org.id)
    return await _settings_probe.probe_email(config, payload.recipient or current_user.email)
