from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from tripl.api.deps import (
    ORG_ADMIN_REQUIRED,
    PLATFORM_ADMIN_REQUIRED,
    CurrentUserDep,
    SessionDep,
    SettingsAdminUserDep,
    legacy_settings_org_id,
)
from tripl.models.user import User
from tripl.schemas.app_settings import (
    AiPromptDefaultsResponse,
    AiSettingsResponse,
    AiSettingsTestRequest,
    CombinedSettingsResponse,
    EmailSettingsTestRequest,
    RowLimitDefaultsResponse,
    ServiceSettingsUpdate,
    SettingsTestResponse,
)
from tripl.schemas.event_photo import PhotoLimitsResponse
from tripl.services import (
    _settings_probe,
    app_settings_service,
    audit_service,
    org_settings_service,
    photo_storage_service,
    project_access,
)
from tripl.services.app_settings_service import OPERATOR_FIELDS, ORG_FIELDS, STORAGE_FIELDS

logger = logging.getLogger(__name__)

# The legacy combined view (F20 PR9). ``/settings`` binds no organization; it
# resolves one itself (``deps.legacy_settings_org_id``): the default
# organization when self-hosted, the user's only organization when hosted.
#
# * every route but the two public limits takes :data:`SettingsAdminUserDep`: a
#   platform admin, or an owner/admin of that organization, from a browser
#   session;
# * a write touching an ``OPERATOR_FIELDS`` field needs ``is_platform_admin``
#   (403) and lands in the operator scope;
# * a write touching an ``ORG_FIELDS`` field lands in the organization's scope —
#   which on a self-hosted instance IS the operator scope (critique #17:
#   password-reset mail follows the SMTP set here). On a hosted instance it
#   needs an owner/admin role in the resolved organization, and a platform
#   admin with no single organization writes the operator's defaults. Whenever
#   it lands in the operator scope, its credential groups (SMTP relay, AI
#   endpoint and key) need ``is_platform_admin`` too (403);
# * the read is the combined view: operator fields as the operator has them,
#   organization fields as the resolved organization runs with them. For
#   anyone but a platform admin the operator's infrastructure is withheld:
#   ``system``, ``security``, ``storage`` and ``observability`` are ``None``
#   and the embedding endpoint is blank unless it is the organization's own.
#
# The per-scope surfaces are ``/orgs/{org}/settings`` and ``/platform/settings``.
router = APIRouter(prefix="/settings", tags=["settings"])


#: Response sections that are wholly the operator's infrastructure.
_OPERATOR_SECTIONS: tuple[str, ...] = ("security", "storage", "observability")

#: What this legacy route writes to the OPERATOR scope, platform admins only.
#: The storage section is the operator's own store and ceilings here, as it
#: always was: an organization's own storage (F20 PR11) is set under
#: ``/orgs/{org}/settings`` (``storage``), never through this combined view.
_LEGACY_OPERATOR_FIELDS: frozenset[str] = OPERATOR_FIELDS | frozenset(STORAGE_FIELDS)
_EMBEDDING_URL_SOURCE = "ai.search_embedding_base_url"


def _for_caller(payload: dict[str, Any], user: User) -> CombinedSettingsResponse:
    """The combined view, with the operator's infrastructure withheld from a
    non-platform admin (server paths, bucket names, telemetry and embedding
    endpoints, CORS and rate-limit policy are not an organization's business)."""
    response = CombinedSettingsResponse.model_validate(payload)
    if user.is_platform_admin:
        return response
    hidden_prefixes = tuple(f"{section}." for section in (*_OPERATOR_SECTIONS, "system"))
    # The embedding endpoint is withheld while it is the OPERATOR's; an
    # organization's own endpoint (F20 PR10) is its own to see.
    own_endpoint = response.sources.get(_EMBEDDING_URL_SOURCE) == "org"
    ai = (
        response.ai
        if own_endpoint
        else response.ai.model_copy(update={"search_embedding_base_url": ""})
    )
    return response.model_copy(
        update={
            "system": None,
            **dict.fromkeys(_OPERATOR_SECTIONS),
            "ai": ai,
            "overridden_fields": [
                field
                for field in response.overridden_fields
                if field not in _LEGACY_OPERATOR_FIELDS
            ],
            "sources": {
                key: value
                for key, value in response.sources.items()
                if not key.startswith(hidden_prefixes)
                and (own_endpoint or key != _EMBEDDING_URL_SOURCE)
            },
        }
    )


def _flatten_update(payload: ServiceSettingsUpdate) -> dict[str, Any]:
    return org_settings_service.flatten_changes(payload.model_dump(exclude_unset=True))


