from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, EmailStr, Field, field_validator

from tripl.alerting_validation import validate_sender_address
from tripl.config import validate_csp, validate_http_token

# Mirrors app_settings_service.SettingSource. "default" means the value equals
# the built-in default — either nothing was delivered for it, or what was
# delivered matches it; the two are indistinguishable from here (tripl-wkwv.2).
SettingSource = Literal["env", "override", "default"]
# Self-service registration policy. "open" lets anyone reaching the instance
# create an account; "disabled" refuses new signups (the first-owner bootstrap
# on an empty instance stays exempt so a fresh install can still be claimed).
RegistrationMode = Literal["open", "disabled"]
# How the SMTP client secures the connection: plaintext, upgrade-after-greeting
# (STARTTLS, the port-587 convention), or TLS from the first byte (SMTPS, what
# port 465 means). Mirrors config.SMTP_SECURITY_MODES, and a test pins the two
# lists equal — a value this type accepts but the transport cannot dispatch on
# would be stored happily and fail only at send time.
SmtpSecurity = Literal["none", "starttls", "implicit_tls"]


class RuntimeSettings(BaseModel):
    app_base_url: str
    scan_row_limit_default: int
    metrics_row_limit_default: int


class RuntimeSettingsUpdate(BaseModel):
    app_base_url: str | None = None
    scan_row_limit_default: int | None = Field(default=None, ge=1)
    metrics_row_limit_default: int | None = Field(default=None, ge=1)


class SecuritySettings(BaseModel):
    cors_allow_origins: str
    session_cookie_name: str
    session_ttl_hours: int
    session_cookie_secure: bool
    security_headers_enabled: bool
    hsts_enabled: bool
    hsts_max_age_seconds: int
    content_security_policy: str
    rate_limit_enabled: bool
    rate_limit_login_per_minute: int
    rate_limit_register_per_hour: int
    rate_limit_trust_forwarded_for: bool
    registration_mode: RegistrationMode


class SecuritySettingsUpdate(BaseModel):
    cors_allow_origins: str | None = None
    session_cookie_name: str | None = Field(default=None, min_length=1, max_length=100)
    session_ttl_hours: int | None = Field(default=None, ge=1)
    session_cookie_secure: bool | None = None
    security_headers_enabled: bool | None = None
    hsts_enabled: bool | None = None
    hsts_max_age_seconds: int | None = Field(default=None, ge=0)
    content_security_policy: str | None = None
    rate_limit_enabled: bool | None = None
    rate_limit_login_per_minute: int | None = Field(default=None, ge=0)
    rate_limit_register_per_hour: int | None = Field(default=None, ge=0)
    rate_limit_trust_forwarded_for: bool | None = None
    registration_mode: RegistrationMode | None = None

    @field_validator("session_cookie_name")
    @classmethod
    def _check_cookie_name(cls, value: str | None) -> str | None:
        return validate_http_token(value) if value is not None else None

    @field_validator("content_security_policy")
    @classmethod
    def _check_csp(cls, value: str | None) -> str | None:
        return validate_csp(value) if value is not None else None


class StorageSettings(BaseModel):
    photo_storage_backend: str
    photo_local_dir: str
    photo_max_size_mb: int
    photo_allowed_mime: str
    gcs_photo_bucket: str
    gcs_photo_credentials_path: str
    gcs_photo_public: bool
    gcs_photo_signed_url_ttl_seconds: int


class StorageSettingsUpdate(BaseModel):
    photo_storage_backend: str | None = None
    photo_local_dir: str | None = None
    photo_max_size_mb: int | None = Field(default=None, ge=1)
    photo_allowed_mime: str | None = None
    gcs_photo_bucket: str | None = None
    gcs_photo_credentials_path: str | None = None
    gcs_photo_public: bool | None = None
    gcs_photo_signed_url_ttl_seconds: int | None = Field(default=None, ge=1)


class ObservabilitySettings(BaseModel):
    request_id_header: str
    log_level: str
    log_json: bool
    prometheus_metrics_enabled: bool
    otel_exporter_otlp_endpoint: str
    otel_service_name: str


