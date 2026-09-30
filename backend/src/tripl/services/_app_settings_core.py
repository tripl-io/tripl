"""Stored overrides of the runtime settings: env values, flattening and storage reads.

Private half of :mod:`tripl.services.app_settings_service`, which re-exports
every name here; import from the facade.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl import crypto
from tripl.config import DEPLOYMENT_SELF_HOSTED, Settings, settings
from tripl.models.app_setting import AI_SETTINGS_KEY, SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.services._app_settings_fields import (
    EDITABLE_FIELDS,
    ORG_FIELDS,
    SECRET_FIELDS,
    STARTUP_APPLIED_FIELDS,
)
from tripl.services.ai_defaults import (
    DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    DEFAULT_ASK_SYSTEM_PROMPT,
    DEFAULT_DESCRIBE_SYSTEM_PROMPT,
)

# Records keep the facade's logger name, so log filters and captures that
# match ``tripl.services.app_settings_service`` still see them.
logger = logging.getLogger("tripl.services.app_settings_service")


# What the environment (or the built-in default) held for each field that
# ``apply_startup_service_overrides`` pinned onto ``settings`` at boot. Written
# there, read here; see that function for why nothing ever puts it back.
_ENV_BEFORE_STARTUP_APPLY: dict[str, Any] = {}


def env_service_values() -> dict[str, Any]:
    """Every editable service setting from env/default settings only.

    "env only" is not the same as "whatever ``settings`` holds right now". For a
    STARTUP_APPLIED_FIELDS field whose override was applied at boot, the singleton
    permanently carries the OVERRIDE's value, so once that override is cleared
    the singleton is no longer a witness for anything the environment delivered —
    it would report the deleted value and ``_setting_source`` would badge it
    "Env", crediting a variable that may not exist (tripl-wkwv.2).
    """
    values: dict[str, Any] = {
        "app_base_url": settings.app_base_url,
        "scan_row_limit_default": settings.scan_row_limit_default,
        "metrics_row_limit_default": settings.metrics_row_limit_default,
        "cors_allow_origins": settings.cors_allow_origins,
        "session_cookie_name": settings.session_cookie_name,
        "session_ttl_hours": settings.session_ttl_hours,
        "session_cookie_secure": settings.session_cookie_secure,
        "security_headers_enabled": settings.security_headers_enabled,
        "hsts_enabled": settings.hsts_enabled,
        "hsts_max_age_seconds": settings.hsts_max_age_seconds,
        "content_security_policy": settings.content_security_policy,
        "rate_limit_enabled": settings.rate_limit_enabled,
        "rate_limit_login_per_minute": settings.rate_limit_login_per_minute,
        "rate_limit_register_per_hour": settings.rate_limit_register_per_hour,
        "rate_limit_trust_forwarded_for": settings.rate_limit_trust_forwarded_for,
        "registration_mode": settings.registration_mode,
        "photo_storage_backend": settings.photo_storage_backend,
        "photo_local_dir": settings.photo_local_dir,
        "photo_max_size_mb": settings.photo_max_size_mb,
        "photo_allowed_mime": settings.photo_allowed_mime,
        "gcs_photo_bucket": settings.gcs_photo_bucket,
        "gcs_photo_credentials_path": settings.gcs_photo_credentials_path,
        "gcs_photo_public": settings.gcs_photo_public,
        "gcs_photo_signed_url_ttl_seconds": settings.gcs_photo_signed_url_ttl_seconds,
        "request_id_header": settings.request_id_header,
        "log_level": settings.log_level,
        "log_json": settings.log_json,
        "prometheus_metrics_enabled": settings.prometheus_metrics_enabled,
        "otel_exporter_otlp_endpoint": settings.otel_exporter_otlp_endpoint,
        "otel_service_name": settings.otel_service_name,
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_username": settings.smtp_username,
        "smtp_password": settings.smtp_password,
        "smtp_security": settings.resolved_smtp_security(),
        "smtp_from_address": settings.smtp_from_address,
        "ai_enabled": settings.ai_enabled,
        "ai_base_url": settings.ai_base_url,
        "ai_model": settings.ai_model,
        "ai_api_key": settings.resolved_ai_api_key(),
        "ai_timeout_seconds": settings.ai_timeout_seconds,
        "ai_max_output_tokens": settings.ai_max_output_tokens,
        "describe_system_prompt": DEFAULT_DESCRIBE_SYSTEM_PROMPT,
        "ask_system_prompt": DEFAULT_ASK_SYSTEM_PROMPT,
        "alert_explanation_system_prompt": DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
        "search_embeddings_enabled": settings.search_embeddings_enabled,
        "search_embedding_provider": settings.search_embedding_provider,
        "search_embedding_model": settings.search_embedding_model,
        "search_embedding_api_key": settings.resolved_search_embedding_api_key(),
        # Env-only for the operator (READ_ONLY_ENV_FIELDS), an organization field
        # all the same: its value here is what an organization inherits.
        "search_embedding_base_url": settings.search_embedding_base_url,
        # Organization-only (F20 PR11): the operator's GCS credentials are
        # GCS_PHOTO_CREDENTIALS_PATH or the server's ambient identity.
        "gcs_photo_credentials_json": "",
    }
    # Last, and unconditionally: a startup-applied override is still an override,
    # and ``build_service_values`` writes it back over this a moment later. What
    # this restores is the answer for a field whose override was CLEARED.
    values.update(_ENV_BEFORE_STARTUP_APPLY)
    return values


def _decrypt_override(key: str, value: Any) -> str | None:
    try:
        return crypto.decrypt_value(str(value))
    except crypto.InvalidToken:
        logger.warning("Cannot decrypt stored %s override; using env value", key)
        return None


def build_service_values(overrides: dict[str, Any]) -> dict[str, Any]:
    values = env_service_values()
    for key, value in overrides.items():
        if key not in EDITABLE_FIELDS or value is None:
            continue
        if key in SECRET_FIELDS:
            decrypted = _decrypt_override(key, value)
            if decrypted is not None:
                values[key] = decrypted
            continue
        values[key] = value
    return values


def _operator_setting(key: str) -> Select[tuple[AppSetting]]:
    """The OPERATOR-scope row for ``key`` — the only scope anything reads today.

    ``app_settings`` is unique per scope, not per key (F20): an organization may
    hold its own row under the same key. Every lookup therefore names the scope,
    so an organization's override can never be picked up as the instance's.
    """
    return select(AppSetting).where(AppSetting.key == key, AppSetting.organization_id.is_(None))


async def _get_overrides_for_key(session: AsyncSession, key: str) -> dict[str, Any]:
    row = await session.scalar(_operator_setting(key))
    if row is None or not isinstance(row.value, dict):
        return {}
    return dict(row.value)


def _get_overrides_for_key_sync(session: Session, key: str) -> dict[str, Any]:
    row = session.scalar(_operator_setting(key))
    if row is None or not isinstance(row.value, dict):
        return {}
    return dict(row.value)


async def get_service_overrides(session: AsyncSession) -> dict[str, Any]:
    # ``ai`` is a legacy read path from the initial AI-only version. The
    # canonical ``service`` document wins when both contain a field.
    legacy_ai = await _get_overrides_for_key(session, AI_SETTINGS_KEY)
    service = await _get_overrides_for_key(session, SERVICE_SETTINGS_KEY)
    return {**legacy_ai, **service}


def get_service_overrides_sync(session: Session) -> dict[str, Any]:
    legacy_ai = _get_overrides_for_key_sync(session, AI_SETTINGS_KEY)
    service = _get_overrides_for_key_sync(session, SERVICE_SETTINGS_KEY)
    return {**legacy_ai, **service}


# ── organization scope (F20 PR9) ────────────────────────────────────────────


def settings_scope_for(org_id: uuid.UUID | None) -> uuid.UUID | None:
    """The ``app_settings`` scope an organization's values live in; ``None`` = operator.

    On a self-hosted instance the default organization IS the operator scope
    (critique #17): its admins editing SMTP there must still change the relay
    that password-reset mail goes through, and nothing about a single-team
    instance changes when organizations arrive. Every other organization — and
    the default one on a hosted instance — has its own scope.
    """
    if org_id is None:
        return None
    if settings.deployment_mode == DEPLOYMENT_SELF_HOSTED and org_id == DEFAULT_ORG_ID:
        return None
    return org_id


def _org_setting(key: str, org_id: uuid.UUID) -> Select[tuple[AppSetting]]:
    return select(AppSetting).where(AppSetting.key == key, AppSetting.organization_id == org_id)


async def get_org_overrides(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """The organization's own stored overrides (raw: secrets still encrypted)."""
    row = await session.scalar(_org_setting(SERVICE_SETTINGS_KEY, org_id))
    if row is None or not isinstance(row.value, dict):
        return {}
    return {key: value for key, value in row.value.items() if key in ORG_FIELDS}


def get_org_overrides_sync(session: Session, org_id: uuid.UUID) -> dict[str, Any]:
    row = session.scalar(_org_setting(SERVICE_SETTINGS_KEY, org_id))
    if row is None or not isinstance(row.value, dict):
        return {}
    return {key: value for key, value in row.value.items() if key in ORG_FIELDS}


def _org_values(org_overrides: Mapping[str, Any]) -> dict[str, Any]:
    """An organization's stored overrides, decrypted, ORG_FIELDS only.

    A secret that no longer decrypts is dropped — and, being part of a
    credential group, its group stays the organization's with an empty key,
    never the operator's.
    """
    values: dict[str, Any] = {}
    for key, value in org_overrides.items():
        if key not in ORG_FIELDS or value is None:
            continue
        if key in SECRET_FIELDS:
            decrypted = _decrypt_override(key, value)
            values[key] = decrypted if decrypted is not None else ""
            continue
        values[key] = value
    return values


def _reject_startup_breaking_overrides(overrides: dict[str, Any]) -> None:
    """Refuse a security override that would stop the next boot (tripl-jfm3.93).

    ``apply_startup_service_overrides`` now ignores such a value rather than
    letting it brick the instance, but silently dropping what the operator just
    saved is its own kind of lie. Rejecting at save time is the honest half:
    they find out while they are still looking at the form.
    """
    try:
        candidate = Settings.model_validate(
            {
                **settings.model_dump(),
                **{
                    field: overrides[field]
                    for field in STARTUP_APPLIED_FIELDS
                    if overrides.get(field) is not None
                },
            }
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if settings.debug:
        return
    # Only what THIS change breaks. A deployment that is already missing, say,
    # SECRET_KEY must not have every unrelated settings save rejected on top.
    introduced = [
        p for p in candidate.production_problems() if p not in settings.production_problems()
    ]
    if introduced:
        raise HTTPException(
            status_code=422,
            detail="These settings would stop the app from starting:\n  - "
            + "\n  - ".join(introduced),
        )


def apply_org_override_changes(
    overrides: Mapping[str, Any], changes: Mapping[str, Any]
) -> dict[str, Any]:
    """An organization's stored overrides after ``changes`` (a new dict).

    ``None`` clears a field, an empty secret clears it too, anything else is
    stored (secrets encrypted). Keys outside :data:`ORG_FIELDS` are ignored.
    """
    result = dict(overrides)
    for key, value in changes.items():
        if key not in ORG_FIELDS:
            continue
        if value is None or (key in SECRET_FIELDS and value == ""):
            result.pop(key, None)
            continue
        result[key] = crypto.encrypt_value(str(value)) if key in SECRET_FIELDS else value
    return result
