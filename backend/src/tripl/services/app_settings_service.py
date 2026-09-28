"""Runtime settings stored in the ``app_settings`` table, per scope.

Two scopes (F20 PR9). The OPERATOR scope (``organization_id IS NULL``) stores
explicit overrides of the env-based defaults from :class:`tripl.config.Settings`
for every editable field. An ORGANIZATION scope stores that organization's own
values of the :data:`ORG_FIELDS` (mail, AI chat, search embeddings, row limits).
Resolution:

* operator view: operator override -> env;
* an organization: org override -> operator override -> env, with the
  credential-group, fallback-policy and ceiling rules of
  :mod:`tripl.services._org_settings_merge`. On a self-hosted instance the
  default organization IS the operator scope (:func:`settings_scope_for`).

Secret overrides are encrypted at rest and never returned to clients; clients
only see ``*_configured`` booleans.

Both async (API) and sync (Celery worker) accessors are provided. The operator
scope's sync accessors fall back to environment values on DB errors; an
organization's fail CLOSED (AI and mail off; row limits re-raise) — env holds
the operator's keys, not the organization's (critique #19). Every degradation is
counted in ``tripl_settings_read_failures_total``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from tripl import crypto
from tripl.alerting_validation import reject_private_host
from tripl.config import (
    DEPLOYMENT_SELF_HOSTED,
    SMTP_SECURITY_NONE,
    SMTP_SECURITY_STARTTLS,
    Settings,
    settings,
)
from tripl.models.app_setting import AI_SETTINGS_KEY, SERVICE_SETTINGS_KEY, AppSetting
from tripl.models.organization import DEFAULT_ORG_ID
from tripl.observability.metrics import settings_read_failures_total
from tripl.services import migration_status_service
from tripl.services._org_settings_merge import merge_org_values
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
#
# The organization view (F20 PR9) adds two: "org" is the organization's own
# value, "disabled" a credential group ``ORG_SETTINGS_OPERATOR_FALLBACK=none``
# withholds from an organization that has not set its own. "override" stays
# the OPERATOR's override in every view.
SettingSource = Literal["env", "override", "default", "org", "disabled"]

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
#
# Read-only for the OPERATOR. An organization may still set its own endpoint
# (ORG_FIELDS, F20 PR10): its provenance includes it, so a change re-embeds only
# that organization's rows.
READ_ONLY_ENV_FIELDS: tuple[str, ...] = (
    "search_embedding_base_url",
    "search_embedding_dimensions",
)
EDITABLE_FIELDS = frozenset(
    field for section_fields in FIELD_SECTIONS.values() for field in section_fields
)
#: Every field a resolution reports a value and a source for: the editable ones
#: and the operator's env-only embedding endpoint, which an organization may
#: still set for itself (F20 PR10).
REPORTED_FIELDS = EDITABLE_FIELDS | {"search_embedding_base_url"}
SECRET_FIELDS = frozenset({"ai_api_key", "search_embedding_api_key", "smtp_password"})
AI_SECRET_FIELDS = frozenset({"ai_api_key", "search_embedding_api_key"})

# The chat half of the AI section.
AI_CHAT_FIELDS: tuple[str, ...] = (
    "ai_enabled",
    "ai_base_url",
    "ai_model",
    "ai_api_key",
    "ai_timeout_seconds",
    "ai_max_output_tokens",
    "describe_system_prompt",
    "ask_system_prompt",
    "alert_explanation_system_prompt",
)

# The search-embedding half (F20 PR10): each organization may run its own vector
# space. ``search_embedding_base_url`` is among them although the OPERATOR's
# value stays env-only (READ_ONLY_ENV_FIELDS): an organization's endpoint is
# part of its own provenance, so repointing it re-embeds that organization's
# documents and nobody else's. ``search_embedding_dimensions`` is not: the
# column is vector(1536) for everyone, and an organization's model is checked
# against it with a test embedding when it is saved.
EMBEDDING_FIELDS: tuple[str, ...] = (
    "search_embeddings_enabled",
    "search_embedding_provider",
    "search_embedding_model",
    "search_embedding_base_url",
    "search_embedding_api_key",
)

# What an ORGANIZATION may set for itself (F20 PR9/PR10, owner decision 4): mail,
# AI chat, search embeddings and the scan/metrics row-limit defaults. Resolution
# for an organization is org override -> operator override -> env (see
# ``resolve_settings``), with the credential-group, ceiling and fallback-policy
# rules of
# :mod:`tripl.services._org_settings_merge`.
ORG_FIELDS: frozenset[str] = frozenset(
    {
        "scan_row_limit_default",
        "metrics_row_limit_default",
        *EMAIL_FIELDS,
        *AI_CHAT_FIELDS,
        *EMBEDDING_FIELDS,
    }
)

# The fields only a PLATFORM admin may change: tripl's own infrastructure. The
# public URL, every security and observability knob (including who may register
# at all), storage (server filesystem paths, critique 12, and the photo size cap,
# which is the process request-body limit, critique 15; per-org storage is
# PR11). Everything editable that an organization may not set for itself.
OPERATOR_FIELDS: frozenset[str] = EDITABLE_FIELDS - ORG_FIELDS


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
    #: Where the OpenAI-compatible embeddings endpoint lives: the operator's
    #: ``SEARCH_EMBEDDING_BASE_URL``, or an organization's own (F20 PR10).
    search_embedding_base_url: str
    # True when ``ai_base_url`` came from an ORGANIZATION (any deployment mode):
    # ``llm_service`` then re-checks the host right before the request
    # (reject_private_host, the DNS-rebinding half of the SSRF guard).
    host_guard: bool = False
    # The same for ``search_embedding_base_url`` and ``embedding_service``.
    embedding_host_guard: bool = False


AI_CONFIG_FIELDS = frozenset(f.name for f in fields(AiConfig)) - {
    "host_guard",
    "embedding_host_guard",
}


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
        # Env-only for the operator (READ_ONLY_ENV_FIELDS), an organization field
        # all the same: its value here is what an organization inherits.
        "search_embedding_base_url": settings.search_embedding_base_url,
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
    return _runtime_config_from(build_service_values(overrides))


def build_email_config(overrides: dict[str, Any]) -> EmailConfig:
    return _email_config_from(build_service_values(overrides))


def build_ai_config(overrides: dict[str, Any]) -> AiConfig:
    return _ai_config_from(build_service_values(overrides))


def _runtime_config_from(values: Mapping[str, Any]) -> RuntimeConfig:
    return RuntimeConfig(
        app_base_url=str(values["app_base_url"]),
        scan_row_limit_default=int(values["scan_row_limit_default"]),
        metrics_row_limit_default=int(values["metrics_row_limit_default"]),
    )


def _email_config_from(values: Mapping[str, Any]) -> EmailConfig:
    return EmailConfig(
        smtp_host=str(values["smtp_host"]),
        smtp_port=int(values["smtp_port"]),
        smtp_username=str(values["smtp_username"]),
        smtp_password=str(values["smtp_password"]),
        smtp_security=str(values["smtp_security"]),
        smtp_from_address=str(values["smtp_from_address"]),
    )


def _ai_config_from(
    values: Mapping[str, Any], *, host_guard: bool = False, embedding_host_guard: bool = False
) -> AiConfig:
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
        search_embedding_base_url=str(values["search_embedding_base_url"]),
        host_guard=host_guard,
        embedding_host_guard=embedding_host_guard,
    )


def disabled_ai_config() -> AiConfig:
    """What an organization gets when its settings cannot be read: no AI at all.

    Built from the env values with every credential removed, so nothing in it
    can reach the operator's provider (critique #19: fail closed in org scope).
    """
    return replace(
        env_ai_config(),
        ai_enabled=False,
        ai_api_key="",
        search_embeddings_enabled=False,
        search_embedding_api_key="",
        search_embedding_base_url="",
    )


def disabled_email_config() -> EmailConfig:
    """What an organization gets when its settings cannot be read: no relay."""
    return EmailConfig(
        smtp_host="",
        smtp_port=int(settings.smtp_port),
        smtp_username="",
        smtp_password="",
        smtp_security=settings.resolved_smtp_security(),
        smtp_from_address="",
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


async def get_row_limit_defaults(
    session: AsyncSession, *, org_id: uuid.UUID | None = None
) -> RuntimeConfig:
    """The effective row caps: the organization's (clamped), else operator, else env.

    ``org_id`` may be omitted: row limits are not secrets, so an unscoped read
    only answers with the operator's caps.
    """
    return _runtime_config_from((await resolve_for_org(session, org_id)).values)


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


@dataclass(frozen=True)
class ResolvedSettings:
    """Every editable field's effective value, and where each one came from."""

    values: dict[str, Any]
    sources: dict[str, SettingSource]
    #: ``None`` for the operator view; else the scope that was resolved.
    org_scope: uuid.UUID | None
    #: The raw overrides of the scope being viewed (operator or organization).
    overridden_fields: tuple[str, ...]
    #: Guarded host fields whose value an ORGANIZATION supplied (every organization
    #: scope, any deployment mode); checked against private addresses at use time.
    guarded_hosts: frozenset[str]


