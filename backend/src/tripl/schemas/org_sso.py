"""Single sign-on (OIDC) request and response bodies (F20, GH #273)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tripl.models.org_sso import DEFAULT_SSO_SCOPES
from tripl.schemas.auth import AuthUserResponse

_SCOPE_TOKEN = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$"
)
#: The TXT record's name is this label in front of the domain.
VERIFICATION_LABEL = "_tripl-verification"
VERIFICATION_PREFIX = "tripl-verification="


def normalize_domain(value: str) -> str:
    """Lowercase, no surrounding space or trailing dot; ``ValueError`` if not a domain."""
    domain = value.strip().lower().rstrip(".")
    if not _DOMAIN.match(domain):
        raise ValueError("Enter a domain name such as example.com")
    return domain


class OrgSsoConfigUpdate(BaseModel):
    """``PUT /orgs/{org}/sso``. ``client_secret`` omitted or null keeps the stored one."""

    model_config = ConfigDict(extra="forbid")

    issuer: str = Field(min_length=1, max_length=512)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str | None = Field(default=None, max_length=4096)
    scopes: str = Field(default=DEFAULT_SSO_SCOPES, max_length=512)
    enabled: bool = False
    sso_required: bool = False

    @field_validator("issuer", "client_id")
    @classmethod
    def _clean(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or any(ch.isspace() or ord(ch) < 32 for ch in stripped):
            raise ValueError("must not be empty or contain whitespace")
        return stripped

    @field_validator("client_secret")
    @classmethod
    def _secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if "\x00" in stripped:
            raise ValueError("must not contain NUL")
        return stripped or None

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: str) -> str:
        tokens = value.split()
        if not all(_SCOPE_TOKEN.match(token) for token in tokens):
            raise ValueError("scopes are space-separated OAuth scope tokens")
        if "openid" not in tokens:
            raise ValueError("scopes must include openid")
        return " ".join(dict.fromkeys(tokens))


class OrgSsoConfigResponse(BaseModel):
    configured: bool
    issuer: str
    client_id: str
    #: The secret itself is write-only.
    client_secret_configured: bool
    scopes: str
    enabled: bool
    sso_required: bool
    #: What to register at the identity provider.
    redirect_uri: str
    #: Where members start signing in.
    login_url: str


class OrgSsoConfigSaved(OrgSsoConfigResponse):
    #: API keys revoked because this save turned "SSO required" on.
    revoked_api_keys: int = 0


class OrgSsoTestResult(BaseModel):
    ok: bool
    #: A short stable code when ``ok`` is false (``idp_issuer_mismatch``, ...).
    error_code: str | None = None
    message: str
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    jwks_uri: str | None = None
    token_endpoint_auth_method: str | None = None


class OrgSsoDomainCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1, max_length=253)

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return normalize_domain(value)


class OrgSsoDomainResponse(BaseModel):
    id: uuid.UUID
    domain: str
    verified: bool
    verified_at: datetime | None
    #: The DNS TXT record the owner publishes to prove the domain.
    txt_record_name: str
    txt_record_value: str
    created_at: datetime


class SsoDiscoverOrg(BaseModel):
    slug: str
    name: str
    login_url: str


class SsoDiscoverResponse(BaseModel):
    orgs: list[SsoDiscoverOrg]


class SsoLinkPreview(BaseModel):
    email: str
    org_slug: str
    org_name: str
    expires_at: datetime
    #: This browser must sign in to the account (password or another way)
    #: before it can confirm the link.
    sign_in_required: bool


class SsoLinkConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(min_length=1, max_length=512)


class SsoLinkResult(BaseModel):
    #: Where the SPA goes next (a same-origin path).
    next: str
    user: AuthUserResponse
