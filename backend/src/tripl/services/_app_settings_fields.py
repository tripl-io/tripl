"""Field catalogue of the runtime settings (see :mod:`tripl.services.app_settings_service`).

Which fields exist, how they are grouped into sections, which are secret, which
an organization may set for itself and which are applied onto ``settings`` at
startup. Pure constants: no database, no ``settings`` singleton.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

# Where the value an owner is looking at actually came from. "default" exists
# because the badge used to assert "env" for every field with no DB override,
# which made it useless as evidence: search_embedding_provider read "Env" on an
# instance that had never been told anything about it. See
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
    # implicit TLS and so left a 465 relay unreachable. The boolean
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
# answer, and nothing in the running system answered it.
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
REPORTED_FIELDS = EDITABLE_FIELDS | {"search_embedding_base_url", "gcs_photo_credentials_json"}


SECRET_FIELDS = frozenset(
    {"ai_api_key", "search_embedding_api_key", "smtp_password", "gcs_photo_credentials_json"}
)


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


# An organization's photo storage (F20 PR11). The operator's storage section
# minus its two server paths (``photo_local_dir``, ``gcs_photo_credentials_path``:
# letting an organization name a file on the server would let it read the
# operator's credential file or write anywhere, critique #12), plus the one
# field only an organization has: the service-account JSON its own bucket is
# written with, a secret stored encrypted and never returned.
# ``photo_max_size_mb`` is capped by the operator's (it is the request-body
# ceiling ``BodyLimitMiddleware`` enforces before any organization is known)
# and ``photo_allowed_mime`` is narrowed to the operator's allow-list.
STORAGE_ORG_FIELDS: tuple[str, ...] = (
    "photo_storage_backend",
    "photo_max_size_mb",
    "photo_allowed_mime",
    "gcs_photo_bucket",
    "gcs_photo_credentials_json",
    "gcs_photo_public",
    "gcs_photo_signed_url_ttl_seconds",
)


# What an ORGANIZATION may set for itself (F20 PR9-PR11, owner decision 4): mail,
# AI chat, search embeddings, photo storage and the scan/metrics row-limit
# defaults. Resolution
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
        *STORAGE_ORG_FIELDS,
    }
)


# The fields only a PLATFORM admin may change: tripl's own infrastructure. The
# public URL, every security and observability knob (including who may register
# at all) and the two storage server paths (critique 12). Everything editable
# that an organization may not set for itself. The operator's own storage
# values (its bucket, its ceilings) are still written only by a platform admin:
# see ``org_settings_service.OPERATOR_ALIAS_PLATFORM_FIELDS`` and the legacy
# ``/settings`` storage section, which is always the operator's.
OPERATOR_FIELDS: frozenset[str] = EDITABLE_FIELDS - ORG_FIELDS


def touches_operator_fields(changes: Mapping[str, Any]) -> bool:
    """Whether a flattened settings write sets any :data:`OPERATOR_FIELDS` field."""
    return not OPERATOR_FIELDS.isdisjoint(changes)


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