def resolve_settings(
    operator_overrides: dict[str, Any],
    org_overrides: dict[str, Any] | None,
    *,
    org_scope: uuid.UUID | None = None,
) -> ResolvedSettings:
    """Pure resolution: operator view when ``org_overrides`` is ``None``, else the org's.

    The organization view applies org override -> operator override -> env with
    the rules of :mod:`tripl.services._org_settings_merge`.
    """
    base = build_service_values(operator_overrides)
    base_sources: dict[str, SettingSource] = {
        field: _setting_source(field, base[field], operator_overrides) for field in REPORTED_FIELDS
    }
    if org_overrides is None:
        return ResolvedSettings(
            values=base,
            sources=base_sources,
            org_scope=None,
            overridden_fields=tuple(sorted(k for k in operator_overrides if k in EDITABLE_FIELDS)),
            guarded_hosts=frozenset(),
        )
    merged = merge_org_values(
        base,
        _org_values(org_overrides),
        org_fields=ORG_FIELDS,
        secret_fields=SECRET_FIELDS,
        inherit_groups=settings.org_settings_operator_fallback == "all",
        code_default=_group_default,
    )
    sources = dict(base_sources)
    for field, provenance in merged.provenance.items():
        if provenance == "org":
            sources[field] = "org"
        elif provenance == "group_default":
            sources[field] = "default"
        elif provenance == "disabled":
            sources[field] = "disabled"
    return ResolvedSettings(
        values=merged.values,
        sources=sources,
        org_scope=org_scope,
        overridden_fields=tuple(sorted(k for k in org_overrides if k in ORG_FIELDS)),
        # Every organization scope (F20 PR9): on a self-hosted instance the
        # operator's own team is the default organization, which resolves as
        # the operator view above and is never guarded.
        guarded_hosts=merged.org_hosts,
    )