class ObservabilitySettingsUpdate(BaseModel):
    request_id_header: str | None = Field(default=None, min_length=1, max_length=100)
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] | None = None
    log_json: bool | None = None
    prometheus_metrics_enabled: bool | None = None
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("request_id_header")
    @classmethod
    def _check_header_name(cls, value: str | None) -> str | None:
        return validate_http_token(value) if value is not None else None


class EmailSettings(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password_configured: bool
    smtp_security: SmtpSecurity
    smtp_from_address: str


class EmailSettingsUpdate(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: str | None = Field(default=None, max_length=4096)
    smtp_security: SmtpSecurity | None = None
    smtp_from_address: str | None = None

    @field_validator("smtp_from_address")
    @classmethod
    def _check_smtp_from_address(cls, value: str | None) -> str | None:
        # The global Default From every email destination without an override
        # falls back to, and until now the only email setting nothing checked:
        # ``app_settings_service.update_service_overrides`` writes whatever
        # arrives. A typo was therefore reported at 10:00 the next morning by a
        # failed alert rather than by the form that accepted it.
        #
        # Checked with the SEND PATH's own helper, so what this endpoint accepts
        # is what the alert tasks accept. Deliberately NOT ``EmailStr`` or
        # ``validate_email_address``: the strict form refuses
        # ``Tripl Alerts <no-reply@example.com>``, which the From: header takes
        # happily, and putting it here would rebuild tripl-0zpq.29 at the other
        # end of the same pipe — a value the operator can never save, instead of
        # one they can save but never deliver.
        #
        # None and "" pass through untouched: they are how the value is CLEARED
        # (``update_service_overrides`` drops a None and stores an empty string),
        # and "no Default From configured" is a supported state — the one
        # Settings → Send test email reports on rather than refuses.
        #
        # A value that is only whitespace is FOLDED INTO that empty string
        # rather than refused. It is the same intent typed differently — an
        # operator clearing the field with a space means "not configured" — and
        # a 422 there would refuse a state the product supports while leaving
        # the previous Default From in place, still sending.
        #
        # Folding is what keeps "not configured" a single FALSY value, which is
        # the only form its readers recognise. All three ask the question with
        # ``not``/``or``: ``alerts._resolve_email_context`` resolves
        # ``destination.email_from_address or email_config.smtp_from_address``
        # and names the unset setting only when that is falsy,
        # ``_email_test_send.send_test_email`` refuses on ``not
        # smtp_from_address`` with the sentence about dropped reset mail, and
        # ``api/v1/auth.py`` computes ``email_configured`` — the flag deciding
        # whether a reset token is minted at all — the same way. A stored "   "
        # is truthy in all three, so it is not the cleared state but a fourth
        # one nothing handles: with no per-destination override the alert fails
        # at send with "From: address is invalid", the probe answers "Not a
        # usable From: address: '   '" instead of naming the setting that is
        # unset, and the reset flow mints a token and hands SMTP a blank From:.
        if value is None:
            return value
        if not value.strip():
            return ""
        return validate_sender_address(value)


class AiSettings(BaseModel):
    ai_enabled: bool
    ai_base_url: str
    ai_model: str
    ai_api_key_configured: bool
    ai_timeout_seconds: int
    ai_max_output_tokens: int
    describe_system_prompt: str
    ask_system_prompt: str
    alert_explanation_system_prompt: str
    search_embeddings_enabled: bool
    search_embedding_provider: str
    search_embedding_model: str
    search_embedding_api_key_configured: bool
    search_embedding_dimensions: int
    # Reported, never accepted: absent from AiSettingsUpdate below AND from
    # EDITABLE_FIELDS, so a body carrying it is dropped twice over. Repointing
    # the endpoint at runtime would silently poison every vector already in the
    # index; leaving it invisible turned a compose allowlist slip into an
    # unnoticed change of where plan text is sent (tripl-wkwv.2).
    search_embedding_base_url: str


class AiSettingsUpdate(BaseModel):
    ai_enabled: bool | None = None
    ai_base_url: str | None = None
    ai_model: str | None = None
    ai_api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("ai_base_url")
    @classmethod
    def _check_ai_base_url(cls, value: str | None) -> str | None:
        # Format-only guard: must be a well-formed http(s) URL with a host.
        # We intentionally allow http and private/localhost hosts so that
        # self-hosted/local LLM endpoints (e.g. http://localhost:11434) work.
        if value is None:
            return value
        trimmed = value.strip()
        if not trimmed:
            return trimmed
        parsed = urlparse(trimmed)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("ai_base_url must be a valid http(s) URL")
        return trimmed

    ai_timeout_seconds: int | None = Field(default=None, ge=1)
    ai_max_output_tokens: int | None = Field(default=None, ge=1)
    describe_system_prompt: str | None = Field(default=None, min_length=1)
    ask_system_prompt: str | None = Field(default=None, min_length=1)
    alert_explanation_system_prompt: str | None = Field(default=None, min_length=1)
    search_embeddings_enabled: bool | None = None
    search_embedding_provider: str | None = None
    search_embedding_model: str | None = None
    search_embedding_api_key: str | None = Field(default=None, max_length=4096)


class SystemSettings(BaseModel):
    debug: bool
    database_url_configured: bool
    sync_database_url_configured: bool
    rabbitmq_url_configured: bool
    redis_url_configured: bool
    encryption_key_configured: bool
    openai_api_key_configured: bool
    # The one part of this section read from the database rather than the
    # process environment. Named ``alembic_*`` because that is the word the
    # runbook, the deployment docs and the table itself already use, so it is
    # what an operator greps for. All three are nullable: a database that is
    # unreachable, or has never been stamped, degrades to an honest unknown
    # rather than a guess (tripl-wkwv.7).
    alembic_revision: str | None = None
    alembic_head_revision: str | None = None
    #: ``None`` whenever either revision above is unknown. Computed here rather
    #: than derived in the frontend so the tri-state rule lives in one place:
    #: ``applied === head`` in TypeScript quietly answers ``false`` for two
    #: nulls, which would paint an unreadable instance as "migrations skipped".
    alembic_up_to_date: bool | None = None


class ServiceSettingsResponse(BaseModel):
    runtime: RuntimeSettings
    security: SecuritySettings
    storage: StorageSettings
    observability: ObservabilitySettings
    email: EmailSettings
    ai: AiSettings
    # ``None`` for everyone but a platform admin (F20 PR4): the process
    # environment and migration state are operator information.
    system: SystemSettings | None
    overridden_fields: list[str]
    sources: dict[str, SettingSource]


class ServiceSettingsUpdate(BaseModel):
    runtime: RuntimeSettingsUpdate | None = None
    security: SecuritySettingsUpdate | None = None
    storage: StorageSettingsUpdate | None = None
    observability: ObservabilitySettingsUpdate | None = None
    email: EmailSettingsUpdate | None = None
    ai: AiSettingsUpdate | None = None


class AiSettingsResponse(BaseModel):
    ai: AiSettings
    overridden_fields: list[str]
    sources: dict[str, SettingSource]


class AiPromptDefaultsResponse(BaseModel):
    """The built-in system prompts, whatever is stored over them (ST-30).

    A "Restore default" link fills the editor from these; saving ``null`` for
    the field clears the override and has the same effect server-side.
    """

    describe_system_prompt: str
    ask_system_prompt: str
    alert_explanation_system_prompt: str


class RowLimitDefaultsResponse(BaseModel):
    """The instance's effective row caps for a scan with no limit of its own.

    Readable by every signed-in user, unlike the rest of ``/settings``: the scan
    form's Limits hints quote them to whoever is filling it in (B15).
    """

    scan_row_limit_default: int
    metrics_row_limit_default: int


class AiSettingsTestRequest(BaseModel):
    prompt: str = Field(
        default="Reply with the word ok if the LLM connection works.",
        min_length=1,
        max_length=500,
    )


class EmailSettingsTestRequest(BaseModel):
    # Defaults to the caller's own address, resolved in the route. An owner
    # testing a relay almost always wants to mail themselves, and asking for an
    # address before the first probe is a step that answers nothing.
    recipient: EmailStr | None = None


class SettingsTestResponse(BaseModel):
    ok: bool
    message: str