def _ai_response(payload: dict[str, Any]) -> AiSettingsResponse:
    ai_fields = set(app_settings_service.AI_FIELDS)
    return AiSettingsResponse(
        ai=payload["ai"],
        overridden_fields=[field for field in payload["overridden_fields"] if field in ai_fields],
        sources={key: value for key, value in payload["sources"].items() if key.startswith("ai.")},
    )


async def _legacy_org(request: Request, session: AsyncSession, user: User) -> uuid.UUID | None:
    return await legacy_settings_org_id(request, session, user)


async def _combined_payload(session: AsyncSession, org_id: uuid.UUID | None) -> dict[str, Any]:
    """Operator fields from the operator scope, org fields as ``org_id`` resolves them."""
    resolved = await app_settings_service.resolve_for_org(session, org_id)
    if resolved.org_scope is not None:
        operator_overrides = await app_settings_service.get_service_overrides(session)
        operator = app_settings_service.resolve_settings(operator_overrides, None)
        overridden = {
            *(field for field in resolved.overridden_fields if field not in STORAGE_FIELDS),
            *(field for field in operator_overrides if field in _LEGACY_OPERATOR_FIELDS),
        }
        # The storage section is the operator's here (see _LEGACY_OPERATOR_FIELDS).
        values = {**resolved.values, **{f: operator.values[f] for f in STORAGE_FIELDS}}
        sources = {**resolved.sources, **{f: operator.sources[f] for f in STORAGE_FIELDS}}
        resolved = app_settings_service.ResolvedSettings(
            values=values,
            sources=sources,
            org_scope=resolved.org_scope,
            overridden_fields=tuple(sorted(overridden)),
            guarded_hosts=resolved.guarded_hosts,
        )
    return await app_settings_service.resolved_settings_payload(session, resolved)


@router.get("", response_model=CombinedSettingsResponse)
async def get_service_settings(
    request: Request,
    session: SessionDep,
    current_user: SettingsAdminUserDep,
) -> CombinedSettingsResponse:
    org_id = await _legacy_org(request, session, current_user)
    return _for_caller(await _combined_payload(session, org_id), current_user)


@router.patch("", response_model=CombinedSettingsResponse)
async def patch_service_settings(
    request: Request,
    session: SessionDep,
    current_user: SettingsAdminUserDep,
    payload: ServiceSettingsUpdate,
) -> CombinedSettingsResponse:
    changes = _flatten_update(payload)
    org_id = await _legacy_org(request, session, current_user)
    operator_changes = {k: v for k, v in changes.items() if k in _LEGACY_OPERATOR_FIELDS}
    org_changes = {
        k: v for k, v in changes.items() if k in ORG_FIELDS and k not in _LEGACY_OPERATOR_FIELDS
    }
    if operator_changes and not current_user.is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLATFORM_ADMIN_REQUIRED)
    org_scope = app_settings_service.settings_scope_for(org_id)
    if org_scope is None:
        # Everything lands in the operator scope: its SMTP relay and AI
        # endpoint are instance-wide (account mail, inherited AI).
        org_settings_service.require_operator_credential_writer(
            org_changes, is_platform_admin=current_user.is_platform_admin
        )
    elif org_changes:
        role = await project_access.org_role_of(session, current_user.id, org_scope)
        if not project_access.is_org_admin_role(role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORG_ADMIN_REQUIRED)
        # Checked BEFORE anything is written, so a refused org value leaves the
        # operator half of the same save unapplied too.
        await org_settings_service.validate_org_changes(session, org_scope, org_changes)

    # The organization's embedding identity before the save (F20 PR10): a move
    # enqueues the reindex of that organization's projects only.
    embeddings_before = (
        await org_settings_service.embedding_identity(session, org_id)
        if org_id is not None and org_settings_service.touches_embeddings(org_changes)
        else None
    )

    # (changed fields, scope written, organization feed)
    written: list[tuple[list[str], uuid.UUID | None, uuid.UUID | None]] = []
    if org_scope is None:
        if changes:
            await app_settings_service.update_service_overrides(session, changes)
            if operator_changes:
                written.append((sorted(operator_changes), None, None))
            if org_changes:
                # A self-hosted default organization's own values (the operator
                # alias): its feed, as ``/orgs/{org}/settings`` files them.
                written.append((sorted(org_changes), None, org_id))
    else:
        if operator_changes:
            await app_settings_service.update_service_overrides(session, operator_changes)
            written.append((sorted(operator_changes), None, None))
        if org_changes:
            await app_settings_service.update_org_overrides(session, org_scope, org_changes)
            written.append((sorted(org_changes), org_scope, org_scope))
    if org_id is not None:
        await org_settings_service.reindex_if_embeddings_moved(session, org_id, embeddings_before)
    if not written:
        written.append(([], None, None))
    for changed, scope, feed in written:
        await audit_service.record(
            session,
            user=current_user,
            action="settings.update",
            target_type="settings",
            target_id=None,
            payload=org_settings_service.audit_scope_payload(changed, scope),
            organization_id=feed,
        )
    return _for_caller(await _combined_payload(session, org_id), current_user)