def _group_default(field: str) -> Any:
    default = _code_default(field)
    return "" if default is _NO_DEFAULT else default


async def resolve_for_org(session: AsyncSession, org_id: uuid.UUID | None) -> ResolvedSettings:
    """The effective settings an organization runs with (``None``: the operator's).

    No env fallback on a database error: the exception propagates, so an
    organization's request fails rather than running on the operator's keys.
    """
    operator = await get_service_overrides(session)
    scope = settings_scope_for(org_id)
    if scope is None:
        return resolve_settings(operator, None)
    return resolve_settings(operator, await get_org_overrides(session, scope), org_scope=scope)


def resolve_for_org_sync(session: Session, org_id: uuid.UUID | None) -> ResolvedSettings:
    operator = get_service_overrides_sync(session)
    scope = settings_scope_for(org_id)
    if scope is None:
        return resolve_settings(operator, None)
    return resolve_settings(operator, get_org_overrides_sync(session, scope), org_scope=scope)


def _guard_smtp_host(resolved: ResolvedSettings, config: EmailConfig) -> EmailConfig:
    """Refuse an organization's private SMTP host (use time, every organization scope).

    Blocking (DNS): async callers run it in a thread. A refused host leaves the
    organization without a relay rather than failing the caller: mail is off,
    which every sender already handles.
    """
    if "smtp_host" not in resolved.guarded_hosts or not config.smtp_host:
        return config
    try:
        reject_private_host(config.smtp_host, field="SMTP host")
    except ValueError:
        logger.warning(
            "Organization %s SMTP host refused by the private-address guard",
            resolved.org_scope,
        )
        return replace(config, smtp_host="", smtp_password="")
    return config


