"""The public payload of a settings resolution (what ``/settings`` answers).

Private half of :mod:`tripl.services.app_settings_service`, which re-exports
every name here; import from the facade.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tripl import __version__
from tripl.config import settings
from tripl.services import migration_status_service
from tripl.services._app_settings_fields import (
    FIELD_SECTIONS,
    READ_ONLY_ENV_FIELDS,
    SettingSource,
)
from tripl.services._app_settings_sources import (
    ResolvedSettings,
    _setting_source,
    resolve_settings,
)


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
    # Here, not at the top: telemetry_service reaches llm_service, which imports
    # the app_settings_service facade this module is part of.
    from tripl.services import telemetry_service

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
            "version": __version__,
            "edition": telemetry_service.edition(),
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
