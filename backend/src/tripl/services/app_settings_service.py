"""Service-wide runtime settings stored in the ``app_settings`` table.

The ``service`` document stores only explicit overrides for env-based defaults
from :class:`tripl.config.Settings`. Resolution order per field is:
DB override -> env. Secret overrides are encrypted at rest and never returned
to clients; clients only see ``*_configured`` booleans.

Both async (API) and sync (Celery worker) accessors are provided. Sync accessors
fall back to environment values on DB errors and count each degradation in
``tripl_settings_read_failures_total`` so operators can detect it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl import crypto
from tripl.config import SMTP_SECURITY_NONE, SMTP_SECURITY_STARTTLS, Settings, settings
from tripl.models.app_setting import AI_SETTINGS_KEY, SERVICE_SETTINGS_KEY, AppSetting
from tripl.observability.metrics import settings_read_failures_total
from tripl.services import migration_status_service
from tripl.services.ai_defaults import (
    DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    DEFAULT_ASK_SYSTEM_PROMPT,
    DEFAULT_DESCRIBE_SYSTEM_PROMPT,
)

logger = logging.getLogger(__name__)

# Where the value an owner is looking at actually came from. "default" exists
# because the badge used to assert "env" for every field with no DB override,
# which made it useless as evidence: search_embedding_provider read "Env" on an
# instance that had never been told anything about it (tripl-wkwv.2). See
# _setting_source for what "default" does and does not claim.
SettingSource = Literal["env", "override", "default"]

RUNTIME_FIELDS = (
    "app_base_url",
    "scan_row_limit_default",
    "metrics_row_limit_default",
)
SECURITY_FIELDS = (
    "cors_allow_origins",
    "session_cookie_name",
    "session_ttl_hours",
    "session_cookie_secure",
    "security_headers_enabled",
    "hsts_enabled",
    "hsts_max_age_seconds",
    "content_security_policy",
    "rate_limit_enabled",
    "rate_limit_login_per_minute",
    "rate_limit_register_per_hour",
    "rate_limit_trust_forwarded_for",
    "registration_mode",
)
STORAGE_FIELDS = (
    "photo_storage_backend",
    "photo_local_dir",
    "photo_max_size_mb",
    "photo_allowed_mime",
    "gcs_photo_bucket",
    "gcs_photo_credentials_path",
    "gcs_photo_public",
    "gcs_photo_signed_url_ttl_seconds",
)
OBSERVABILITY_FIELDS = (
    "request_id_header",
    "log_level",
    "log_json",
    "prometheus_metrics_enabled",
    "otel_exporter_otlp_endpoint",
    "otel_service_name",
)
EMAIL_FIELDS = (
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    # Replaces the old ``smtp_use_tls`` boolean, which could not express
    # implicit TLS and so left a 465 relay unreachable (tripl-x1vk). The boolean
    # survives as a deprecated ENV default only — it is deliberately absent
    # here, so it is neither reported nor editable and exactly one field decides
    # the transport. Stored overrides carrying the old key are rewritten by
    # migration a3f7c21e9b64.
    "smtp_security",
    "smtp_from_address",
)
AI_FIELDS = (
    "ai_enabled",
    "ai_base_url",
    "ai_model",
    "ai_api_key",
    "ai_timeout_seconds",
    "ai_max_output_tokens",
    "describe_system_prompt",
    "ask_system_prompt",
    "alert_explanation_system_prompt",
    "search_embeddings_enabled",
    "search_embedding_provider",
    "search_embedding_model",
    "search_embedding_api_key",
)

FIELD_SECTIONS: dict[str, tuple[str, ...]] = {
    "runtime": RUNTIME_FIELDS,
    "security": SECURITY_FIELDS,
    "storage": STORAGE_FIELDS,
    "observability": OBSERVABILITY_FIELDS,
    "email": EMAIL_FIELDS,
    "ai": AI_FIELDS,
}
# AI fields that are REPORTED but can never be edited. Deliberately declared
# OUTSIDE FIELD_SECTIONS, because EDITABLE_FIELDS is derived from it just below
# and ``update_service_overrides`` gates on that set — listing either field there
# would make it persistable in the same edit. Both describe the vector space
# every row already in pgvector was written into: repointing the endpoint or
# resizing the width makes similarity against older vectors meaningless, with no
# error anywhere. They still get a ``sources`` entry, because "which endpoint is
# the indexed plan text going to" is the question the AI section exists to
# answer, and nothing in the running system answered it (tripl-wkwv.2).
READ_ONLY_ENV_FIELDS: tuple[str, ...] = (
    "search_embedding_base_url",
    "search_embedding_dimensions",
)
EDITABLE_FIELDS = frozenset(
    field for section_fields in FIELD_SECTIONS.values() for field in section_fields
)
SECRET_FIELDS = frozenset({"ai_api_key", "search_embedding_api_key", "smtp_password"})
AI_SECRET_FIELDS = frozenset({"ai_api_key", "search_embedding_api_key"})

# The fields only a PLATFORM admin may change (F20 PR4, design section 5
# "Operator-only fields"). They configure the process and the host rather than an
# organization: the public URL, every security and observability knob (including
# who may register at all), the server filesystem paths (critique 12) and the
# photo size cap, which is the process request-body limit (critique 15).
#
# Until PR9 gives each organization its own values, every field here is ONE
# value shared by every organization, so the fields that route another
# organization's data somewhere are operator-only too: where photos are stored
# and whether they are public (every storage field but the MIME allow-list),
# the SMTP relay that carries every user's password-reset and invitation mail,
# and the AI / embedding endpoint and keys that receive every organization's
# plan text. What is left (row limits, the MIME allow-list, AI model, limits
# and prompts) is open to the settings admins (a platform admin, or an
# owner/admin of the default organization). ``api.v1.app_settings`` refuses a
# write touching any of these unless the caller is a platform admin.
OPERATOR_FIELDS: frozenset[str] = frozenset(
    {
        "app_base_url",
        *SECURITY_FIELDS,
        *OBSERVABILITY_FIELDS,
        *(field for field in STORAGE_FIELDS if field != "photo_allowed_mime"),
        *EMAIL_FIELDS,
        "ai_base_url",
        "ai_api_key",
        "search_embedding_provider",
        "search_embedding_model",
        "search_embedding_api_key",
    }
)


def touches_operator_fields(changes: Mapping[str, Any]) -> bool:
    """Whether a flattened settings write sets any :data:`OPERATOR_FIELDS` field."""
    return not OPERATOR_FIELDS.isdisjoint(changes)


@dataclass(frozen=True)
class RuntimeConfig:
    app_base_url: str
    scan_row_limit_default: int
    metrics_row_limit_default: int


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    # One of config.SMTP_SECURITY_MODES. A string rather than a bool because
    # the three transports are not orderable: implicit TLS is not "more" than
    # STARTTLS, it is a different conversation from the first byte.
    smtp_security: str
    smtp_from_address: str


@dataclass(frozen=True)
class AiConfig:
    """Effective AI configuration used by LLM and embedding callers.

    API keys are already resolved (field-specific override -> field-specific
    env -> ``OPENAI_API_KEY`` env fallback) and decrypted.
    """

    ai_enabled: bool
    ai_base_url: str
    ai_model: str
    ai_api_key: str
    ai_timeout_seconds: int
    ai_max_output_tokens: int
    describe_system_prompt: str
    ask_system_prompt: str
    alert_explanation_system_prompt: str
    search_embeddings_enabled: bool
    search_embedding_provider: str
    search_embedding_model: str
    search_embedding_api_key: str


AI_CONFIG_FIELDS = frozenset(f.name for f in fields(AiConfig))


def default_ai_prompts() -> dict[str, str]:
    return {
        "describe_system_prompt": DEFAULT_DESCRIBE_SYSTEM_PROMPT,
        "ask_system_prompt": DEFAULT_ASK_SYSTEM_PROMPT,
        "alert_explanation_system_prompt": DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    }


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
    }
    # Last, and unconditionally: a startup-applied override is still an override,
    # and ``build_service_values`` writes it back over this a moment later. What
    # this restores is the answer for a field whose override was CLEARED.
    values.update(_ENV_BEFORE_STARTUP_APPLY)
    return values


def env_ai_config() -> AiConfig:
    return build_ai_config({})


def env_email_config() -> EmailConfig:
    return build_email_config({})


def env_runtime_config() -> RuntimeConfig:
    return build_runtime_config({})


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


def build_runtime_config(overrides: dict[str, Any]) -> RuntimeConfig:
    values = build_service_values(overrides)
    return RuntimeConfig(
        app_base_url=str(values["app_base_url"]),
        scan_row_limit_default=int(values["scan_row_limit_default"]),
        metrics_row_limit_default=int(values["metrics_row_limit_default"]),
    )


def build_email_config(overrides: dict[str, Any]) -> EmailConfig:
    values = build_service_values(overrides)
    return EmailConfig(
        smtp_host=str(values["smtp_host"]),
        smtp_port=int(values["smtp_port"]),
        smtp_username=str(values["smtp_username"]),
        smtp_password=str(values["smtp_password"]),
        smtp_security=str(values["smtp_security"]),
        smtp_from_address=str(values["smtp_from_address"]),
    )


def build_ai_config(overrides: dict[str, Any]) -> AiConfig:
    values = build_service_values(overrides)
    return AiConfig(
        ai_enabled=bool(values["ai_enabled"]),
        ai_base_url=str(values["ai_base_url"]),
        ai_model=str(values["ai_model"]),
        ai_api_key=str(values["ai_api_key"]),
        ai_timeout_seconds=int(values["ai_timeout_seconds"]),
        ai_max_output_tokens=int(values["ai_max_output_tokens"]),
        describe_system_prompt=str(values["describe_system_prompt"]),
        ask_system_prompt=str(values["ask_system_prompt"]),
        alert_explanation_system_prompt=str(values["alert_explanation_system_prompt"]),
        search_embeddings_enabled=bool(values["search_embeddings_enabled"]),
        search_embedding_provider=str(values["search_embedding_provider"]),
        search_embedding_model=str(values["search_embedding_model"]),
        search_embedding_api_key=str(values["search_embedding_api_key"]),
    )


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


def email_can_send(email_config: EmailConfig) -> bool:
    """Whether this instance can actually deliver mail.

    Both the host and a From: address, not just the host: the password-reset
    sender returns without sending when there is no From: address, so a relay
    with no sender would mint a token, drop the mail, and still have the UI
    promise a link was on its way.
    """
    return bool(email_config.smtp_host and email_config.smtp_from_address)


def ai_prompt_defaults() -> dict[str, str]:
    """The built-in system prompts, before any stored override (ST-30)."""
    return {
        "describe_system_prompt": DEFAULT_DESCRIBE_SYSTEM_PROMPT,
        "ask_system_prompt": DEFAULT_ASK_SYSTEM_PROMPT,
        "alert_explanation_system_prompt": DEFAULT_ALERT_EXPLANATION_SYSTEM_PROMPT,
    }


async def get_row_limit_defaults(session: AsyncSession) -> RuntimeConfig:
    """The effective instance row caps: env values with stored overrides on top."""
    return build_runtime_config(await get_service_overrides(session))


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


async def get_ai_overrides(session: AsyncSession) -> dict[str, Any]:
    return {
        key: value
        for key, value in (await get_service_overrides(session)).items()
        if key in AI_CONFIG_FIELDS
    }


def get_ai_overrides_sync(session: Session) -> dict[str, Any]:
    return {
        key: value
        for key, value in get_service_overrides_sync(session).items()
        if key in AI_CONFIG_FIELDS
    }


async def get_ai_config(session: AsyncSession) -> AiConfig:
    return build_ai_config(await get_service_overrides(session))


async def get_email_config(session: AsyncSession) -> EmailConfig:
    """The async twin of ``get_email_config_sync``, for request-path callers."""
    return build_email_config(await get_service_overrides(session))


async def get_service_settings(session: AsyncSession) -> dict[str, Any]:
    return await service_settings_payload(session, await get_service_overrides(session))


def get_ai_config_sync(session: Session | None = None) -> AiConfig:
    try:
        if session is not None:
            return build_ai_config(get_service_overrides_sync(session))
        from tripl.worker.db import SyncSessionLocal

        with SyncSessionLocal() as own_session:
            return build_ai_config(get_service_overrides_sync(own_session))
    except Exception:  # noqa: BLE001
        logger.warning("Falling back to env AI config: app_settings read failed", exc_info=True)
        settings_read_failures_total.labels(section="ai").inc()
        return env_ai_config()


def get_email_config_sync(session: Session | None = None) -> EmailConfig:
    try:
        if session is not None:
            return build_email_config(get_service_overrides_sync(session))
        from tripl.worker.db import SyncSessionLocal

        with SyncSessionLocal() as own_session:
            return build_email_config(get_service_overrides_sync(own_session))
    except Exception:  # noqa: BLE001
        logger.warning("Falling back to env email config: app_settings read failed", exc_info=True)
        settings_read_failures_total.labels(section="email").inc()
        return env_email_config()


def get_runtime_config_sync(session: Session | None = None) -> RuntimeConfig:
    try:
        if session is not None:
            return build_runtime_config(get_service_overrides_sync(session))
        from tripl.worker.db import SyncSessionLocal

        with SyncSessionLocal() as own_session:
            return build_runtime_config(get_service_overrides_sync(own_session))
    except Exception:  # noqa: BLE001
        logger.warning(
            "Falling back to env runtime config: app_settings read failed", exc_info=True
        )
        settings_read_failures_total.labels(section="runtime").inc()
        return env_runtime_config()


# Fields excluded from the startup apply below because they are resolved live
# per request instead. ``registration_mode`` gates POST /auth/register, and an
# owner closing registration on a public instance must take effect on the very
# next request — "on the next deploy" is not an acceptable latency for shutting
# a door. See ``get_registration_mode``.
LIVE_APPLIED_FIELDS: frozenset[str] = frozenset({"registration_mode"})

# Sections whose values are read directly off the ``settings`` singleton by
# import-time consumers — the middleware stack, auth rate limiters, logging
# config and the /metrics route. Unlike runtime/email/ai (consumed through
# build_*_config at request/task time), their overrides only take effect when
# applied back onto ``settings`` at process startup. None of these fields are
# secrets (SECRET_FIELDS are ai/smtp only), so no decryption is needed here.
STARTUP_APPLIED_FIELDS: tuple[str, ...] = tuple(
    field
    for field in (*SECURITY_FIELDS, *STORAGE_FIELDS, *OBSERVABILITY_FIELDS)
    if field not in LIVE_APPLIED_FIELDS
)


async def get_registration_mode(session: AsyncSession) -> str:
    """Effective self-service registration mode (DB override -> env).

    Read per request rather than pinned onto ``settings`` at startup, so an
    owner toggling registration in Settings -> Security closes (or reopens) the
    door immediately. Returns one of :data:`tripl.config.REGISTRATION_MODES`.
    """
    values = build_service_values(await get_service_overrides(session))
    return str(values["registration_mode"])


def apply_startup_service_overrides(session: Session | None = None) -> list[str]:
    """Apply persisted Security/Storage/Observability overrides onto ``settings``.

    Must run at the very top of the API and worker entry modules, before the
    middleware, rate limiters, logging and metrics route are wired from
    ``settings`` — those read it once at import, so a later apply would be too
    late. This is what makes these overrides "take effect on the next deploy",
    as the admin settings UI states.

    Degrades to a no-op (env-only config) when the DB is unreachable, so simply
    importing the app never fails because overrides cannot be read. Returns the
    list of fields actually applied (for logging and tests).
    """
    try:
        if session is not None:
            overrides = get_service_overrides_sync(session)
        else:
            from tripl.worker.db import SyncSessionLocal

            with SyncSessionLocal() as own_session:
                overrides = get_service_overrides_sync(own_session)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Startup service-override apply skipped: app_settings read failed",
            exc_info=True,
        )
        return []

    candidate_values = {
        field: overrides[field]
        for field in STARTUP_APPLIED_FIELDS
        if overrides.get(field) is not None and hasattr(settings, field)
    }
    try:
        validated = Settings.model_validate({**settings.model_dump(), **candidate_values})
    except ValidationError as exc:
        logger.error("Ignoring invalid stored service overrides: %s", exc)
        return []

    applied: list[str] = []
    previous: dict[str, Any] = {}
    for field in STARTUP_APPLIED_FIELDS:
        value = overrides.get(field)
        if value is None or not hasattr(settings, field):
            continue
        previous[field] = getattr(settings, field)
        setattr(settings, field, getattr(validated, field))
        applied.append(field)

    # A stored override must never be the reason the process cannot boot.
    #
    # These fields land on ``settings`` here and ``assert_production_ready`` runs
    # a few lines later in main.py. Unticking "Secure cookie" (or setting CORS to
    # "*") under Settings -> Instance -> Security therefore bricked the instance
    # on the NEXT restart — and because the UI that set it lives in the process
    # that now refuses to start, the only way out was hand-editing app_settings
    # in Postgres (tripl-jfm3.93). Falling back to env-only config keeps the
    # instance reachable so the operator can undo the change where they made it.
    if applied and not settings.debug:
        with_overrides = settings.production_problems()
        if with_overrides:
            # Compare against env-only config: a deployment that was already
            # unready must still fail on its own merits, with the operator's
            # intended settings in place. Only roll back what the OVERRIDES break.
            for field, value in previous.items():
                setattr(settings, field, value)
            introduced = [p for p in with_overrides if p not in settings.production_problems()]
            if not introduced:
                for field in applied:
                    setattr(settings, field, getattr(validated, field))
            else:
                logger.error(
                    "Ignoring %d stored service override(s) — applying them would stop "
                    "this process from starting. Falling back to environment "
                    "configuration; correct or clear them under Settings -> Instance. "
                    "Fields: %s. Problems: %s",
                    len(applied),
                    ", ".join(sorted(applied)),
                    "; ".join(introduced),
                )
                return []

    if applied:
        # This apply is permanent for the life of the process — the only restore
        # is the rollback branch above, which returns [] before reaching here —
        # so ``settings`` has now lost what the environment delivered for these
        # fields. Keep it, or clearing the override later reports the deleted
        # value and badges it "Env" (tripl-wkwv.2).
        #
        # ``setdefault``, not ``update``: only the FIRST apply in a process saw
        # the pre-override value, so a repeat call must not record what it wrote.
        for field, env_value in previous.items():
            _ENV_BEFORE_STARTUP_APPLY.setdefault(field, env_value)
        logger.info("Applied %d service override(s) onto settings at startup", len(applied))
    return applied


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


async def update_service_overrides(
    session: AsyncSession,
    changes: dict[str, Any],
) -> dict[str, Any]:
    row = await session.scalar(_operator_setting(SERVICE_SETTINGS_KEY))
    overrides: dict[str, Any] = (
        dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    )

    for key, value in changes.items():
        if key not in EDITABLE_FIELDS:
            continue
        if value is None or (key in SECRET_FIELDS and value == ""):
            overrides.pop(key, None)
            continue
        overrides[key] = crypto.encrypt_value(str(value)) if key in SECRET_FIELDS else value

    _reject_startup_breaking_overrides(overrides)

    if row is None:
        row = AppSetting(key=SERVICE_SETTINGS_KEY, value=overrides, organization_id=None)
        session.add(row)
    else:
        row.value = overrides

    # If a partial local DB has the first-cut key="ai" row, make resets behave
    # predictably by removing touched AI fields from that legacy document.
    legacy_row = await session.scalar(_operator_setting(AI_SETTINGS_KEY))
    if legacy_row is not None and isinstance(legacy_row.value, dict):
        legacy = dict(legacy_row.value)
        for key in changes:
            if key in AI_CONFIG_FIELDS:
                legacy.pop(key, None)
        legacy_row.value = legacy

    await session.commit()
    return await get_service_overrides(session)


_NO_DEFAULT = object()

# The three system prompts are not ``Settings`` fields at all — env_service_values
# reads them straight off ai_defaults — so no environment variable can deliver
# them and their built-in constant IS the default to compare against.
_PROMPT_DEFAULTS = default_ai_prompts()

# Fields whose REPORTED value is derived from more than one ``Settings`` field,
# so the field's own class default is not what an untouched instance shows.
# ``smtp_security`` defaults to "" meaning "ask the deprecated smtp_use_tls",
# and what reaches the operator is the answer, never the empty string — so
# comparing against "" would badge a fresh instance "Env" and credit a delivery
# that never happened. Derived from the sibling's CLASS default rather than
# written out, so the two cannot drift apart.
_DERIVED_DEFAULTS: dict[str, Any] = {
    "smtp_security": (
        SMTP_SECURITY_STARTTLS
        if Settings.model_fields["smtp_use_tls"].get_default()
        else SMTP_SECURITY_NONE
    ),
}


def _code_default(field: str) -> Any:
    """What this field holds when nothing — no env var, no .env line — delivered it."""
    if field in _DERIVED_DEFAULTS:
        return _DERIVED_DEFAULTS[field]
    # ``model_fields`` is read off the CLASS: instance access is deprecated in
    # pydantic 2.11+ and this project pins 2.13.
    info = Settings.model_fields.get(field)
    if info is not None and not info.is_required():
        return info.get_default(call_default_factory=True)
    return _PROMPT_DEFAULTS.get(field, _NO_DEFAULT)


def _setting_source(field: str, value: Any, overrides: dict[str, Any]) -> SettingSource:
    """Where the value in front of the operator actually came from.

    Note the honest limit of comparing against the built-in default: an operator
    who sets an environment variable to EXACTLY that default is reported as
    "default", because from here the two are indistinguishable. That is the safe
    direction — this never claims a delivery that did not happen, it only
    declines to credit a redundant variable — and the UI's badge says so in as
    many words rather than implying "default" proves nothing arrived.

    A normalising validator can fold a delivered value onto the default the same
    way (``LOG_LEVEL=info`` -> ``"INFO"``), with the same consequence.

    ``override`` is checked first on purpose: an override whose value happens to
    equal the default still reads "Override", because a row exists and Reset
    will clear it.
    """
    if field in overrides:
        return "override"
    default = _code_default(field)
    # ``type(value) is type(default)`` keeps Python's ``False == 0`` from matching
    # a bool field against an int default.
    if default is not _NO_DEFAULT and type(value) is type(default) and value == default:
        return "default"
    return "env"


async def service_settings_payload(
    session: AsyncSession, overrides: dict[str, Any]
) -> dict[str, Any]:
    """Public settings for already-read overrides, plus live migration status.

    Every router path goes through here so GET and PATCH answer identically: the
    frontend writes the PATCH response straight into its query cache, so a field
    present on one and absent from the other blanks out on the next save.
    """
    return public_service_settings(
        overrides, await migration_status_service.get_migration_status(session)
    )


def public_service_settings(
    overrides: dict[str, Any], migration: migration_status_service.MigrationStatus
) -> dict[str, Any]:
    values = build_service_values(overrides)
    overridden_fields = sorted(key for key in overrides if key in EDITABLE_FIELDS)
    sources: dict[str, SettingSource] = {}
    for section, section_fields in FIELD_SECTIONS.items():
        for field in section_fields:
            sources[f"{section}.{field}"] = _setting_source(field, values[field], overrides)
    # No override can exist for these — they are outside EDITABLE_FIELDS — so the
    # only question they can answer is delivered-versus-default, which is exactly
    # the one an operator verifying SEARCH_EMBEDDING_BASE_URL is asking.
    for field in READ_ONLY_ENV_FIELDS:
        sources[f"ai.{field}"] = _setting_source(field, getattr(settings, field), {})

    return {
        "runtime": {
            "app_base_url": values["app_base_url"],
            "scan_row_limit_default": values["scan_row_limit_default"],
            "metrics_row_limit_default": values["metrics_row_limit_default"],
        },
        "security": {
            "cors_allow_origins": values["cors_allow_origins"],
            "session_cookie_name": values["session_cookie_name"],
            "session_ttl_hours": values["session_ttl_hours"],
            "session_cookie_secure": values["session_cookie_secure"],
            "security_headers_enabled": values["security_headers_enabled"],
            "hsts_enabled": values["hsts_enabled"],
            "hsts_max_age_seconds": values["hsts_max_age_seconds"],
            "content_security_policy": values["content_security_policy"],
            "rate_limit_enabled": values["rate_limit_enabled"],
            "rate_limit_login_per_minute": values["rate_limit_login_per_minute"],
            "rate_limit_register_per_hour": values["rate_limit_register_per_hour"],
            "rate_limit_trust_forwarded_for": values["rate_limit_trust_forwarded_for"],
            "registration_mode": values["registration_mode"],
        },
        "storage": {
            "photo_storage_backend": values["photo_storage_backend"],
            "photo_local_dir": values["photo_local_dir"],
            "photo_max_size_mb": values["photo_max_size_mb"],
            "photo_allowed_mime": values["photo_allowed_mime"],
            "gcs_photo_bucket": values["gcs_photo_bucket"],
            "gcs_photo_credentials_path": values["gcs_photo_credentials_path"],
            "gcs_photo_public": values["gcs_photo_public"],
            "gcs_photo_signed_url_ttl_seconds": values["gcs_photo_signed_url_ttl_seconds"],
        },
        "observability": {
            "request_id_header": values["request_id_header"],
            "log_level": values["log_level"],
            "log_json": values["log_json"],
            "prometheus_metrics_enabled": values["prometheus_metrics_enabled"],
            "otel_exporter_otlp_endpoint": values["otel_exporter_otlp_endpoint"],
            "otel_service_name": values["otel_service_name"],
        },
        "email": {
            "smtp_host": values["smtp_host"],
            "smtp_port": values["smtp_port"],
            "smtp_username": values["smtp_username"],
            "smtp_password_configured": bool(values["smtp_password"]),
            "smtp_security": values["smtp_security"],
            "smtp_from_address": values["smtp_from_address"],
        },
        "ai": {
            "ai_enabled": values["ai_enabled"],
            "ai_base_url": values["ai_base_url"],
            "ai_model": values["ai_model"],
            "ai_api_key_configured": bool(values["ai_api_key"]),
            "ai_timeout_seconds": values["ai_timeout_seconds"],
            "ai_max_output_tokens": values["ai_max_output_tokens"],
            "describe_system_prompt": values["describe_system_prompt"],
            "ask_system_prompt": values["ask_system_prompt"],
            "alert_explanation_system_prompt": values["alert_explanation_system_prompt"],
            "search_embeddings_enabled": values["search_embeddings_enabled"],
            "search_embedding_provider": values["search_embedding_provider"],
            "search_embedding_model": values["search_embedding_model"],
            "search_embedding_api_key_configured": bool(values["search_embedding_api_key"]),
            "search_embedding_dimensions": settings.search_embedding_dimensions,
            "search_embedding_base_url": settings.search_embedding_base_url,
        },
        "system": {
            "debug": settings.debug,
            "database_url_configured": bool(settings.database_url),
            "sync_database_url_configured": bool(settings.sync_database_url),
            "rabbitmq_url_configured": bool(settings.rabbitmq_url),
            "redis_url_configured": bool(settings.redis_url),
            "encryption_key_configured": bool(settings.encryption_key),
            "openai_api_key_configured": bool(settings.openai_api_key),
            "alembic_revision": migration.applied_revision,
            "alembic_head_revision": migration.head_revision,
            "alembic_up_to_date": migration.up_to_date,
        },
        "overridden_fields": overridden_fields,
        "sources": sources,
    }