def ai_config_for(resolved: ResolvedSettings) -> AiConfig:
    return _ai_config_from(
        resolved.values,
        host_guard="ai_base_url" in resolved.guarded_hosts,
        embedding_host_guard="search_embedding_base_url" in resolved.guarded_hosts,
    )


def email_config_for(resolved: ResolvedSettings) -> EmailConfig:
    """The email config of a resolution, with the hosted SMTP-host guard applied."""
    return _guard_smtp_host(resolved, _email_config_from(resolved.values))


async def get_ai_config(session: AsyncSession, *, org_id: uuid.UUID | None) -> AiConfig:
    """The AI config an organization's call runs with.

    ``org_id`` is required: ``None`` is the operator's own config and must be
    asked for explicitly, so a call site cannot forget its organization and
    quietly borrow the operator's key.
    """
    return ai_config_for(await resolve_for_org(session, org_id))


async def get_email_config(session: AsyncSession, *, org_id: uuid.UUID | None) -> EmailConfig:
    """The async twin of ``get_email_config_sync``, for request-path callers."""
    resolved = await resolve_for_org(session, org_id)
    if not resolved.guarded_hosts:
        return _email_config_from(resolved.values)
    return await asyncio.to_thread(email_config_for, resolved)


async def get_service_settings(session: AsyncSession) -> dict[str, Any]:
    return await service_settings_payload(session, await get_service_overrides(session))


def _open_sync_session() -> Session:
    from tripl.worker.db import SyncSessionLocal

    return SyncSessionLocal()


def _resolve_sync(session: Session | None, org_id: uuid.UUID | None) -> ResolvedSettings:
    if session is not None:
        return resolve_for_org_sync(session, org_id)
    with _open_sync_session() as own_session:
        return resolve_for_org_sync(own_session, org_id)


def get_ai_config_sync(session: Session | None = None, *, org_id: uuid.UUID | None) -> AiConfig:
    """Sync twin of :func:`get_ai_config` for workers.

    On a read failure the operator scope falls back to env (the operator's own
    configuration either way); an organization scope fails CLOSED with AI off
    (critique #19) — env holds the operator's key, not the organization's.
    """
    try:
        return ai_config_for(_resolve_sync(session, org_id))
    except Exception:  # noqa: BLE001
        settings_read_failures_total.labels(section="ai").inc()
        if settings_scope_for(org_id) is not None:
            logger.warning(
                "AI disabled for org %s: app_settings read failed", org_id, exc_info=True
            )
            return disabled_ai_config()
        logger.warning("Falling back to env AI config: app_settings read failed", exc_info=True)
        return env_ai_config()