@router.put("", response_model=CombinedSettingsResponse)
async def put_service_settings(
    request: Request,
    session: SessionDep,
    current_user: SettingsAdminUserDep,
    payload: ServiceSettingsUpdate,
) -> CombinedSettingsResponse:
    """Upsert service overrides. Intentionally identical to PATCH: unset fields
    are left untouched (partial update), not reset. Kept as a stable alias for
    clients that issue PUT; settings are a sparse override map with no full
    "replace all" semantics."""
    return await patch_service_settings(request, session, current_user, payload)


@router.get("/photo-limits", response_model=PhotoLimitsResponse)
async def get_photo_limits(
    request: Request, session: SessionDep, current_user: CurrentUserDep
) -> PhotoLimitsResponse:
    """The photo upload limits, readable by every signed-in user.

    The rest of this router is for settings admins; these values are not, because it is
    an editor's upload they refuse and the browser should say so before the
    upload rather than after (EVT-28). The caller's organization's limits
    (F20 PR11), resolved like the rest of the legacy route; the operator's when
    it resolves none. ``/orgs/{org}/settings/photo-limits`` names one.
    """
    org_id = await _legacy_org(request, session, current_user)
    policy = await photo_storage_service.policy_for_org(session, org_id)
    return photo_limits_response(policy)


def photo_limits_response(policy: photo_storage_service.PhotoPolicy) -> PhotoLimitsResponse:
    return PhotoLimitsResponse(
        photo_max_size_mb=policy.max_size_mb, photo_allowed_mime=list(policy.allowed_mime)
    )


@router.get("/row-limits", response_model=RowLimitDefaultsResponse)
async def get_row_limit_defaults(
    request: Request, session: SessionDep, current_user: CurrentUserDep
) -> RowLimitDefaultsResponse:
    """The instance row caps a scan falls back to, readable by every signed-in user.

    Admin-only like the rest of this router would hide the real numbers from the
    editors who fill in a scan's Limits, so the form hard-coded the shipped
    defaults instead (B15). Two integers, nothing about the connection.

    The caller's organization's caps (F20 PR9), resolved like the rest of the
    legacy route; ``/orgs/{org}/settings/row-limits`` names one explicitly.
    """
    org_id = await _legacy_org(request, session, current_user)
    config = await app_settings_service.get_row_limit_defaults(session, org_id=org_id)
    return RowLimitDefaultsResponse(
        scan_row_limit_default=config.scan_row_limit_default,
        metrics_row_limit_default=config.metrics_row_limit_default,
    )


@router.get("/ai/defaults", response_model=AiPromptDefaultsResponse)
async def get_ai_prompt_defaults(_current_user: SettingsAdminUserDep) -> AiPromptDefaultsResponse:
    """The built-in AI system prompts, for each prompt's "Restore default" (ST-30)."""
    return AiPromptDefaultsResponse(**app_settings_service.ai_prompt_defaults())


@router.get("/ai", response_model=AiSettingsResponse)
async def get_ai_settings(
    request: Request,
    session: SessionDep,
    current_user: SettingsAdminUserDep,
) -> AiSettingsResponse:
    org_id = await _legacy_org(request, session, current_user)
    return _ai_response(await _combined_payload(session, org_id))


@router.post("/ai/test", response_model=SettingsTestResponse)
async def test_ai_settings(
    request: Request,
    session: SessionDep,
    current_user: SettingsAdminUserDep,
    payload: AiSettingsTestRequest,
) -> SettingsTestResponse:
    org_id = await _legacy_org(request, session, current_user)
    config = await app_settings_service.get_ai_config(session, org_id=org_id)
    return await _settings_probe.probe_ai(config, payload.prompt)


@router.post("/email/test", response_model=SettingsTestResponse)
async def test_email_settings(
    request: Request,
    session: SessionDep,
    current_user: SettingsAdminUserDep,
    payload: EmailSettingsTestRequest,
) -> SettingsTestResponse:
    """Send one probe message with the saved SMTP settings and report what happened.

    Always 200: a relay refusing us is the answer the caller asked for, not a
    server fault — the same reasoning the alert-destination test states.
    """
    org_id = await _legacy_org(request, session, current_user)
    config = await app_settings_service.get_email_config(session, org_id=org_id)
    return await _settings_probe.probe_email(config, payload.recipient or current_user.email)
