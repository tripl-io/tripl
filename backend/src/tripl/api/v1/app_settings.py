from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status

from tripl.api.deps import PLATFORM_ADMIN_REQUIRED, SessionDep, SettingsAdminUserDep
from tripl.models.user import User
from tripl.schemas.app_settings import (
    AiPromptDefaultsResponse,
    AiSettingsResponse,
    AiSettingsTestRequest,
    EmailSettingsTestRequest,
    RowLimitDefaultsResponse,
    ServiceSettingsResponse,
    ServiceSettingsUpdate,
    SettingsTestResponse,
)
from tripl.schemas.event_photo import PhotoLimitsResponse
from tripl.services import (
    _email_test_send,
    app_settings_service,
    audit_service,
    event_photo_service,
    llm_service,
)

logger = logging.getLogger(__name__)

# Who may do what here (F20 PR4). ``/settings`` binds no organization and there
# is no per-organization storage until PR9, so:
#
# * every route but the two public limits takes :data:`SettingsAdminUserDep`: a
#   platform admin, or an owner/admin of the default organization (whose values
#   the instance scope is), from a browser session;
# * a write touching any ``app_settings_service.OPERATOR_FIELDS`` field (the
#   public URL, security, observability, server paths, the photo size cap)
#   additionally needs ``users.is_platform_admin``, else 403;
# * the ``system`` block of the read is ``None`` for anyone but a platform admin.
router = APIRouter(prefix="/settings", tags=["settings"])


def _require_operator_write(user: User, changes: dict[str, Any]) -> None:
    """403 when ``changes`` sets an operator-only field and ``user`` is not a platform admin."""
    if app_settings_service.touches_operator_fields(changes) and not user.is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=PLATFORM_ADMIN_REQUIRED)


def _for_caller(payload: dict[str, Any], user: User) -> ServiceSettingsResponse:
    """The settings response, with ``system`` hidden from a non-platform admin."""
    response = ServiceSettingsResponse.model_validate(payload)
    if user.is_platform_admin:
        return response
    return response.model_copy(update={"system": None})


def _flatten_update(payload: ServiceSettingsUpdate) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    data = payload.model_dump(exclude_unset=True)
    for section_value in data.values():
        if isinstance(section_value, dict):
            changes.update(section_value)
    return changes


def _ai_response(payload: dict[str, Any]) -> AiSettingsResponse:
    ai_fields = set(app_settings_service.AI_FIELDS)
    return AiSettingsResponse(
        ai=payload["ai"],
        overridden_fields=[field for field in payload["overridden_fields"] if field in ai_fields],
        sources={key: value for key, value in payload["sources"].items() if key.startswith("ai.")},
    )


@router.get("", response_model=ServiceSettingsResponse)
async def get_service_settings(
    session: SessionDep,
    current_user: SettingsAdminUserDep,
) -> ServiceSettingsResponse:
    return _for_caller(await app_settings_service.get_service_settings(session), current_user)


@router.patch("", response_model=ServiceSettingsResponse)
async def patch_service_settings(
    session: SessionDep,
    current_user: SettingsAdminUserDep,
    payload: ServiceSettingsUpdate,
) -> ServiceSettingsResponse:
    changes = _flatten_update(payload)
    _require_operator_write(current_user, changes)
    settings_payload = await app_settings_service.service_settings_payload(
        session,
        await app_settings_service.update_service_overrides(session, changes),
    )
    await audit_service.record(
        session,
        user=current_user,
        action="settings.update",
        target_type="settings",
        target_id=None,
        payload={"changed_fields": sorted(changes)},
    )
    return _for_caller(settings_payload, current_user)


@router.put("", response_model=ServiceSettingsResponse)
async def put_service_settings(
    session: SessionDep,
    current_user: SettingsAdminUserDep,
    payload: ServiceSettingsUpdate,
) -> ServiceSettingsResponse:
    """Upsert service overrides. Intentionally identical to PATCH: unset fields
    are left untouched (partial update), not reset. Kept as a stable alias for
    clients that issue PUT; settings are a sparse override map with no full
    "replace all" semantics."""
    return await patch_service_settings(session, current_user, payload)


@router.get("/photo-limits", response_model=PhotoLimitsResponse)
async def get_photo_limits() -> PhotoLimitsResponse:
    """The photo upload limit, readable by every signed-in user.

    The rest of this router is for settings admins; this one value is not, because it is
    an editor's upload it refuses and the browser should say so before the
    upload rather than after (EVT-28). The router's own dependency still
    requires a session.
    """
    return PhotoLimitsResponse(photo_max_size_mb=event_photo_service.max_size_mb())


@router.get("/row-limits", response_model=RowLimitDefaultsResponse)
async def get_row_limit_defaults(session: SessionDep) -> RowLimitDefaultsResponse:
    """The instance row caps a scan falls back to, readable by every signed-in user.

    Admin-only like the rest of this router would hide the real numbers from the
    editors who fill in a scan's Limits, so the form hard-coded the shipped
    defaults instead (B15). Two integers, nothing about the connection.
    """
    config = await app_settings_service.get_row_limit_defaults(session)
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
    session: SessionDep,
    _current_user: SettingsAdminUserDep,
) -> AiSettingsResponse:
    return _ai_response(await app_settings_service.get_service_settings(session))


@router.post("/ai/test", response_model=SettingsTestResponse)
async def test_ai_settings(
    session: SessionDep,
    _current_user: SettingsAdminUserDep,
    payload: AiSettingsTestRequest,
) -> SettingsTestResponse:
    config = await app_settings_service.get_ai_config(session)
    if not llm_service.is_enabled(config):
        return SettingsTestResponse(
            ok=False,
            message="AI is disabled or no API key is configured.",
        )
    try:
        raw = await asyncio.to_thread(
            llm_service.complete,
            "You are a connection test endpoint. Keep the response short.",
            payload.prompt,
            max_tokens=20,
            temperature=0,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("AI settings test failed", exc_info=True)
        return SettingsTestResponse(ok=False, message=str(exc))
    message = (raw or "").strip()
    return SettingsTestResponse(ok=bool(message), message=message or "No response from provider.")


@router.post("/email/test", response_model=SettingsTestResponse)
async def test_email_settings(
    session: SessionDep,
    current_user: SettingsAdminUserDep,
    payload: EmailSettingsTestRequest,
) -> SettingsTestResponse:
    """Send one probe message with the saved SMTP settings and report what happened.

    Always 200: a relay refusing us is the answer the caller asked for, not a
    server fault — the same reasoning the alert-destination test states. The
    error text is passed through verbatim because a useful SMTP diagnostic is
    the server's own words ("535 authentication failed", a connection timeout);
    smtplib carries the relay's response in there, never the credential we sent.
    """
    config = await app_settings_service.get_email_config(session)
    recipient = payload.recipient or current_user.email
    try:
        await asyncio.to_thread(
            _email_test_send.send_test_email,
            email_config=config,
            recipient=recipient,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("SMTP settings test failed", exc_info=True)
        return SettingsTestResponse(ok=False, message=str(exc))
    return SettingsTestResponse(ok=True, message=f"Test message sent to {recipient}.")