def get_email_config_sync(
    session: Session | None = None, *, org_id: uuid.UUID | None
) -> EmailConfig:
    """Sync twin of :func:`get_email_config`; fails closed for an organization."""
    try:
        return email_config_for(_resolve_sync(session, org_id))
    except Exception:  # noqa: BLE001
        settings_read_failures_total.labels(section="email").inc()
        if settings_scope_for(org_id) is not None:
            logger.warning(
                "Email disabled for org %s: app_settings read failed", org_id, exc_info=True
            )
            return disabled_email_config()
        logger.warning("Falling back to env email config: app_settings read failed", exc_info=True)
        return env_email_config()


def get_runtime_config_sync(
    session: Session | None = None, *, org_id: uuid.UUID | None = None
) -> RuntimeConfig:
    """``app_base_url`` (always the operator's) and the row-limit defaults.

    ``org_id`` gives the organization's row limits; omitted, the operator's.
    An organization scope re-raises a read failure instead of using env limits.
    """
    try:
        return _runtime_config_from(_resolve_sync(session, org_id).values)
    except Exception:
        settings_read_failures_total.labels(section="runtime").inc()
        if settings_scope_for(org_id) is not None:
            logger.warning("Row limits for org %s unreadable: app_settings read failed", org_id)
            raise
        logger.warning(
            "Falling back to env runtime config: app_settings read failed", exc_info=True
        )
        return env_runtime_config()


async def get_embedding_config(session: AsyncSession, *, org_id: uuid.UUID | None) -> AiConfig:
    """The config an organization's search embeddings run with (F20 PR10).

    Each organization has its own vector space: its endpoint, provider, model
    and key (one credential group), stamped on its documents through
    ``embedding_service.embedding_provenance``. ``org_id`` is required for the
    same reason as :func:`get_ai_config`. Only the ``search_embedding*``
    fields of the result may be used.
    """
    return await get_ai_config(session, org_id=org_id)


def get_embedding_config_sync(
    session: Session | None = None, *, org_id: uuid.UUID | None
) -> AiConfig:
    """Sync twin of :func:`get_embedding_config` for the search worker; fails closed."""
    return get_ai_config_sync(session, org_id=org_id)


async def project_org_id(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID | None:
    """The organization a project belongs to, read from the database."""
    from tripl.models.project import Project

    org_id: uuid.UUID | None = await session.scalar(
        select(Project.organization_id).where(Project.id == project_id)
    )
    return org_id


async def get_embedding_config_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> AiConfig:
    """The embedding config of the organization owning ``project_id``; off if unknown."""
    org_id = await project_org_id(session, project_id)
    if org_id is None:
        return disabled_ai_config()
    return await get_embedding_config(session, org_id=org_id)


async def get_search_embedding_config(session: AsyncSession, *, project_id: uuid.UUID) -> AiConfig:
    """The embedding config a search query in ``project_id`` runs with.

    Always the PROJECT's organization, read from the database: its key and
    endpoint embed the query, and its provenance is what the query-time
    ``embedding_model`` filter compares stored vectors with, so a query never
    ranks another organization's vector space (F20 PR10). A bound request
    organization that is not the project's fails closed (semantic search off)
    rather than spend either organization's key on the other's corpus.
    """
    from tripl.middleware.org_context import current_org_id

    org_id = await project_org_id(session, project_id)
    if org_id is None:
        return disabled_ai_config()
    bound = current_org_id()
    if bound is not None and bound != org_id:
        return disabled_ai_config()
    return await get_embedding_config(session, org_id=org_id)


def get_embedding_config_for_project_sync(
    session: Session | None, project_id: uuid.UUID
) -> AiConfig:
    """Sync twin of :func:`get_embedding_config_for_project` (worker paths)."""
    return get_ai_config_for_project_sync(session, project_id)


def project_org_id_sync(session: Session | None, project_id: uuid.UUID) -> uuid.UUID | None:
    """The organization a project belongs to, read from the database (worker paths)."""
    from tripl.models.project import Project

    stmt = select(Project.organization_id).where(Project.id == project_id)
    if session is not None:
        return session.scalar(stmt)
    with _open_sync_session() as own_session:
        return own_session.scalar(stmt)


def get_ai_config_for_project_sync(session: Session | None, project_id: uuid.UUID) -> AiConfig:
    """The AI config of the organization owning ``project_id``; AI off if unknown."""
    try:
        org_id = project_org_id_sync(session, project_id)
    except Exception:  # noqa: BLE001
        logger.warning("AI disabled: cannot read the organization of project %s", project_id)
        return disabled_ai_config()
    if org_id is None:
        return disabled_ai_config()
    return get_ai_config_sync(session, org_id=org_id)


def get_email_config_for_project_sync(
    session: Session | None, project_id: uuid.UUID
) -> EmailConfig:
    """The email config of the organization owning ``project_id``; no relay if unknown."""
    try:
        org_id = project_org_id_sync(session, project_id)
    except Exception:  # noqa: BLE001
        logger.warning("Email disabled: cannot read the organization of project %s", project_id)
        return disabled_email_config()
    if org_id is None:
        return disabled_email_config()
    return get_email_config_sync(session, org_id=org_id)


def get_operator_email_config_sync(session: Session | None = None) -> EmailConfig:
    """The operator's relay: account mail (sign-up, password reset, invitations)."""
    return get_email_config_sync(session, org_id=None)


async def get_operator_email_config(session: AsyncSession) -> EmailConfig:
    """The operator's relay, for account mail sent from a request."""
    return await get_email_config(session, org_id=None)


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


async def update_org_overrides(
    session: AsyncSession,
    org_scope: uuid.UUID,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Write an organization's own overrides (ORG_FIELDS only; the caller validates).

    Same sparse semantics as the operator document: ``None`` clears a field, an
    empty secret clears it too, anything else is stored (secrets encrypted).
    Returns the organization's raw overrides after the write.
    """
    row = await session.scalar(_org_setting(SERVICE_SETTINGS_KEY, org_scope))
    overrides = apply_org_override_changes(
        dict(row.value) if row is not None and isinstance(row.value, dict) else {}, changes
    )
    if row is None:
        session.add(
            AppSetting(key=SERVICE_SETTINGS_KEY, value=overrides, organization_id=org_scope)
        )
    else:
        row.value = overrides
    await session.commit()
    return await get_org_overrides(session, org_scope)


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
    """Public operator-scope settings for already-read overrides, plus migration status.

    Every router path goes through here (or :func:`resolved_settings_payload`) so
    GET and PATCH answer identically: the frontend writes the PATCH response
    straight into its query cache, so a field present on one and absent from the
    other blanks out on the next save.
    """
    return await resolved_settings_payload(session, resolve_settings(overrides, None))


async def resolved_settings_payload(
    session: AsyncSession, resolved: ResolvedSettings
) -> dict[str, Any]:
    """The public payload of any resolution: operator, organization or combined."""
    return public_service_settings(
        resolved, await migration_status_service.get_migration_status(session)
    )


def public_service_settings(
    resolved: ResolvedSettings, migration: migration_status_service.MigrationStatus
) -> dict[str, Any]:
    values = resolved.values
    overridden_fields = list(resolved.overridden_fields)
    sources: dict[str, SettingSource] = {}
    for section, section_fields in FIELD_SECTIONS.items():
        for field in section_fields:
            sources[f"{section}.{field}"] = resolved.sources[field]
    # No operator override can exist for these — they are outside EDITABLE_FIELDS
    # — so for the operator the only question they can answer is delivered-
    # versus-default, which is exactly the one an operator verifying
    # SEARCH_EMBEDDING_BASE_URL is asking. An organization's own endpoint
    # (F20 PR10) reads "org" from the resolution.
    for field in READ_ONLY_ENV_FIELDS:
        sources[f"ai.{field}"] = resolved.sources.get(
            field, _setting_source(field, getattr(settings, field), {})
        )

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
            "search_embedding_base_url": values["search_embedding_base_url"],
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
